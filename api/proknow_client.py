"""
Proknow object used to open/close connections and query database at various levels
"""
import os

import json
import tempfile
import logging
import pydicom
import numpy as np
from datetime import datetime
from hashlib import sha256
from proknow import ProKnow
from proknow.Patients import PatientItem
from dicompylercore import dicomparser, dvhcalc

import utils.geom_metrics as gm


logger = logging.getLogger(__name__)

class AskProKnow():
    def __init__(self, patient_id: str):
        self.url = 'https://nhs.proknow.com' 
        self.workspace = os.getenv("PROKNOW_WORKSPACE")
        self.credentials = os.getenv("PROKNOW_CREDS")
        self.pk = ProKnow(self.url, self.credentials)

        self.patient_id = patient_id
        self.patient_summary = self.find_patient(patient_id)
        if self.patient_summary is None:
            return 
        
        self.patient = self.patient_summary.get()
        #self.to_json(patient_id, self.patient.data)
        # Calc patient hash to check if known 
        self.patient_hash = self._calc_patient_hash(self.patient.data)

        # Downloads RTDOSEs for this patient and extracts the DoseSummationType tag. 
        # Where type == BEAM, doses are dropped.
        self.accepted_dose_ids = self._filter_doses()
    
    def _filter_doses(self):
        accepted_ids = []
        doses = self.patient.find_entities(lambda entity: entity.data['type'] == 'dose')
        for dose_ in doses:
            dose = dose_.get() 
            with tempfile.TemporaryDirectory() as tmpdir:
                dosepath = dose.download(tmpdir)
                ds = pydicom.dcmread(dosepath)
                if ds.DoseSummationType == "PLAN":
                    accepted_ids.append(dose.data['id'])

        return accepted_ids

    @staticmethod
    def _calc_patient_hash(data: dict) -> str:
        """
        Calculate SHA-256 hash of patient summary data, used to skip patient if hash is known
        data: json dict of patient summary (e.g. from self.patient.data)
        """
        hash_obj = sha256()
        byte_data = json.dumps(data).encode('utf-8')
        hash_obj.update(byte_data)
        return hash_obj.hexdigest()
    
    @staticmethod
    def to_json(filename: str, data: dict) -> None:
        with open(f'./tmp/{filename}.json', 'w') as f:
            json.dump(data, f, indent=4)

    def find_patient(self, patient_id: str) -> PatientItem:
        """Gets respective PatientItem for a given patient"""
        #patients = self.pk.patients.lookup(self.workspace, [patient_id])
        patients = self.pk.patients.query(self.workspace, search=patient_id)
        if not patients: #If patient not in PK
            logger.warning(f"Patient ({patient_id}) not found in Proknow")
            return 
        if len(patients) == 1: # As expected
            return patients[0]
    
    ## ================ PATIENTS DATA ================

    def get_patient_data(self) -> dict:
        """Fetch basic patient info from Proknow."""
        payload = {
            "sha256": self.patient_hash,
            "id": self.patient.id,
            "MRN": self.patient.mrn,
            "name": self.patient.name,
            "birth_date": self.patient.birth_date,
            "sex": self.patient.sex,
            "created_at": self.patient_summary.data["created_at"],
            "clinical_date": self.patient_summary.data["clinical_date"],
            "collections": [x["name"] for x in self.patient.data["collections"]] if self.patient.data["collections"] else None,
            "study_count": self.patient_summary.data["study_count"],
            "ct_count": self.patient_summary.data["ct_count"],
            "mr_count": self.patient_summary.data["mr_count"],
            "structure_set_count": self.patient_summary.data["structure_set_count"],
            "plan_count": self.patient_summary.data["plan_count"],
            "dose_count": self.patient_summary.data["dose_count"],
            "unknown_count": self.patient_summary.data["unknown_count"]
        }
        assert payload['MRN'] == self.patient_id, "MRN does not matched requested ID"
        return payload

    ## ============== DOSES TABLE ============================
    def get_treatment_data(self) -> dict:
        """
        Fetch treatment info for patient on Proknow
        Info about all the objects related to a treatment (image, struct, plan, dose)
        Uses patient_id as FOREIGN_KEY
        """
        # Get data from RTPLAN
        doses = self.patient.find_entities(lambda entity: entity.data['type'] == 'dose')
        payload = []
        for dose in doses:
            if dose.data['id'] not in self.accepted_dose_ids:
                continue
            # Get all parent data to this dose
            data = {
                "id": dose.data["id"],
                "MRN": self.patient.mrn,
                "image_set_id": dose.data["image_set_id"],
                "structure_set_id": dose.data["structure_set_id"],
                "plan_id": dose.data["plan_id"]
            }

            # Extract info from the plan
            plans = self.patient.find_entities(lambda entity: entity.data['id'] == dose.data['plan_id'])
            plan = plans[0].get()

            with tempfile.TemporaryDirectory() as tmpdir:
                filepath = plan.download(tmpdir)
                plan_data = self.get_data_from_plan(filepath)

            data.update(plan_data)
            payload.append(data)
        return payload
    
    @staticmethod
    def get_data_from_plan(filepath: str) -> dict:
        logger.debug("Reading RTPLAN header")
        ds = pydicom.dcmread(filepath)

        if ds.get("ManufacturerModelName"):
            planning_system = ds.get("ManufacturerModelName")
        else:
            planning_system = ds.get("Manufacturer")

        plan_name = ds.get("RTPlanLabel")
        if "DoseReferenceSequence" in ds:
            prescribed_dose = ds.DoseReferenceSequence[0].get("TargetPrescriptionDose")
        else:
            prescribed_dose = None

        if "FractionGroupSequence" in ds:
            prescribed_fractions = ds.FractionGroupSequence[0].get("NumberOfFractionsPlanned")
        else:
            prescribed_fractions = None

        if "RTPlanDate" in ds:
            plan_date = ds.get("RTPlanDate")
        else:
            plan_date = ds.get("SeriesDate")
        plan_date = datetime.strptime(plan_date, '%Y%m%d').date()
        site = ds.get("TreatmentSites")
        
        return {
            "planning_system": planning_system,
            "plan_name": plan_name,
            "plan_date": plan_date,
            "prescribed_dose": prescribed_dose,
            "prescribed_fractions": prescribed_fractions,
            "treatment_site": site,
        }

    ## ============  DVH_DATA + GEOMETRICAL METRICS ================

    def get_dose_metrics(self) -> tuple[list[dict], list[dict]]:
        """
        Computes DVH data and geometrical metrics for every accepted dose.
        Each dose's RTDOSE, RTSTRUCT, and a slice subset of its image set are
        downloaded once and shared between the two calculations (previously
        fetched/downloaded independently by each).
        """
        doses = self.patient.find_entities(lambda entity: entity.data['type'] == 'dose')

        dvh_data = []
        geom_metrics = []
        for dose in doses:
            if dose.data['id'] not in self.accepted_dose_ids:
                continue
            dose_id = dose.data['id']
            structure_set_id = dose.data['structure_set_id']
            image_set_id = dose.data['image_set_id']

            dose_item = self.patient.find_entities(lambda entity: entity.data['id'] == dose_id)[0].get()
            structure_set = self.patient.find_entities(lambda entity: entity.data['id'] == structure_set_id)[0].get()
            image_set = self.patient.find_entities(lambda entity: entity.data['id'] == image_set_id)[0].get()

            with tempfile.TemporaryDirectory() as tmpdir:
                dosepath = dose_item.download(tmpdir)
                structpath = structure_set.download(tmpdir)
                image_dir = os.path.join(tmpdir, "image")
                os.makedirs(image_dir)
                # Only 2 adjacent slices are needed for axial-orientation
                # validation and true slice spacing, not the whole series.
                self._download_image_slices(image_set, image_dir, indices=[0, 1])

                dvh_data.extend(self.calculate_dvhs(dose_id, structpath, dosepath))

                metrics = self.calculate_geometrical_metrics(structure_set_id, image_set_id, structpath, image_dir)
                for metric_ in metrics['pairwise_metrics']:
                    metric_['dose_id'] = dose_id
                    metric_['structure_set_id'] = structure_set_id
                    geom_metrics.append(metric_)

        return dvh_data, geom_metrics

    @staticmethod
    def _download_image_slices(image_set, tmpdir: str, indices: list[int]) -> list[str]:
        """
        Download only specific images from an image set, by position-sorted
        index, instead of ImageSetItem.download()'s whole-series download.
        Mirrors the per-image request ImageSetItem.download() makes
        internally; relies on ProKnow SDK internals since there's no public
        partial-download API.
        """
        images = sorted(image_set.data["data"]["images"], key=lambda img: img["pos"])
        modality = image_set.data["modality"]
        paths = []
        for i in indices:
            image = images[i]
            path = os.path.join(tmpdir, f"{modality}.{image['uid']}")
            image_set._requestor.stream(
                f"/workspaces/{image_set._workspace_id}/imagesets/{image_set._id}/images/{image['id']}/dicom",
                path,
            )
            paths.append(path)
        return paths

    def calculate_dvhs(self, dose_id: str, structpath: str, dosepath: str) -> list[dict]:
        """
        Method to get DVHs for each structure in a consistent format.
        Expects already-downloaded RTSTRUCT/RTDOSE file paths for the dose.
        """
        # Parsed once and reused across structures: dvhcalc.get_dvh() re-reads
        # its structure/dose arguments from disk on every call if given file
        # paths, but accepts pre-parsed pydicom Datasets instead.
        ds_struct = pydicom.dcmread(structpath)
        ds_dose = pydicom.dcmread(dosepath)
        struct = dicomparser.DicomParser(ds_struct)
        structures = struct.GetStructures()

        results = []
        for idx in structures:
            name = structures[idx]['name']
            dvh = dvhcalc.get_dvh(ds_struct, ds_dose, idx, interpolation_resolution=1.)
            volume = round(dvh.volume, 4)
            dvh = dvh.relative_volume
            payload = {
                'dose_id': dose_id,
                'structure_name': name,
                'cumulative_dvh': dvh.counts.tolist(),
                'volume': volume,
                'bin_width': 0.01
            }
            #dvh.plot()
            if payload['volume'] == 0:
                continue
            results.append(payload)
        return results

    def calculate_geometrical_metrics(self, structure_set_id: str, image_set_id: str, struct_path: str, image_dir: str) -> dict:
        """
        Given an already-downloaded RTSTRUCT path and a directory containing
        a slice subset of the referenced image set, calculate a range of
        metrics:
            - pairwise metrics between targets (name contains 'TV') and all OARs
            (excludes body / external):
                * Dice coefficient
                * signed 3D centroid displacement vector (target -> OAR) in patient
                mm, plus its Euclidean magnitude
                * minimum surface-to-surface distance (closest approach)
                * HD95 surface-to-surface distance (95th percentile Hausdorff)
            - informative per-structure metrics (volume, centroid).

        The image set provides the authoritative slice thickness and is validated as
        axial; geometry itself is computed from the RTSTRUCT contours. Returns a dict
        with per-structure info and the target/OAR pairwise metrics.

        Note: Code generated by Claude Opus 4.8
        """
        parser = dicomparser.DicomParser(struct_path)
        raw_structures = parser.GetStructures()

        # 1. Parse contours into per-slice polygons; skip ROIs without geometry.
        parsed: dict[int, dict] = {}
        contour_z: list[float] = []
        for roi_number, meta in raw_structures.items():
            try:
                coords = parser.GetStructureCoordinates(roi_number)
            except KeyError:
                continue
            if not coords:
                continue
            slices = gm._structure_slices(coords)
            if not slices:
                continue
            parsed[roi_number] = {"meta": meta, "slices": slices}
            contour_z.extend(slices.keys())

        # 2. Validate axial acquisition and take the authoritative slice
        #    thickness from the image, falling back to contour z-gaps only if
        #    the image cannot be read.
        planes = gm._read_image_planes(image_dir)
        if planes:
            gm._assert_axial_orientation(planes)
            thickness = gm._slice_thickness_mm(np.array([z for z, _ in planes]))
        else:
            thickness = None
        if thickness is None:
            thickness = gm._slice_thickness_mm(np.asarray(contour_z))
        if thickness is None:
            raise ValueError(
                "Cannot determine slice thickness from image set or contours "
                "(fewer than two planes)."
            )

        # 3. Derive per-structure quantities and classify.
        structures: dict[int, gm.StructureGeometry] = {}
        for roi_number, entry in parsed.items():
            slices = entry["slices"]
            volume_mm3 = gm._volume_mm3(slices, thickness)
            structures[roi_number] = gm.StructureGeometry(
                name=entry["meta"]["name"],
                role=gm._classify(entry["meta"]["name"], entry["meta"].get("type", "")),
                slices=slices,
                volume_mm3=volume_mm3,
                centroid_mm=gm._centroid_mm(slices, thickness, volume_mm3),
                surface_points_mm=gm._surface_points(slices),
            )

        targets = [s for s in structures.values() if s.role == gm.ROLE_TARGET]
        oars = [s for s in structures.values() if s.role == gm.ROLE_OAR]

        # 4. Pairwise target/OAR metrics (skip degenerate zero-volume structures).
        pairwise = []
        for target in targets:
            if target.volume_mm3 <= 0:
                continue
            for oar in oars:
                if oar.volume_mm3 <= 0:
                    continue
                displacement = oar.centroid_mm - target.centroid_mm  # mm, target -> OAR
                min_dist, hd95_dist = gm._surface_distances(
                    target.surface_points_mm, oar.surface_points_mm
                )
                pairwise.append(
                    {
                        "target": target.name,
                        "oar": oar.name,
                        "dice": round(gm._dice(target, oar, thickness), gm._DICE_DP),
                        "relative_overlap": round(gm._relative_overlap(target, oar, thickness), gm._DICE_DP),
                        "distance_vector_mm": [
                            round(float(d), gm._DISTANCE_DP) for d in displacement
                        ],
                        "distance_mm": round(float(np.linalg.norm(displacement)), gm._DISTANCE_DP),
                        "min_surface_distance_mm": (
                            round(min_dist, gm._DISTANCE_DP) if min_dist is not None else None
                        ),
                        "hd95_surface_distance_mm": (
                            round(hd95_dist, gm._DISTANCE_DP) if hd95_dist is not None else None
                        ),
                    }
                )

        structure_info = [
            {
                "name": s.name,
                "role": s.role,
                "volume_cc": round(s.volume_cc, gm._VOLUME_DP),
                "centroid_mm": (
                    [round(float(c), gm._DISTANCE_DP) for c in s.centroid_mm]
                    if s.centroid_mm is not None
                    else None
                ),
            }
            for s in structures.values()
        ]

        return {
            "structure_set_id": structure_set_id,
            "image_set_id": image_set_id,
            "slice_thickness_mm": round(thickness, gm._DISTANCE_DP),
            "structures": structure_info,
            "pairwise_metrics": pairwise,
        }




