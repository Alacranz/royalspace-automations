"""Read-only recent dental availability based on observed Calls and Pings."""
from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import re
from threading import Lock
from time import monotonic

import requests

from app.callgrid.client import CALLGRID_BASE_URL

CAMPAIGN = "cmtjfrqup06lu07jv0w93og2g"
ZIP_RE = re.compile(r"^\d{5}$")
CACHE_SECONDS = 75
BACKOFF_SECONDS = 120
MAX_PAGES = 12
PAGE_SIZE = 100
_lock = Lock()
_cache = None
_retry_after = 0.0


def field(row, *names):
    tags = row.get("tags")
    for name in names:
        for obj in (row, tags if isinstance(tags, dict) else {}):
            v = obj.get(name)
            if v is not None and v != "":
                return v
    return None


def instant(value):
    try:
        x = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return x.astimezone(timezone.utc) if x.tzinfo is not None else None
    except ValueError:
        return None


def truth(value):
    return str(value).lower() in ("true", "1", "1.0")


def positive(value):
    try:
        return float(value or 0) > 0
    except (ValueError, TypeError):
        return False


def campaign_ok(row):
    return field(row, "CampaignId", "campaignId") == CAMPAIGN


def buy_key(row):
    return re.sub(r"^\(\s*\d+\s*\)\s*", "", str(field(row, "BuyerName", "buyerName") or "")).strip()


def endpoint_rows(api_key, org_id, endpoint, start, end):
    params = {
        "organizationId": org_id, "startDate": start.strftime("%Y-%m-%dT%H:%M:%S"),
        "endDate": end.strftime("%Y-%m-%dT%H:%M:%S"), "reportTimeZone": "UTC",
        "maxItems": PAGE_SIZE, "useCursor": "true", "sortDirection": "desc",
    }
    # Filter defensively by campaign after the request too. Nothing from other campaigns is exposed or kept.
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    rows = []
    cursor_seen = set()
    deadline = monotonic() + 16
    for _ in range(MAX_PAGES):
        if monotonic() >= deadline:
            return rows, False
        response = requests.get(f"{CALLGRID_BASE_URL}/{endpoint}", params=params, headers=headers, timeout=(3, 7))
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise ValueError("invalid CallGrid report schema")
        chunk = payload["data"]
        for row in chunk:
            if not isinstance(row, dict):
                raise ValueError("invalid CallGrid row")
            ts = instant(field(row, "createdAt", "@timestamp", "date"))
            if ts is None or not field(row, "CampaignId", "campaignId"):
                raise ValueError("CallGrid returned no usable timestamps or campaign IDs")
            if campaign_ok(row) and start <= ts <= end:
                rows.append(row)
        cursor = payload.get("nextCursor") or payload.get("lastSortValues") or payload.get("searchAfter")
        if payload.get("hasMore") is False:
            return rows, True
        if not cursor:
            total = payload.get("totalCount")
            return rows, bool(len(chunk) < PAGE_SIZE and payload.get("hasMore") is not True
                              and not (isinstance(total, int) and total > len(chunk)))
        encoded = json.dumps(cursor, separators=(",", ":")) if not isinstance(cursor, str) else cursor
        if encoded in cursor_seen:
            return rows, False
        cursor_seen.add(encoded)
        params["searchAfter"] = encoded
    return rows, False


def zip_language(row):
    plan = str(field(row, "RoutingPlanName", "routingPlanName") or "").lower().strip()
    lang = "ES" if plan.startswith("spanish") else "EN" if plan.startswith("english") else ""
    z, ivr, visitor = (str(field(row, name) or "").strip() for name in ("ZipCode", "IVRZip", "VisitorZipCode"))
    if not ZIP_RE.fullmatch(z) or z == "00000" or not lang:
        return None
    # A ZIP known only by area-code estimation is not an actionable clinic match.
    if "messenger" in plan and z != ivr:
        return None
    if ("lp" in plan or "landing" in plan) and z not in (visitor, ivr):
        return None
    if "messenger" not in plan and not ("lp" in plan or "landing" in plan):
        return None
    return z, lang


def observe(pings, calls, now):
    call_map = {}
    for c in calls:
        cid = field(c, "id", "CallId", "callId")
        if cid:
            call_map[str(cid)] = c
    zip_cases = defaultdict(set)
    eligible = []
    for p in pings:
        cid, b = field(p, "CallId", "callId"), buy_key(p)
        key = zip_language(p)
        t = instant(field(p, "createdAt", "@timestamp", "date"))
        if not cid or not key or not t or not b:
            continue
        zip_cases[str(cid)].add(key)
        eligible.append((p, str(cid), b, key, t))
    # A call with competing ZIP/plan tags is ambiguous, not multiple demand.
    grouped = defaultdict(dict)
    for p, cid, b, (z, lang), t in eligible:
        if len(zip_cases[cid]) != 1:
            continue
        state = str(field(p, "StateCodeFromZipCode") or "").strip().upper()[:2]
        case = grouped[z, lang, b].setdefault(cid, {
            "state": state, "last": t, "offer": None, "connected": False, "billable": False,
            "no_bid": False, "technical": False, "in_progress": False, "connect_signal_at": None,
        })
        case["last"] = max(t, case["last"])
        code = str(field(p, "PingResponseCode") or "").lower()
        status = str(field(p, "PingCode") or "")
        if "1006" in code or "no-target" in code or (b.lower() == "ray advertising" and "4004" in (code + " " + status)):
            case["no_bid"] = True
        if "1004" in code or "429" in status or any(truth(field(p, key)) for key in ("PingTimeout", "PingApiFailed", "PingRateLimited")):
            case["technical"] = True
        # Callback/static downstream tagged accepted without a positive bid isn't a fresh RTB offer.
        if not truth(field(p, "PingAccepted")) or not positive(field(p, "PingBidAmount", "PingRevenueAmount")):
            continue
        case["offer"] = max(t, case["offer"] or t)
        c = call_map.get(cid)
        if c and truth(field(p, "PingWonCall")):
            dest = field(p, "DestinationId")
            chosen = field(c, "DestinationId", "destinationId")
            same = (dest == chosen) if dest and chosen else (
                str(field(p, "DestinationName") or "").strip() == str(field(c, "DestinationName") or "").strip()
                and bool(field(p, "DestinationName")))
            if not same:
                continue
            call_status = str(field(c, "callStatus", "CallStatus", "status")).upper()
            if call_status == "COMPLETED" and positive(field(c, "CallDurationFromConnect", "callDurationFromConnect")):
                case["connected"] = True
                case["connect_signal_at"] = max(t, case["connect_signal_at"] or t)
                case["billable"] = positive(field(c, "CallRevenue", "RevenueAmount", "revenue"))
            elif call_status == "IN_PROGRESS":
                case["in_progress"] = True
    result = []
    for (z, lang, buyer), attempts in grouped.items():
        values = list(attempts.values())
        con = sum(x["connected"] for x in values)
        offers = sum(x["offer"] is not None for x in values)
        no_bid = sum(x["no_bid"] and not x["offer"] and not x["technical"] for x in values)
        recent_offer = max((x["offer"] for x in values if x["offer"]), default=None)
        last = max(x["last"] for x in values)
        recent_connects = sum(x["connect_signal_at"] is not None and now - x["connect_signal_at"] <= timedelta(minutes=30)
                              for x in values)
        if recent_connects >= 2 and len(values) >= 3:
            status = "Señal repetida"
        elif con:
            status = "Conexión aislada"
        elif offers:
            status = "Oferta sin conexión verificada"
        elif no_bid >= 3:
            status = "Sin puja observada"
        elif any(x["technical"] for x in values):
            status = "Error o límite técnico"
        else:
            status = "Muestra insuficiente"
        result.append({
            "zip": z, "language": lang, "buyer": buyer, "status": status, "attempts": len(values),
            "connected": con, "offers": offers, "billable": sum(x["billable"] for x in values),
            "in_progress": sum(x["in_progress"] and not x["connected"] for x in values),
            "last": last.isoformat(), "offer_at": recent_offer.isoformat() if recent_offer else None,
            "latest_connected": max((x["connect_signal_at"].isoformat() for x in values if x["connect_signal_at"]), default=None),
            "state": next((x["state"] for x in values if x["state"]), ""),
        })
    return sorted(result, key=lambda r: (-r["connected"], -r["offers"], -r["attempts"], r["zip"]))


def snapshot(api_key, org_id):
    # Lock bounds upstream traffic even if several admins refresh at the same time.
    global _cache, _retry_after
    with _lock:
        now = datetime.now(timezone.utc)
        tick = monotonic()
        if _cache and tick - _cache[0] < CACHE_SECONDS:
            return _cache[1]
        if tick < _retry_after:
            return {**_cache[1], "stale": True} if _cache else {"unavailable": True}
        start = now - timedelta(minutes=120)
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                ping_f = pool.submit(endpoint_rows, api_key, org_id, "ping", start, now)
                call_f = pool.submit(endpoint_rows, api_key, org_id, "call", start, now)
                pings, p_full = ping_f.result()
                calls, c_full = call_f.result()
            data = {"unavailable": False, "stale": False, "complete": p_full and c_full,
                    "as_of": now.isoformat(), "window_minutes": 120, "rows": observe(pings, calls, now)}
        except (requests.RequestException, ValueError, TypeError):
            _retry_after = monotonic() + BACKOFF_SECONDS
            return {**_cache[1], "stale": True} if _cache else {"unavailable": True}
        _cache = (monotonic(), data)
        return data
