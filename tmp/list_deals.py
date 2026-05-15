import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import requests
from billing.zoho_crm import get_crm_token, ZOHO_CRM_URL, _h

def list_all_deals():
    try:
        token = get_crm_token()
    except KeyError as e:
        print(f"Missing environment variable: {e}")
        return
    except Exception as e:
        print(f"Error getting token: {e}")
        return

    url = f"{ZOHO_CRM_URL}/Deals"
    resp = requests.get(url, headers=_h(token), timeout=30)
    if resp.status_code == 204:
        print("No deals found.")
        return
    resp.raise_for_status()
    
    data = resp.json().get("data", [])
    
    print(f"Found {len(data)} deals:")
    for deal in data:
        amount = deal.get("Amount", 0)
        stage = deal.get("Stage", "")
        account = deal.get("Account_Name", {}).get("name", "Unknown") if deal.get("Account_Name") else "None"
        name = deal.get("Deal_Name")
        closing = deal.get("Closing_Date")
        print(f"- {name}: {account} | ${amount} | Stage: {stage} | Close: {closing}")

if __name__ == "__main__":
    list_all_deals()
