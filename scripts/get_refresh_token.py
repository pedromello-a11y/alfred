"""
Roda uma vez para obter o refresh token do Google Calendar.
Uso: python scripts/get_refresh_token.py
"""
import os
from google_auth_oauthlib.flow import InstalledAppFlow

CLIENT_ID     = os.getenv("GOOGLE_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")

SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
]

flow = InstalledAppFlow.from_client_config(
    {
        "web": {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "redirect_uris": ["http://localhost:8080"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    },
    scopes=SCOPES,
)

creds = flow.run_local_server(port=8080, prompt="consent", access_type="offline")

print("\n" + "="*60)
print("REFRESH TOKEN:")
print(creds.refresh_token)
print("="*60)
print("\nAdicione no Railway (web → Variables):")
print(f"GOOGLE_CLIENT_ID     = {CLIENT_ID}")
print(f"GOOGLE_CLIENT_SECRET = {CLIENT_SECRET}")
print(f"GOOGLE_REFRESH_TOKEN = {creds.refresh_token}")
