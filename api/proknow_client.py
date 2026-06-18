"""
Proknow object used to open/close connections and query database at various levels
"""
import os
import json
import tempfile
import logging
import pydicom
from datetime import datetime
from hashlib import sha256
from proknow import ProKnow
from dicompylercore import dicomparser, dvhcalc

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

    def find_patient(self, patient_id: str) -> ProKnow.PatientItem:
        """Gets respective PatientItem for a given patient"""
        #patients = self.pk.patients.lookup(self.workspace, [patient_id])
        patients = self.pk.patients.query(self.workspace, search=patient_id)
        if not patients: #If patient not in PK
            logger.warning(f"Patient ({patient_id}) not found in Proknow")
            return 
        if len(patients) == 1: # As expected
            return patients[0]
        

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
        plan_date = datetime.strptime(plan_date, '%Y%M%d').date()
        site = ds.get("TreatmentSites")
        
        return {
            "planning_system": planning_system,
            "plan_name": plan_name,
            "plan_date": plan_date,
            "prescribed_dose": prescribed_dose,
            "prescribed_fractions": prescribed_fractions,
            "treatment_site": site,
        }

    def get_dvh_data(self) -> dict:
        doses = self.patient.find_entities(lambda entity: entity.data['type'] == 'dose')

        all_data = []
        for dose in doses:
            all_data.extend(self.calculate_dvhs(dose.data['id'], dose.data['structure_set_id']))
            ##
        return all_data


    def calculate_dvhs(self, dose_id: str, structure_set_id: str):
        """
        Method to get DVHs for each structure in a consistent format. 
        Expects PK dose_id and associated parent structure_set_id
        """

        dose = self.patient.find_entities(lambda entity: entity.data['id'] == dose_id)[0].get()
        structure_set = self.patient.find_entities(lambda entity: entity.data['id'] == structure_set_id)[0].get()

        results = []
        with tempfile.TemporaryDirectory() as tmpdir:
            dosepath = dose.download(tmpdir)
            structpath = structure_set.download(tmpdir)
            struct = dicomparser.DicomParser(structpath)
            structures = struct.GetStructures() 
            for idx in structures:
                name = structures[idx]['name']
                dvh = dvhcalc.get_dvh(structpath, dosepath, idx, interpolation_resolution=1.)
                dvh = dvh.relative_volume
                payload = {
                    'dose_id': dose_id,
                    'structure_name': name,
                    'cumulative_dvh': dvh.counts.tolist(),
                    'volume': dvh.volume,
                    'bin_width': 0.01
                }
                #dvh.plot()
                if payload['volume'] == 0:
                    continue
                results.append(payload)
        return results