import os
from mysql.connector import Error
import mysql.connector
from dotenv import load_dotenv
from pathlib import Path
# Load environment variables
load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env", override=True)

def get_db_connection():
    """Create and return a MySQL database connection."""
    try:
        conn = mysql.connector.connect(
            host=os.getenv('DB_HOST'),
            user=os.getenv('DB_USER'),
            password=os.getenv('DB_PASSWORD'),
            database=os.getenv('DB_NAME')
        )
        if conn.is_connected():
            return conn
    except Error as e:
        print(f"Error connecting to MySQL database: {e}")
        return None