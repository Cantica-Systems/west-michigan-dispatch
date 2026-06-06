#!/usr/bin/env python3
"""Fetch Kent County's public dispatch feed (ArcGIS), persist raw response
bytes under raw/, and append normalized rows into per-month CSVs at
data/kent/YYYY-MM.csv (partitioned on event time, not fetch time).
"""

import csv
import hashlib
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

API_URL = (
    "https://gis.kentcountymi.gov/agisprod/rest/services/"
    "Kent_County_Public_Incidents/MapServer/0/query"
)
QUERY_PARAMS = {
    "f": "json",
    "where": "1=1",
    "outFields": "*",
    "returnGeometry": "false",
    "orderByFields": "DateTime ASC",
    "resultRecordCount": "500",
}
PAGE_SIZE = int(QUERY_PARAMS["resultRecordCount"])
PAGINATION_DELAY_S = 2

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "raw" / "kent"
DATA_DIR = ROOT / "data" / "kent"
CSV_COLUMNS = [
    "objectid", "event_time_utc", "event_time_epoch_ms",
    "city_twp", "display_address", "incident_type",
    "agency_type", "agency_name", "latitude", "longitude",
]


def fetch_pages():
    """Paginate ArcGIS query until exhausted. Returns list of (raw_bytes, parsed_features) per page."""
    session = requests.Session()
    session.headers["User-Agent"] = None
    pages = []
    offset = 0
    while True:
        params = dict(QUERY_PARAMS)
        params["resultOffset"] = str(offset)
        resp = session.get(API_URL, params=params, timeout=30)
        resp.raise_for_status()
        if "json" not in resp.headers.get("Content-Type", "").lower():
            raise SystemExit("Kent ArcGIS returned non-JSON content type")
        data = resp.json()
        # ArcGIS surfaces errors in two envelope shapes; both must be caught so
        # an error response isn't mistaken for an empty result and its body isn't
        # archived as a raw snapshot:
        #   {"error": {...}}
        #   {"status": "error", "messages": [...]}
        err = data.get("error") or (
            data.get("messages") if data.get("status") == "error" else None
        )
        if err is not None:
            print(f"kent: upstream error envelope, skipping -- {str(err)[:200]}",
                  file=sys.stderr)
            break
        features = data.get("features", [])
        pages.append((resp.content, features))
        if not features:
            break
        exceeded = bool(data.get("exceededTransferLimit"))
        if not exceeded and len(features) < PAGE_SIZE:
            break
        offset += len(features)
        time.sleep(PAGINATION_DELAY_S)
    return pages


def _most_recent_raw_hash():
    """Return the 8-char hash suffix of the most recent raw file, or None if empty."""
    candidates = sorted(RAW_DIR.rglob("*.json"))
    if not candidates:
        return None
    return candidates[-1].stem.rsplit("-", 1)[-1]


def save_raw(now_utc, pages):
    """Write each page as raw/kent/YYYY/MM/DD/HH-MM-SSZ[-pN]-{hash8}.json.

    Single-page case: skip if hash matches the most recent existing file
    (avoids a wall of identical snapshots when upstream doesn't change).
    Multi-page case: always write all pages with -p{N} suffix.
    """
    if not pages:
        return []
    day_dir = RAW_DIR / now_utc.strftime("%Y/%m/%d")
    written = []
    if len(pages) == 1:
        body, _ = pages[0]
        h = hashlib.sha256(body).hexdigest()[:8]
        if _most_recent_raw_hash() == h:
            return []
        day_dir.mkdir(parents=True, exist_ok=True)
        path = day_dir / f"{now_utc.strftime('%H-%M-%SZ')}-{h}.json"
        path.write_bytes(body)
        written.append(path)
    else:
        day_dir.mkdir(parents=True, exist_ok=True)
        for i, (body, _) in enumerate(pages, start=1):
            h = hashlib.sha256(body).hexdigest()[:8]
            path = day_dir / f"{now_utc.strftime('%H-%M-%SZ')}-p{i}-{h}.json"
            path.write_bytes(body)
            written.append(path)
    return written


def normalize_feature(feature):
    a = feature.get("attributes", {})
    epoch_ms = a.get("DateTime")
    utc_iso = ""
    if epoch_ms is not None:
        utc_iso = (
            datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
    return {
        "objectid": a.get("OBJECTID"),
        "event_time_utc": utc_iso,
        "event_time_epoch_ms": epoch_ms,
        "city_twp": a.get("CityorTwp"),
        "display_address": a.get("DisplayAddress"),
        "incident_type": a.get("IncidentTypeDescription"),
        "agency_type": a.get("AgencyType"),
        "agency_name": a.get("AgencyName"),
        "latitude": a.get("Latitude"),
        "longitude": a.get("Longitude"),
    }


def _partition_for(epoch_ms_str):
    """Bucket a row's event time into a YYYY-MM partition. Rows whose
    event time we couldn't parse go to 'unknown' so they're still preserved."""
    if not epoch_ms_str:
        return "unknown"
    try:
        epoch_ms = int(epoch_ms_str)
    except (ValueError, TypeError):
        return "unknown"
    return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).strftime("%Y-%m")


def _row_key(row):
    """Logical identity for dedup. OBJECTID is a recyclable queue slot --
    never use it for identity."""
    return (
        str(row.get("event_time_epoch_ms") or ""),
        row.get("incident_type") or "",
        row.get("agency_name") or "",
        row.get("display_address") or "",
    )


def _sort_key(row):
    return (
        int(row["event_time_epoch_ms"]) if str(row.get("event_time_epoch_ms") or "").lstrip("-").isdigit() else 0,
        str(row.get("agency_name") or ""),
        str(row.get("display_address") or ""),
    )


def merge_csv(new_rows):
    """Append-only merge into per-month CSVs under data/kent/. Partitions
    by event time so a record always lands in the month it occurred,
    regardless of when we scraped it."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    by_partition = {}
    for csv_file in DATA_DIR.glob("*.csv"):
        partition = csv_file.stem
        bucket = by_partition.setdefault(partition, {})
        with csv_file.open(newline="") as f:
            for row in csv.DictReader(f):
                bucket[_row_key(row)] = row
    touched = set()
    added = 0
    for row in new_rows:
        partition = _partition_for(str(row.get("event_time_epoch_ms") or ""))
        bucket = by_partition.setdefault(partition, {})
        key = _row_key(row)
        if key not in bucket:
            bucket[key] = row
            touched.add(partition)
            added += 1
    for partition in touched:
        sorted_rows = sorted(by_partition[partition].values(), key=_sort_key)
        out_path = DATA_DIR / f"{partition}.csv"
        tmp = out_path.with_suffix(".csv.tmp")
        with tmp.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            for r in sorted_rows:
                writer.writerow({k: ("" if r.get(k) is None else r[k]) for k in CSV_COLUMNS})
        tmp.replace(out_path)
    return added


def main():
    now_utc = datetime.now(timezone.utc)
    try:
        pages = fetch_pages()
    except requests.RequestException as e:
        print(f"kent: network error -- {e}", file=sys.stderr)
        return 0
    total_features = sum(len(p[1]) for p in pages)
    raw_paths = save_raw(now_utc, pages)
    rows = [normalize_feature(f) for page in pages for f in page[1]]
    added = merge_csv(rows)
    raw_msg = f"raw {len(raw_paths)} file(s)" if raw_paths else "raw unchanged"
    print(f"kent: {total_features} feature(s), +{added} new CSV row(s), {raw_msg}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
