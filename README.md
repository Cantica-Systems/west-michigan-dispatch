# West Michigan Dispatch Archive

This is an append-only public-record archive of live dispatch data from two West
Michigan sources, preserved before it disappears from the upstream feeds.

- **Kent County** — county-wide live dispatch (ArcGIS feature service)
- **Grand Rapids City** — combined GRPD + GRFD dispatch (HTML)

A GitHub Actions workflow fetches both sources on a five-minute trigger,
writes the raw upstream response bytes to `raw/`, materializes a canonical
CSV in `data/`, and commits any change. **The git history *is* the
time-series.** Running `git log -p data/kent/2026-05.csv` (or any partition
file) shows when each record first appeared and any later edits — every
change is a timestamped, immutable commit.

## Why this exists

Kent County and the City of Grand Rapids publish computer aided dispatch data through
public endpoints for the public --  a level of transparency that most agencies in Michigan
don't provide. That's not a given, it's a choice, and it matters. When
agencies encrypt their radio traffic, publishing informal data to keep the public informed is how they
maintain accountability. These departments that publish reasonably realtime data deserve
recognition for that.

But what's published is **ephemeral** and rolling windows:

- Grand Rapids' dispatch HTML file is a rolling window of roughly the last 24
  hours of calls. Anything older drops off the page; the city does not
  maintain a public archive.
- Kent County's ArcGIS endpoint is a live queue. Records flow through and
  out; the same `OBJECTID` slots get reused as new calls arrive.  Historical incidents are not accessible.

In other words, the agencies publish the data once, briefly, and then
it's gone, locked behind a FOIA request. Without a third party preserving these snapshots, today's
dispatch feed is unrecoverable tomorrow without needless administrative burden. That gap is what this repository
fills, a permanent, byte-for-byte, verifiable record of what the agencies themselves chose to publish.

## Notes on Kent County's publication

Kent's dispatch feed publishes lat/lon for some kinds of locations and not others. In `data/kent/2026-05.csv` (representative month, ~4,300 rows), the observable pattern was:

- Block-anonymized street addresses (the "X BLOCK OF Y" form representing a ~100-house range, ~81% of rows): lat/lon empty.
- Highways, intersections, and freeway-ramp rows (~4% of rows): lat/lon populated at ~6-decimal precision.
- Other rows (trails, named landmarks, businesses, ~15%): lat/lon mostly empty, occasionally populated for well-known features.

We don't know the exact rules Kent applies for which records get coords, and the specific publishing decisions are Kent's. The pattern may change at any time.

This archive relays Kent's output verbatim. No geocoding, no enrichment, no precision adjustment.

## Layout

```
data/                                     ← legible canonical CSVs, partitioned by event month
  kent/
    YYYY-MM.csv                           ← append-only, sorted, deduped
    unknown.csv                           ← rows whose event timestamp didn't parse (rare)
  grcity/
    YYYY-MM.csv
    unknown.csv

raw/                                      ← byte-for-byte upstream responses
  kent/YYYY/MM/DD/HH-MM-SSZ-{hash8}.json
  grcity/YYYY/MM/DD/HH-MM-SSZ-{pd|fd}-{hash8}.html
```

CSVs are partitioned by the **event time** of each record (not the fetch
time), so a record always lives in the file for the month it occurred.
Late-arriving rows scraped after a month boundary still land in the
correct month. To get all records as a single stream, glob the partition
files: `cat data/kent/*.csv` or your tooling's equivalent.

The 8-character suffix on each raw file is the first 8 hex chars of the
SHA-256 of the response body. If a fetch returns bytes identical to the
previous fetch, the file is skipped (no duplicate written).

## Sources

| Source | URL | Format |
|---|---|---|
| Kent County dispatch | `https://gis.kentcountymi.gov/agisprod/rest/services/Kent_County_Public_Incidents/MapServer/0/query` | ArcGIS JSON |
| GRPD dispatch | `https://data.grcity.us/Dispatch/Dispatched_Calls.html` | HTML table |
| GRFD dispatch | `https://data.grcity.us/Fire_Dispatch/Dispatched_Calls.html` | HTML table |

The CSV columns mirror what the upstream publishes — no derived fields beyond timestamp normalization to UTC. The
goal is fidelity to source, not improvement of source.

### What each source uniquely provides

**Kent County dispatch** covers all city and county agencies other than Grand Rapids City: Wyoming PD, Kentwood PD, Walker PD, East Grand Rapids PD, township
fire departments, and the Kent County Sheriff's Office. Most of these
municipalities publish nothing of their own; their dispatch activity
exists in public form *only* through the county's combined queue. When
this archive captures a Wyoming PD call, it's preserving a record that
has no other public home.

**Grand Rapids City dispatch** covers the city limits of Grand Rapids, and uniquely splits police and fire into separate published streams. That
distinction is preserved in the archive's `source` column, which lets
downstream analysis cleanly separate (for example) medical-call volume
from law-enforcement-call volume — a separation that's lost in
single-stream feeds.

### `data/kent/YYYY-MM.csv`

| column | type | source |
|---|---|---|
| `objectid` | int | `attributes.OBJECTID` (caution: this is a recyclable queue slot, not a stable id) |
| `event_time_utc` | ISO-8601 Z | `attributes.DateTime` (epoch ms) → UTC |
| `event_time_epoch_ms` | int | `attributes.DateTime` (epoch ms, unchanged) |
| `city_twp` | text | `attributes.CityorTwp` |
| `display_address` | text | `attributes.DisplayAddress` |
| `incident_type` | text | `attributes.IncidentTypeDescription` |
| `agency_type` | text | `attributes.AgencyType` |
| `agency_name` | text | `attributes.AgencyName` |
| `latitude` | float | `attributes.Latitude` |
| `longitude` | float | `attributes.Longitude` |

### `data/grcity/YYYY-MM.csv`

| column | type | source |
|---|---|---|
| `source` | `police` \| `fire` | which HTML page the row came from |
| `event_time_raw` | text | first column of the HTML table, verbatim (`"05/02/2026 09:25"`-style, no zone published) |
| `event_time_utc` | ISO-8601 Z | `event_time_raw` interpreted as `America/Detroit`, converted to UTC |
| `event_time_epoch_ms` | int | same instant as `event_time_utc`, as epoch ms |
| `incident_type` | text | second column of the HTML table |
| `location` | text | third column of the HTML table |

The `_raw` column preserves byte-for-byte what the upstream prints; `_utc`
and `_epoch_ms` are derived under the assumption that the source publishes
in `America/Detroit` (which it does in practice — confirmed against
incident times). If the upstream ever changes that assumption, the raw
column survives and consumers can re-parse.

## License

[CC0 1.0 Universal](LICENSE) — public domain dedication. Use it for
anything; no attribution required (though kindly appreciated).

The underlying records are themselves public information published by
public agencies. Putting the archive under CC0 makes that explicit:
nobody owns this data, and nobody should need permission to study,
republish, or build on it.

## Caveats

- **Kent's `OBJECTID` is a queue slot, not a stable id.** Kent's ArcGIS
  endpoint reuses ~20 OBJECTID values as records flow through their
  dispatch queue. The `(event_time_epoch_ms, incident_type, agency_name,
  display_address)` tuple is the practical primary key for joining
  across snapshots; we keep `objectid` because it's what the upstream
  publishes, but don't trust it for identity.
- **GR City's HTML table is a rolling window.** The page only shows the
  most recent ~24 hours of calls. Anything older drops off the source;
  this archive is the only persisted record going forward.
- **Nature-of-call narratives are not published upstream.** Both feeds
  publish a categorical incident type (e.g. `THEFT`, `ASSAULT`,
  `MEDICAL`) but not the dispatcher's free-text description. There is
  no way to recover narrative detail from these sources.
- **Address anonymization.** Most rows are anonymized to a 100-block
  (e.g. `100 BLOCK MAIN ST`) by the source agency before publication.
  All same-block rows therefore share a single coordinate and cannot
  be resolved to specific addresses. This is upstream behavior, not
  imposed by this archive.
- **Kent's endpoint has recurring overnight outages.** Kent County's
  ArcGIS endpoint periodically returns errors or stops responding
  overnight; during those windows no Kent records are captured and the
  archive has a gap. Collection resumes automatically once the endpoint
  recovers. The outage is on Kent's side and outside our control.
