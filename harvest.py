"""
Harvest Proknow data and write to PostgreSQL database.

Reads a CSV of patient IDs, queries Proknow for metadata, scorecards,
custom metrics, and planning data, then writes results to a psql database.
"""
import os
import csv
import logging
import psycopg
from dotenv import load_dotenv

from api import AskProKnow
from api import AskCWP
from utils.setup import init_logger, init_config

load_dotenv("/config/.secrets/HarvestProknow/.env")

logger = logging.getLogger(__name__)

class ProknowHarvester:
    """Orchestrates data collection from Proknow and writes to Postgres database."""

    def __init__(self, db_host: str, db_port: int, db_name: str, db_user: str, db_password: str):
        """Initialize database connection parameters."""
        self.db_host = db_host
        self.db_port = db_port
        self.db_name = db_name
        self.db_user = db_user
        self.db_password = db_password
        self.conn = None

    def __enter__(self):
        """Context manager entry."""
        self._connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self._disconnect()

    def _connect(self):
        """Establish database connection."""
        try:
            conn_string = (
                f"postgresql://{self.db_user}:{self.db_password}@"
                f"{self.db_host}:{self.db_port}/{self.db_name}"
            )
            logger.info(f"Opening connection to: postgresql://{self.db_host}:{self.db_port}")
            self.conn = psycopg.connect(conn_string)
            logger.info("Connected to database")
        except psycopg.Error as e:
            logger.error(f"Failed to connect to database: {e}")
            raise

    def _disconnect(self):
        """Close database connection."""
        if self.conn:
            self.conn.close()
            logger.info("Disconnected from database")

    def check_hash_in_db(self, hash_: str) -> bool:
        """
        Checks if a hash appears in the sha256 column of patient table
        If so, skips patient (return True).
        """
        
        cursor = self.conn.cursor()
        sql = f"SELECT EXISTS (SELECT 1 FROM patients WHERE sha256 = %s LIMIT 1)"
        return cursor.execute(sql, (hash_,)).fetchone()[0] # Returns (True,) or (False,) 

    def read_patient_ids(self, csv_path: str) -> list[str]:
        """Read patient IDs from CSV file."""
        patient_ids = []
        try:
            with open(csv_path, "r") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    patient_id = row.get("patient_id")
                    if patient_id:
                        patient_ids.append(patient_id)
            logger.info(f"Read {len(patient_ids)} patient IDs from {csv_path}")
        except FileNotFoundError:
            logger.error(f"CSV file not found: {csv_path}")
            raise
        except Exception as e:
            logger.error(f"Error reading CSV: {e}")
            raise
        return patient_ids

    def fetch_proknow_data(self, patient_id: str) -> dict:
        """Fetch data from Proknow for given patient IDs."""
        
        # NOTE: Add extra result types here
        results = {
            "patient_data": [],
            "treatment_data": [],
            "dvh_data": [],
            "geom_metrics": []
        }


        logger.info(f"Fetching data for {patient_id}")
        # NOTE: Will skip patient if patient summary has not changed
        AskPK = AskProKnow(patient_id)
        if AskPK.patient_summary is None:
            return
        #AskCWP_ = AskCWP(patient_id)

        # Checks hash against db
        if self.check_hash_in_db(AskPK.patient_hash):
            logger.info("Patient already in db, skipping")
            #return
        try:
            results["patient_data"].append(AskPK.get_patient_data())
            results["treatment_data"].extend(AskPK.get_treatment_data())
            #results["dvh_data"].extend(AskPK.get_dvh_data())
            results["geom_metrics"].extend(AskPK.get_geometrical_metrics())
            # Add CWP call (booking_form) 
            #results["booking_form"].extend(AskCWP.get_booking_data()) #NOTE: append vs extend?

            logger.debug(f"Fetched data for patient {patient_id}")

        except Exception as e:
            logger.error(f"Failed to fetch data for patient {patient_id}: {e}")
            return

            #AskCWP_.disconnect()
        
        return results

    def write_results_to_db(self, results: dict):
        """Write fetched results to database."""
        if not self.conn:
            raise RuntimeError("Database connection not established")

        cursor = self.conn.cursor()
        try:
            # NOTE: Add table writes here
            # Write
            if results["patient_data"]:
                self._write_table(cursor, "patients", results["patient_data"])
            
            if results["treatment_data"]:
                self._write_table(cursor, "doses", results["treatment_data"])

            if results["dvh_data"]:
                self._write_table(cursor, "dvh_data", results["dvh_data"], id_col=("dose_id", "structure_name"))

            if results["geom_metrics"]:
                self._write_table(cursor, "geom_metrics", results["geom_metrics"], id_col=("dose_id", "structure_set_id", "target", "oar"))

            self.conn.commit()
            logger.info("Successfully wrote results to database")

        except psycopg.Error as e:
            self.conn.rollback()
            logger.error(f"Database write error: {e}")
            raise
        finally:
            cursor.close()

    def _write_table(self, cursor, table_name: str, data: dict, id_col: str = "id"):
        """Helper to write data to a specific table."""
        if not data:
            return
        
        columns = list(data[0].keys())
        placeholders = ", ".join(["%s"] * len(columns))
        
        if isinstance(id_col, str):
            update_clause = ", ".join(
                f"{col} = EXCLUDED.{col}" for col in columns if col != id_col
            )
        elif isinstance(id_col, tuple):
            update_clause = ", ".join(
                f"{col} = EXCLUDED.{col}" for col in columns if col not in id_col
            )
            id_col = ', '.join(_ for _ in id_col)

        query = f"INSERT INTO {table_name} ({', '.join(columns)}) VALUES ({placeholders}) ON CONFLICT ({id_col}) DO UPDATE SET {update_clause}"

        values = [tuple(row.values()) for row in data]
        cursor.executemany(query, values)
        logger.debug(f"Inserted {len(data)} rows into {table_name}")

    def run(self, csv_path: str):
        """Execute full harvest workflow."""
        logger.info("Starting Proknow harvest")

        patient_ids = self.read_patient_ids(csv_path)
        for patient_id in patient_ids:
            results = self.fetch_proknow_data(patient_id)
            if results:
                self.write_results_to_db(results)
            else:
                logger.warning("No results found!")
        logger.info("Proknow harvest completed")


def main():
    """Main entry point."""
    config = init_config()
    init_logger(config)

    db_config = {
        "db_host": os.getenv('DB_HOST'),
        "db_port": os.getenv('DB_PORT'),
        "db_name": os.getenv('DB_NAME'),
        "db_user": os.getenv('DB_USER'),
        "db_password": os.getenv('DB_PASS'),
    }

    with ProknowHarvester(**db_config) as harvester:
        harvester.run(config['path_to_csv'])


if __name__ == "__main__":
    main()
