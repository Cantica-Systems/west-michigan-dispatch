#!/usr/bin/env python3
"""Fetch the City of Grand Rapids' public dispatch HTML pages (police +
fire), persist raw response bytes under raw/, and append normalized rows
into per-month CSVs at data/grcity/YYYY-MM.csv (partitioned on event
time, not fetch time).
"""

import csv
import hashlib
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

SOURCES = [
    ("police", "https://data.grcity.us/Dispatch/Dispatched_Calls.html"),
    ("fire",   "https://data.grcity.us/Fire_Dispatch/Dispatched_Calls.html"),
]
SOURCE_TZ = ZoneInfo("America/Detroit")
# Contact point is the site, not a personal address: this repo is public.
USER_AGENT = "CanticaBlotter/1.0 (west michigan dispatch archive; cantica.dev)"

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "raw" / "grcity"
DATA_DIR = ROOT / "data" / "grcity"
CSV_COLUMNS = [
    "source", "event_time_raw", "event_time_utc", "event_time_epoch_ms",
    "incident_type", "location",
]


def fetch(session, url):
    resp = session.get(url, timeout=15)
    resp.raise_for_status()
    return resp.content


def find_incident_table(soup):
    """The HTML has multiple tables; the right one's header row contains
    'date', 'incident', and 'location' tokens."""
    expected = ("date", "incident", "location")
    for table in soup.find_all("table"):
        header_row = table.find("tr")
        if header_row is None:
            continue
        cells = header_row.find_all(["th", "td"])
        if len(cells) < 3:
            continue
        text = " ".join(c.get_text(" ", strip=True).lower() for c in cells)
        if all(token in text for token in expected):
            return table
    return None


def parse_table(table):
    rows = []
    for tr in table.find_all("tr"):
        cols = tr.find_all("td")
        if len(cols) == 3:
            rows.append((
                cols[0].get_text(strip=True),
                cols[1].get_text(strip=True),
                cols[2].get_text(strip=True),
            ))
    return rows


def normalize_event_time(raw):
    """Source publishes 'MM/DD/YYYY HH:MM' in America/Detroit. Returns
    (utc_iso_z, epoch_ms) or ('', None) on parse failure."""
    if not raw:
        return "", None
    raw = raw.strip()
    for fmt in ("%m/%d/%Y %H:%M", "%m/%d/%Y %H:%M:%S"):
        try:
            local_dt = datetime.strptime(raw, fmt).replace(tzinfo=SOURCE_TZ)
            utc_dt = local_dt.astimezone(timezone.utc)
            iso = utc_dt.isoformat().replace("+00:00", "Z")
            return iso, int(utc_dt.timestamp() * 1000)
        except ValueError:
            continue
    return "", None


def _most_recent_raw_hash(source):
    """Return the 8-char hash of the most recent raw file for this source, or None."""
    candidates = sorted(RAW_DIR.rglob(f"*-{source[:2]}-*.html"))
    if not candidates:
        return None
    return candidates[-1].stem.rsplit("-", 1)[-1]


def save_raw(now_utc, source, body):
    """Write to raw/grcity/YYYY/MM/DD/HH-MM-SSZ-{pd|fd}-{hash8}.html.
    Skip if hash matches the most recent raw file for this source."""
    code = {"police": "pd", "fire": "fd"}[source]
    h = hashlib.sha256(body).hexdigest()[:8]
    if _most_recent_raw_hash(code) == h:
        return None
    day_dir = RAW_DIR / now_utc.strftime("%Y/%m/%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / f"{now_utc.strftime('%H-%M-%SZ')}-{code}-{h}.html"
    path.write_bytes(body)
    return path


def _partition_for(epoch_ms_str):
    """Bucket a row's event time into a YYYY-MM partition. Rows whose
    timestamp didn't parse go to 'unknown' so the raw text is still preserved."""
    if not epoch_ms_str:
        return "unknown"
    try:
        epoch_ms = int(epoch_ms_str)
    except (ValueError, TypeError):
        return "unknown"
    return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).strftime("%Y-%m")


def _row_key(row):
    return (
        row.get("source") or "",
        str(row.get("event_time_epoch_ms") or ""),
        row.get("incident_type") or "",
        row.get("location") or "",
    )


def _sort_key(row):
    return (
        int(row["event_time_epoch_ms"]) if str(row.get("event_time_epoch_ms") or "").lstrip("-").isdigit() else 0,
        str(row.get("source") or ""),
        str(row.get("location") or ""),
    )


def merge_csv(new_rows):
    """Append-only merge into per-month CSVs under data/grcity/. Partitions
    by event time so a record always lands in the month it occurred."""
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
    session = requests.Session()
    # Identify ourselves. Setting this to None strips the header outright,
    # making every request from this workflow anonymous and scraper-shaped.
    # See fetch_kent.py for the full reasoning; same policy both feeds.
    session.headers["User-Agent"] = USER_AGENT

    all_rows = []
    raw_count = 0
    for source, url in SOURCES:
        try:
            body = fetch(session, url)
        except requests.RequestException as e:
            print(f"grcity[{source}]: network error -- {e}", file=sys.stderr)
            continue
        soup = BeautifulSoup(body, "html.parser")
        table = find_incident_table(soup)
        if table is None:
            print(f"grcity[{source}]: incident table not found in HTML", file=sys.stderr)
            return 1
        if save_raw(now_utc, source, body):
            raw_count += 1
        for date_time, incident_type, location in parse_table(table):
            iso, epoch_ms = normalize_event_time(date_time)
            all_rows.append({
                "source": source,
                "event_time_raw": date_time,
                "event_time_utc": iso,
                "event_time_epoch_ms": epoch_ms,
                "incident_type": incident_type,
                "location": location,
            })

    added = merge_csv(all_rows)
    raw_msg = f"raw {raw_count} file(s)" if raw_count else "raw unchanged"
    print(f"grcity: {len(all_rows)} row(s), +{added} new CSV row(s), {raw_msg}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
