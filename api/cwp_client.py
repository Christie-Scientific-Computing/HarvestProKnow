"""
Class to interact with CWP database.
Patient-level queries only.

"""
import os
import pyodbc
import pymssql
import logging
import json

logger = logging.getLogger(__name__)


class AskCWP():
    def __init__(self, patient_id: str):
        self.server = "CHT-BI-DEV01"
        self.database = "EForms"
        self.driver = '{FreeTDS}'#'{ODBC Driver 17 for SQL Server}'
        self.patient_id = patient_id
        with open(os.getenv("CHRISTIE_CREDS"), "r") as f:
            self.credentials = json.load(f)
        self.conn = self.connect()


    def disconnect(self):
        self.conn.close()

    def connect(self):
        #TODO: This only works on Windows!
        conn_string = (
            f'DRIVER={self.driver};'
            f'SERVER={self.server};'
            f'PORT=1433;'
            f'DATABASE={self.database};'
            f'Trusted_Connection=yes;'
            f'UID={self.credentials['username']};'
            f'PWD={self.credentials['password']};'
            f'TDS_Version=8.0;'
        )
        try: 
            conn = pyodbc.connect(conn_string)
            return conn
        except Exception as e:
            logger.error(f"Failed to connect to CWP database: {e}")
            raise

    def get_booking_data(self):
        raw_data = self.fetch_data()
        if not raw_data:
            logger.warning("No booking form found!")
            return
            
        payload = []
        for elem in raw_data:
            data = self.filter_data(elem)
            payload.append(data)
        return {'data': payload}


    def filter_data(self, raw: list[dict]):
        """Filters a single row by removing unnecessary data"""
        return {
            "MRN": raw['ChristieNo'],
            "sex": raw['PatientSex'],
            "age": raw['PatientAge'],
            "decision_to_treat_date": raw['DecisionToTreatDate'],
            "diagnosis": raw["Diagnosis"],
            "treatment_intent": raw['TreatmentIntent'],
            "treatment_site": raw['TreatmentSite'],
            "primary_site": raw['PrimaryTreatmentSite'],
            "metastatic_site": raw['MetastaticSite'],
            "regional_node_site": raw['RegionalNodalSite'],
            "side_of_nodal_treatment": raw['SideOfNodalTreatmentSite'],
            "treatment_by": raw['TreatmentBy'],
            "treatment_category": raw['TreatmentCategory'],
            "treatment_modality": raw['Modality'],
            "fractions": raw['Fractions'],
            "fractionation": raw['Fractionation'],
            "chemotherapy": raw['Chemotherapy'],
            "previous_radiotherapy": raw['HasThePatientHadPreviousRadiotherapy'],
            "has_pacemaker": raw['DoesThePatientHaveAPacemaker'],
        }

    def fetch_data(self):
        cursor = self.conn.cursor()
        
        sql = "EXEC [EForms].[dbo].[usp_GetLatestRadiotherapyBooking] @ChristieNo = ?"
        cursor.execute(sql, self.patient_id)
        
        columns = [column[0] for column in cursor.description]
        rows = cursor.fetchall()

        return [dict(zip(columns, row)) for row in rows]