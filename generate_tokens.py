import os
import json
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

# Scopes required for YouTube Upload
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

def generate_tokens():
    client_secret_file = 'client_secret.json'
    token_file = 'token.json'
    
    if not os.path.exists(client_secret_file):
        print(f"Error: {client_secret_file} not found. Please download it from Google Cloud Console.")
        return

    # Run the local server flow for the initial authorization
    flow = InstalledAppFlow.from_client_secrets_file(client_secret_file, SCOPES)
    creds = flow.run_local_server(port=0)

    # Save the credentials for future use
    with open(token_file, 'w') as token:
        token.write(creds.to_json())
    
    print("\n" + "="*50)
    print("SUCCESS! Credentials generated.")
    print("="*50)
    print(f"File saved: {token_file}")
    print("\nTO USE IN GITHUB ACTIONS (as Secrets):")
    print(f"1. Open {token_file}")
    print("2. Copy the entire JSON content.")
    print("3. Create a GitHub Secret named 'YOUTUBE_TOKEN' and paste the content.")
    print("="*50)

if __name__ == "__main__":
    generate_tokens()
