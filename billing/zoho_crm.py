"""
Zoho CRM integration — Royalspace Billing

Keeps Zoho CRM in sync with billing activity:
  - Upsert contacts (buyers) when invoices are created
  - Log invoices as Deals in CRM
  - Mark deals paid/overdue automatically
  - Update weekly revenue from Ringba

Required env vars (same OAuth as Zoho Books):
  ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET, ZOHO_REFRESH_TOKEN_CRM
  (separate refresh token with CRM scope — see README)
"""
from __future__ import annotations

import os
import requests

ZOHO_TOKEN_URL = "https://accounts.zoho.com/oauth/v2/token"
ZOHO_CRM_URL   = "https://www.zohoapis.com/crm/v2"


# ── Auth ──────────────────────────────────────────────────────────────────────

def get_crm_token() -> str:
    resp = requests.post(ZOHO_TOKEN_URL, data={
        "grant_type":    "refresh_token",
        "client_id":     os.environ["ZOHO_CLIENT_ID"],
        "client_secret": os.environ["ZOHO_CLIENT_SECRET"],
        "refresh_token": os.environ["ZOHO_REFRESH_TOKEN_CRM"],
    }, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if "access_token" not in data:
        raise RuntimeError(f"Zoho CRM token error: {data}")
    return data["access_token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Zoho-oauthtoken {token}"}


# ── Contacts ──────────────────────────────────────────────────────────────────

def find_contact(token: str, account_name: str) -> dict | None:
    """Search for a contact/account by name. Returns first match or None."""
    resp = requests.get(
        f"{ZOHO_CRM_URL}/Accounts/search",
        headers=_h(token),
        params={"criteria": f"(Account_Name:equals:{account_name})"},
        timeout=30,
    )
    if resp.status_code == 204:
        return None
    resp.raise_for_status()
    data = resp.json().get("data", [])
    return data[0] if data else None


def upsert_contact(token: str, buyer_name: str, category: str = "") -> str:
    """
    Creates or updates a CRM Account for a buyer.
    Returns the account_id.
    """
    existing = find_contact(token, buyer_name)

    fields = {
        "Account_Name": buyer_name,
        "Description":  f"Buyer category: {category}" if category else "",
    }
    if category:
        fields["Type"] = "Partner" if category == "external" else "Vendor"

    if existing:
        account_id = existing["id"]
        requests.put(
            f"{ZOHO_CRM_URL}/Accounts/{account_id}",
            headers=_h(token),
            json={"data": [fields]},
            timeout=30,
        ).raise_for_status()
        print(f"  [CRM] Updated account: {buyer_name} ({account_id})")
        return account_id
    else:
        resp = requests.post(
            f"{ZOHO_CRM_URL}/Accounts",
            headers=_h(token),
            json={"data": [fields]},
            timeout=30,
        )
        resp.raise_for_status()
        account_id = resp.json()["data"][0]["details"]["id"]
        print(f"  [CRM] Created account: {buyer_name} ({account_id})")
        return account_id


# ── Deals ─────────────────────────────────────────────────────────────────────

def find_deal(token: str, invoice_number: str) -> dict | None:
    """Find a Deal by invoice number stored in Deal_Name."""
    resp = requests.get(
        f"{ZOHO_CRM_URL}/Deals/search",
        headers=_h(token),
        params={"criteria": f"(Deal_Name:equals:{invoice_number})"},
        timeout=30,
    )
    if resp.status_code == 204:
        return None
    resp.raise_for_status()
    data = resp.json().get("data", [])
    return data[0] if data else None


def log_invoice_deal(
    token: str,
    account_id: str,
    invoice_number: str,
    buyer_name: str,
    billed_month: str,
    revenue: float,
    due_date: str,       # "YYYY-MM-DD"
) -> str:
    """
    Creates a Deal in CRM for an invoice.
    Stage: 'Needs Analysis' (pending payment).
    Returns deal_id.
    """
    existing = find_deal(token, invoice_number)
    if existing:
        print(f"  [CRM] Deal already exists for {invoice_number}")
        return existing["id"]

    deal = {
        "Deal_Name":        invoice_number,
        "Account_Name":     {"id": account_id},
        "Stage":            "Needs Analysis",
        "Amount":           revenue,
        "Closing_Date":     due_date,
        "Description":      f"Factura {invoice_number} — {buyer_name} — {billed_month}",
        "Lead_Source":      "Internal",
    }
    resp = requests.post(
        f"{ZOHO_CRM_URL}/Deals",
        headers=_h(token),
        json={"data": [deal]},
        timeout=30,
    )
    resp.raise_for_status()
    deal_id = resp.json()["data"][0]["details"]["id"]
    print(f"  [CRM] Created deal: {invoice_number} (${revenue:,.2f}) → {deal_id}")
    return deal_id


def mark_deal_paid(token: str, invoice_number: str, payment_date: str) -> bool:
    """
    Moves a Deal to stage 'Closed Won' and records payment date.
    Returns True if found and updated.
    """
    deal = find_deal(token, invoice_number)
    if not deal:
        print(f"  [CRM] Deal not found for {invoice_number}")
        return False

    deal_id = deal["id"]
    requests.put(
        f"{ZOHO_CRM_URL}/Deals/{deal_id}",
        headers=_h(token),
        json={"data": [{
            "Stage":        "Closed Won",
            "Closing_Date": payment_date,
            "Description":  deal.get("Description", "") + f"\nPagado: {payment_date}",
        }]},
        timeout=30,
    ).raise_for_status()
    print(f"  [CRM] Deal {invoice_number} → Closed Won ({payment_date})")
    return True


def mark_deal_overdue(token: str, invoice_number: str, days_overdue: int) -> bool:
    """
    Moves a Deal to stage 'Needs Analysis' with overdue note.
    Returns True if found and updated.
    """
    deal = find_deal(token, invoice_number)
    if not deal:
        return False

    deal_id = deal["id"]
    requests.put(
        f"{ZOHO_CRM_URL}/Deals/{deal_id}",
        headers=_h(token),
        json={"data": [{
            "Stage":       "Value Proposition",   # custom stage for overdue
            "Description": deal.get("Description", "") + f"\nVENCIDO — {days_overdue} días sin pagar",
        }]},
        timeout=30,
    ).raise_for_status()
    print(f"  [CRM] Deal {invoice_number} → Overdue ({days_overdue} days)")
    return True


def update_buyer_revenue(token: str, buyer_name: str, revenue_mtd: float, month_label: str) -> bool:
    """
    Updates the Description of the buyer's CRM Account with current month revenue.
    Returns True if account found and updated.
    """
    account = find_contact(token, buyer_name)
    if not account:
        print(f"  [CRM] Account not found for {buyer_name} — skipping revenue update")
        return False

    account_id = account["id"]
    requests.put(
        f"{ZOHO_CRM_URL}/Accounts/{account_id}",
        headers=_h(token),
        json={"data": [{
            "Description": f"Revenue {month_label}: ${revenue_mtd:,.2f}",
        }]},
        timeout=30,
    ).raise_for_status()
    print(f"  [CRM] {buyer_name} revenue updated: ${revenue_mtd:,.2f} ({month_label})")
    return True
