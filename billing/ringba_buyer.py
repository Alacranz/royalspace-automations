"""
Ringba revenue by buyer — Royalspace Billing

Groups call logs by buyerName and aggregates revenue (conversionAmount).
The "Revenue" column in the Ringba Buyer report = sum of conversionAmount
for all calls routed to each buyer.
"""
from __future__ import annotations

import re
import sys
import os
from datetime import datetime, timedelta, timezone

import pytz
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "profit"))
from common.ringba_client import RINGBA_BASE_URL, PAGE_SIZE, MAX_PAGES, to_float

VET = pytz.timezone("America/Caracas")


def normalize_buyer_name(name: str) -> str:
    """
    Strips Ringba's numeric prefix e.g. '(701) Aragon Advertising' → 'aragon advertising'
    Same logic as normalize_name() in ringba_client.py but applied to buyer names.
    """
    if not name or not name.strip():
        return ""
    n = name.strip()
    n = re.sub(r'^\(\d+\)\s*', '', n)
    return n.strip().lower()


def get_month_utc_range(year: int, month: int, tz_name: str = "America/New_York") -> tuple[datetime, datetime]:
    """
    Returns (start_utc, end_utc) covering the full calendar month in the given timezone.
    """
    tz = pytz.timezone(tz_name)
    # First moment of the month
    start_local = tz.localize(datetime(year, month, 1, 0, 0, 0))
    # First moment of the next month - 1 second = last moment of this month
    if month == 12:
        next_month = datetime(year + 1, 1, 1, 0, 0, 0)
    else:
        next_month = datetime(year, month + 1, 1, 0, 0, 0)
    end_local = tz.localize(next_month) - timedelta(seconds=1)

    return (
        start_local.astimezone(timezone.utc).replace(tzinfo=timezone.utc),
        end_local.astimezone(timezone.utc).replace(tzinfo=timezone.utc),
    )


def get_current_month_utc_range(tz_name: str = "America/New_York") -> tuple[datetime, datetime]:
    """Returns (start_utc, now_utc) for the current month so far."""
    tz = pytz.timezone(tz_name)
    now_local = datetime.now(tz)
    start_local = now_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return (
        start_local.astimezone(timezone.utc).replace(tzinfo=timezone.utc),
        datetime.now(timezone.utc),
    )


def get_buyer_revenue(
    token: str,
    account_id: str,
    start_utc: datetime,
    end_utc: datetime,
    verbose: bool = False,
) -> dict[str, dict]:
    """
    Queries Ringba calllogs and aggregates revenue by buyerName.

    Returns dict: normalized_buyer_name → {
        raw_name, revenue, calls, conversions, connected
    }

    Revenue = sum of conversionAmount (what the buyer pays Royalspace).
    Uses daily chunking to avoid pagination instability.
    """
    buyer_map: dict[str, dict] = {}
    total_fetched = 0
    day_num = 0
    chunk_start = start_utc

    while chunk_start < end_utc:
        day_num += 1
        chunk_end = min(chunk_start + timedelta(hours=24) - timedelta(seconds=1), end_utc)
        day_fetched = 0
        offset = 0

        for _page in range(1, MAX_PAGES + 1):
            url = f"{RINGBA_BASE_URL}/{account_id}/calllogs"
            headers = {
                "Authorization": f"Token {token}",
                "Accept":        "application/json",
                "Content-Type":  "application/json",
            }
            body = {
                "reportStart": chunk_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "reportEnd":   chunk_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "size":        PAGE_SIZE,
                "offset":      offset,
            }
            resp = requests.post(url, headers=headers, json=body, timeout=60)
            resp.raise_for_status()
            data = resp.json()
            records = (data.get("report") or {}).get("records") or []

            if not records:
                break


            for r in records:
                raw = str(r.get("buyer") or r.get("buyerName") or "")
                key = normalize_buyer_name(raw) or "unknown"

                if key not in buyer_map:
                    buyer_map[key] = {
                        "raw_name":    raw,
                        "revenue":     0.0,
                        "calls":       0,
                        "connected":   0,
                        "conversions": 0,
                    }

                m = buyer_map[key]
                m["calls"] += 1
                if r.get("hasConnected") is True:
                    m["connected"] += 1
                if r.get("hasConverted") is True:
                    m["conversions"] += 1
                m["revenue"] += to_float(r.get("conversionAmount"))

            day_fetched   += len(records)
            total_fetched += len(records)
            offset        += len(records)

            if len(records) < PAGE_SIZE:
                break

        if verbose:
            print(f"  [Ringba] Day {day_num} ({chunk_start.strftime('%m/%d')}): {day_fetched} records")
        chunk_start += timedelta(hours=24)

    print(f"  [Ringba Buyer] Total records processed: {total_fetched}")
    return buyer_map


def find_buyer(buyer_map: dict, buyer_name: str) -> float:
    """
    Find a buyer's revenue in the map by matching their name (case-insensitive, prefix-stripped).
    Returns 0.0 if not found.
    """
    key = normalize_buyer_name(buyer_name)
    entry = buyer_map.get(key)
    return entry["revenue"] if entry else 0.0


def find_buyer_data(buyer_map: dict, buyer_name: str) -> dict | None:
    """Returns the full buyer data dict, or None if not found."""
    key = normalize_buyer_name(buyer_name)
    return buyer_map.get(key)
