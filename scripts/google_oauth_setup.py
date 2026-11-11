"""운영자 PC에서 1회 실행. Google Desktop OAuth credentials.json 필요.

python -m pip install google-auth-oauthlib
python scripts/google_oauth_setup.py --credentials ./credentials.json --output ./secrets/google-token.json
"""
import argparse
import os
from pathlib import Path
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--credentials", required=True)
    p.add_argument("--output", default="secrets/google-token.json")
    args = p.parse_args()
    creds = InstalledAppFlow.from_client_secrets_file(
        args.credentials, SCOPES
    ).run_local_server(port=0, access_type="offline", prompt="consent")
    if not creds.refresh_token:
        raise SystemExit("refresh_token을 받지 못했습니다. OAuth 동의 화면 설정을 확인하세요.")
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(creds.to_json(), encoding="utf-8")
    os.chmod(path, 0o600)
    print(f"Google OAuth token saved to {path}. DO NOT COMMIT IT.")

if __name__ == "__main__":
    main()
