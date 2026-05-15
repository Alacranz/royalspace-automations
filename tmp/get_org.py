import os
import sys
import json
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from billing.zoho_crm import get_crm_token, ZOHO_CRM_URL, _h

def get_org_details():
    try:
        token = get_crm_token()
    except Exception as e:
        print(f"Error getting token: {e}")
        return

    url = f"{ZOHO_CRM_URL}/org"
    resp = requests.get(url, headers=_h(token), timeout=30)
    
    if resp.status_code == 204:
        print("No organization details found (204).")
        return
        
    try:
        resp.raise_for_status()
        data = resp.json()
        print(json.dumps(data, indent=2))
    except Exception as e:
        print(f"Error making request: {e}")
        print(resp.text)

if __name__ == "__main__":
    get_org_details()
