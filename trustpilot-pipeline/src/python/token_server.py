from flask import Flask, jsonify
from google.oauth2 import service_account
import google.auth.transport.requests
import jwt
import time
import json

app = Flask(__name__)

CREDENTIALS_FILE = r"C:\Users\Manne\Downloads\customer-support-ai-analysis-7f54c0433f13.json"
SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]

@app.route("/token")
def get_token():
    creds = service_account.Credentials.from_service_account_file(
        CREDENTIALS_FILE, scopes=SCOPES
    )
    creds.refresh(google.auth.transport.requests.Request())
    return jsonify({"token": creds.token})

@app.route("/jwt")
def get_jwt():
    with open(CREDENTIALS_FILE) as f:
        sa = json.load(f)

    now = int(time.time())
    payload = {
        "iss": sa["client_email"],
        "sub": sa["client_email"],
        "aud": "https://texttospeech.googleapis.com/",
        "iat": now,
        "exp": now + 3600
    }

    token = jwt.encode(payload, sa["private_key"], algorithm="RS256")
    return jsonify({"token": token})

if __name__ == "__main__":
    print("Token server running at http://localhost:5000/token")
    print("JWT endpoint available at http://localhost:5000/jwt")
    app.run(port=5000)
