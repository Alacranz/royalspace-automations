"""
Zoho Books API client — Royalspace Billing

Handles OAuth2 token refresh, contact lookup, invoice creation and sending.
Org ID: 771911284 (Royalspace)
"""
from __future__ import annotations

import requests

ZOHO_TOKEN_URL    = "https://accounts.zoho.com/oauth/v2/token"
ZOHO_BOOKS_URL    = "https://www.zohoapis.com/books/v3"


# ── Auth ──────────────────────────────────────────────────────────────────────

def get_access_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    """Exchange refresh token for a short-lived access token."""
    resp = requests.post(ZOHO_TOKEN_URL, data={
        "grant_type":    "refresh_token",
        "client_id":     client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
    }, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if "access_token" not in data:
        raise RuntimeError(f"Zoho token error: {data}")
    return data["access_token"]


def _headers(token: str) -> dict:
    return {"Authorization": f"Zoho-oauthtoken {token}", "Content-Type": "application/json"}


# ── Contacts ──────────────────────────────────────────────────────────────────

def get_contact_id(token: str, org_id: str, contact_name: str) -> str:
    """Find a contact by name and return their contact_id."""
    url = f"{ZOHO_BOOKS_URL}/contacts"
    params = {"organization_id": org_id, "contact_name": contact_name}
    resp = requests.get(url, headers=_headers(token), params=params, timeout=30)
    resp.raise_for_status()
    contacts = resp.json().get("contacts", [])
    if not contacts:
        raise ValueError(f"Contact '{contact_name}' not found in Zoho Books")
    return contacts[0]["contact_id"]


# ── Items ─────────────────────────────────────────────────────────────────────

def get_item_id(token: str, org_id: str, item_name: str) -> str | None:
    """Find an item by name. Returns None if not found (will use name directly)."""
    url = f"{ZOHO_BOOKS_URL}/items"
    params = {"organization_id": org_id, "name": item_name}
    resp = requests.get(url, headers=_headers(token), params=params, timeout=30)
    resp.raise_for_status()
    items = resp.json().get("items", [])
    return items[0]["item_id"] if items else None


# ── Invoices ──────────────────────────────────────────────────────────────────

def create_invoice(
    token: str,
    org_id: str,
    contact_id: str,
    invoice_date: str,        # "YYYY-MM-DD"
    due_date: str,            # "YYYY-MM-DD"
    line_items: list[dict],   # [{"name": ..., "description": ..., "quantity": ..., "rate": ...}]
    reference_number: str = "",
) -> dict:
    """
    Create a draft invoice in Zoho Books.
    Returns the full invoice object from the API.
    """
    url = f"{ZOHO_BOOKS_URL}/invoices"
    params = {"organization_id": org_id}
    body = {
        "customer_id":      contact_id,
        "invoice_date":     invoice_date,
        "due_date":         due_date,
        "payment_terms":    30,
        "line_items":       line_items,
    }
    if reference_number:
        body["reference_number"] = reference_number

    resp = requests.post(url, headers=_headers(token), params=params, json=body, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Zoho create invoice error: {data}")
    return data["invoice"]


def send_invoice(token: str, org_id: str, invoice_id: str) -> None:
    """
    Send an invoice via email using the contact's emails stored in Zoho.
    Zoho uses the To/CC emails already configured in the customer profile.
    """
    url = f"{ZOHO_BOOKS_URL}/invoices/{invoice_id}/email"
    params = {"organization_id": org_id}
    body = {"send_from_org_email_id": True}
    resp = requests.post(url, headers=_headers(token), params=params, json=body, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Zoho send invoice error: {data}")
    print(f"  [Zoho] Invoice {invoice_id} sent successfully")


def get_invoice(token: str, org_id: str, invoice_id: str) -> dict:
    """Fetch a single invoice by ID."""
    url = f"{ZOHO_BOOKS_URL}/invoices/{invoice_id}"
    params = {"organization_id": org_id}
    resp = requests.get(url, headers=_headers(token), params=params, timeout=30)
    resp.raise_for_status()
    return resp.json().get("invoice", {})


def list_invoices(
    token: str,
    org_id: str,
    contact_id: str | None = None,
    status: str | None = None,
) -> list[dict]:
    """List invoices, optionally filtered by contact or status."""
    url = f"{ZOHO_BOOKS_URL}/invoices"
    params: dict = {"organization_id": org_id}
    if contact_id:
        params["customer_id"] = contact_id
    if status:
        params["status"] = status

    all_invoices = []
    page = 1
    while True:
        params["page"] = page
        resp = requests.get(url, headers=_headers(token), params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        invoices = data.get("invoices", [])
        all_invoices.extend(invoices)
        if not data.get("page_context", {}).get("has_more_page"):
            break
        page += 1

    return all_invoices
