"""
Ringba revenue by buyer — Royalspace Billing

Groups call logs by targetBuyerSubId (numeric ID) and aggregates revenue.
The "Revenue" column in the Ringba Buyer report = sum of conversionAmount
for all calls routed to each buyer.

Uses targetBuyerSubId (e.g. "1401") instead of buyer name to avoid
naming variations between API and UI.
"""
from __future__ import annotations

import sys
import os
from datetime import datetime, timedelta, timezone

import pytz
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "profit"))
from common.ringba_client import RINGBA_BASE_URL, PAGE_SIZE, MAX_PAGES, to_float


def get_month_utc_range(year: int, month: int, tz_name: str = "America/Caracas") -> tuple[datetime, datetime]:
    """
    Returns (start_utc, end_utc) covering the full calendar month in the given timezone.
    """
    tz = pytz.timezone(tz_name)
    start_local = tz.localize(datetime(year, month, 1, 0, 0, 0))
    if month == 12:
        next_month = datetime(year + 1, 1, 1, 0, 0, 0)
    else:
        next_month = datetime(year, month + 1, 1, 0, 0, 0)
    end_local = tz.localize(next_month) - timedelta(seconds=1)

    return (
        start_local.astimezone(timezone.utc).replace(tzinfo=timezone.utc),
        end_local.astimezone(timezone.utc).replace(tzinfo=timezone.utc),
    )


def get_half_month_utc_range(year: int, month: int, half: int, tz_name: str = "America/Caracas") -> tuple[datetime, datetime]:
    """
    Returns (start_utc, end_utc) for either half of a month.
    half=1 → days 1–15, half=2 → days 16–end of month.
    """
    import calendar as _cal
    tz       = pytz.timezone(tz_name)
    last_day = _cal.monthrange(year, month)[1]
    if half == 1:
        start_local = tz.localize(datetime(year, month, 1,  0,  0,  0))
        end_local   = tz.localize(datetime(year, month, 15, 23, 59, 59))
    else:
        start_local = tz.localize(datetime(year, month, 16,       0,  0,  0))
        end_local   = tz.localize(datetime(year, month, last_day, 23, 59, 59))
    return (
        start_local.astimezone(timezone.utc).replace(tzinfo=timezone.utc),
        end_local.astimezone(timezone.utc).replace(tzinfo=timezone.utc),
    )


def get_current_month_utc_range(tz_name: str = "America/Caracas") -> tuple[datetime, datetime]:
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
    Queries Ringba calllogs and aggregates revenue by targetBuyerSubId.

    Returns dict: buyer_sub_id (str) → {
        raw_name, sub_id, revenue, calls, conversions, connected
    }

    Revenue = sum of conversionAmount (what the buyer pays Royalspace).

    Uses 12-hour chunks to stay under 1000 records per request and avoid
    pagination instability (Ringba doesn't guarantee stable ordering across pages).
    Billing calllogs cover ALL buyers → more records/day than the profit system.
    """
    CHUNK_HOURS = 6

    buyer_map: dict[str, dict] = {}
    total_fetched = 0
    chunk_num = 0
    chunk_start = start_utc

    while chunk_start < end_utc:
        chunk_num += 1
        chunk_end = min(chunk_start + timedelta(hours=CHUNK_HOURS) - timedelta(seconds=1), end_utc)
        chunk_fetched = 0
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
                sub_id = str(r.get("targetBuyerSubId") or "")
                if not sub_id:
                    continue

                raw_name = str(r.get("buyer") or "")

                if sub_id not in buyer_map:
                    buyer_map[sub_id] = {
                        "raw_name":    raw_name,
                        "sub_id":      sub_id,
                        "revenue":     0.0,
                        "calls":       0,
                        "connected":   0,
                        "conversions": 0,
                    }

                m = buyer_map[sub_id]
                m["calls"] += 1
                if r.get("hasConnected") is True:
                    m["connected"] += 1
                if r.get("hasConverted") is True:
                    m["conversions"] += 1
                m["revenue"] += to_float(r.get("conversionAmount"))

            chunk_fetched  += len(records)
            total_fetched  += len(records)
            offset         += len(records)

            if len(records) < PAGE_SIZE:
                break

        if verbose:
            print(f"  [Ringba] Chunk {chunk_num} ({chunk_start.strftime('%m/%d %H:%M')}): {chunk_fetched} records")
        chunk_start += timedelta(hours=CHUNK_HOURS)

    print(f"  [Ringba Buyer] Total records processed: {total_fetched}")
    return buyer_map


def find_buyer_data(buyer_map: dict, buyer_sub_id: str) -> dict | None:
    """Returns buyer data by numeric sub ID (e.g. '1401'), or None if not found."""
    return buyer_map.get(str(buyer_sub_id))
