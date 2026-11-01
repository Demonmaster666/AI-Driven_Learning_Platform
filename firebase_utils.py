# firebase_utils.py
import os
import json
import firebase_admin
from firebase_admin import credentials, firestore

def init_firebase(app=None):
    """
    Initialize Firebase Admin SDK and return firestore client.
    Looks for firebase_service_account.json file or FIREBASE_CREDENTIALS_JSON env var.
    """
    if firebase_admin._apps:
        return firestore.client()

    # Try file
    json_path = os.path.join(os.getcwd(), "firebase_service_account.json")
    if os.path.exists(json_path):
        cred = credentials.Certificate(json_path)
        firebase_admin.initialize_app(cred)
        return firestore.client()

    # Try env var
    creds_json = os.getenv("FIREBASE_CREDENTIALS_JSON")
    if creds_json:
        try:
            cred_dict = json.loads(creds_json)
            cred = credentials.Certificate(cred_dict)
            firebase_admin.initialize_app(cred)
            return firestore.client()
        except Exception as e:
            raise RuntimeError(f"Invalid FIREBASE_CREDENTIALS_JSON: {e}")

    raise RuntimeError("Firebase credentials not found. Provide firebase_service_account.json or FIREBASE_CREDENTIALS_JSON env var.")
