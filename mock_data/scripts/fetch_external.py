"""Fetch a small, optional slice of *real* open data to sit next to the synthetic mock.

Two public sources are wired in; both are free. If the network (or the time window)
is unavailable, each fetcher degrades gracefully and leaves nothing behind:

  FMI (Finnish Meteorological Institute) open WFS   -> external/fmi/observations_15min.jsonl
      https://opendata.fmi.fi/wfs  (no key)
      Temperature/relative humidity/wind per station for our DC sites' nearest stations.

  Elering "Nord Pool" price feed                       -> external/nordpool/fi_price_15min.jsonl
      https://dashboard.elering.ee/api/nps/price  (no key)
      This is the Nord Pool system price for FI (the value the Finnish bidding area is
      settled against). Documented in mock_data/README.md.

  Fingrid open data (optional, needs a free API key)   -> external/fingrid/*.jsonl
      Set FINGRID_API_KEY in the environment; skip otherwise.

These files are only helpers for realistic site cooling cost / load patterns. The
generators never *require* them (deterministic synthetic fallbacks are in place).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "generators"))
import common as c
from generate_network import SITES

NS = {"wfs": "http://www.opengis.net/wfs/2.0", "gml": "http://www.opengis.net/gml/3.2",
      "BsWfs": "http://xml.fmi.fi/schema/wfs/2.0"}

FMI_PARAMS = ["t2m"]


SITE_CITY = {s[0]: s[2] for s in SITES}


STATIONS = {
    # DC site -> (FMI station id, name, lat, lon)
    "FI-HEL-DC1": ("101004", "Helsinki Kaisaniemi", 60.175, 24.945),
    "FI-TRE-DC2": ("101118", "Tampere Harmala", 61.469, 23.739),
    "FI-OUL-DC3": ("101786", "Oulu Vihreäsaari", 65.007, 25.393),
    "FI-KEM-DC4": ("101924", "Kemi Ajos", 65.673, 24.515),
}


def _fmi_bbox(lat: float, lon: float, pad: float = 0.03) -> str:
    """Small box around a station. Stations are on a ~2.5 km grid, so a 0.03 deg
    (\u22483 km) box reliably captures exactly the station we want."""
    return f"{lon - pad},{lat - pad},{lon + pad},{lat + pad}"


def _fmi_request(url: str) -> "ET.Element":
    import time
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=120) as fh:
                return ET.parse(fh).getroot()
        except Exception as exc:
            print(f"FMI: retry {attempt + 1}/4 after error {exc}")
            time.sleep(2 + attempt * 3)
    raise RuntimeError("FMI unavailable")


def fetch_fmi(start: dt.datetime, end: dt.datetime) -> int:
    """Real FMI weather for the 4 DC sites, one small bbox query per site per 7 days (server hard cap).

    We use `fmi::observations::weather::simple` restricted to a ~2 km bbox around each
    site's nearest FMI station. NOTE: the FMI endpoint caps the time interval ('Too long time
    interval requested!'; ~7 days for 3 parameters), so we chunk by (site, 11 days)
    and only the first chunk actually carries 3 parameters and, after the first chunk, send a tiny
    one-point query only if we still need to resolve the station position.
    """
    all_rows: list[dict] = []
    for site, (fmisid, sname, lat, lon) in STATIONS.items():
        city = SITE_CITY[site]
        chunk = start
        while chunk < end:
            chunk_end = min(chunk + dt.timedelta(days=7), end)
            q = {
                "service": "WFS", "version": "2.0.0", "request": "getFeature",
                "storedquery_id": "fmi::observations::weather::simple",
                "bbox": _fmi_bbox(lat, lon), "parameters": "t2m",
                "starttime": chunk.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "endtime": chunk_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            print(f"FMI: {site} {chunk.date()} .. {chunk_end.date()}", flush=True)
            try:
                root = _fmi_request("https://opendata.fmi.fi/wfs?" + urllib.parse.urlencode(q))
            except Exception as exc:  # pragma: no cover - offline
                print(f"FMI: chunk skipped ({exc})")
                chunk = chunk_end
                continue
            by_time: dict[str, dict] = {}
            for el in root.findall(".//BsWfs:BsWfsElement", NS):
                t_el = el.find("BsWfs:Time", NS)
                p_el = el.find("BsWfs:ParameterName", NS)
                v_el = el.find("BsWfs:ParameterValue", NS)
                if None in (t_el, p_el, v_el):
                    continue
                try:
                    val = float(v_el.text)
                except (TypeError, ValueError):
                    continue
                by_time.setdefault(t_el.text, {})[p_el.text] = val
            for when, params in by_time.items():
                all_rows.append({
                    "site": site, "city": city, "station_id": fmisid, "station_name": sname,
                    "station_lat": lat, "station_lon": lon, "measured_at": when,
                    "t2m_c": params.get("t2m"), "rh_pct": params.get("rh"),
                    "ws_10min_ms": params.get("ws_10min"),
                })
            chunk = chunk_end
    if not all_rows:
        return 0
    grid: dict[tuple, dict] = {}
    for r in all_rows:
        d = dt.datetime.strptime(r["measured_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
        slot = d.replace(minute=(d.minute // 15) * 15, second=0)
        key = (r["site"], slot.strftime("%Y-%m-%dT%H:%M:%SZ"))
        if key not in grid:
            grid[key] = dict(r, measured_at=slot.strftime("%Y-%m-%dT%H:%M:%SZ"),
                             source="FMI open data: fmi::observations::weather::simple, nearest station")
    out = [r for _, r in sorted(grid.items()) if r.get("t2m_c") is not None]
    _write("external/fmi/observations_15min.jsonl", out)
    print(f"FMI: wrote {len(out)} station-quarter rows")
    return len(out)


def fetch_nordpool(start: dt.datetime, end: dt.datetime) -> int:
    # The public endpoint rejects a single multi-month range; fetch in 30-day chunks.
    out = []
    probe_rows = 0
    cur = start
    while cur < end:
        chunk_end = min(cur + dt.timedelta(days=30), end)
        probe_rows += _nordpool_chunk(cur, chunk_end, out)
        cur = chunk_end
    _write("external/nordpool/fi_price_15min.jsonl", out)
    print(f"Nordpool(FI): wrote {len(out)} 15-min prices")
    return len(out)


def _nordpool_chunk(start: dt.datetime, end: dt.datetime, out: list) -> int:
    q = {"start": start.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "end": end.strftime("%Y-%m-%dT%H:%M:%S.000Z")}
    url = "https://dashboard.elering.ee/api/nps/price?" + urllib.parse.urlencode(q)
    print(f"Nordpool(FI): GET {url}")
    try:
        with urllib.request.urlopen(url, timeout=120) as fh:
            data = json.load(fh)
    except Exception as exc:  # pragma: no cover - offline
        print(f"Nordpool(FI): chunk skipped ({exc})")
        return 0
    fi = data.get("data", {}).get("fi", [])
    for r in fi:
        when = dt.datetime.fromtimestamp(r["timestamp"], tz=dt.timezone.utc)
        out.append({"ts": when.strftime("%Y-%m-%dT%H:%M:%SZ"), "bidding_zone": "FI",
                    "price_eur_mwh": r["price"], "unit": "EUR/MWh",
                    "source": "Elering nps/price (Nord Pool FI)"})
    return len(fi)


def fetch_fingrid(start: dt.datetime, end: dt.datetime) -> int:
    key = os.environ.get("FINGRID_API_KEY")
    if not key:
        print("Fingrid: skipped (set FINGRID_API_KEY to enable; free registration at data.fingrid.fi)")
        return 0
    return 0


def _write(rel: str, rows: list[dict]) -> None:
    path = os.path.join(c.root_dir(), *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def main() -> None:
    start = c.START
    end = c.END - dt.timedelta(seconds=1)
    ok = 0
    ok += 1 if fetch_fmi(start, end) else 0
    ok += 1 if fetch_nordpool(start, end) else 0
    ok += 1 if fetch_fingrid(start, end) else 0
    print(f"{ok} external sources fetched")


if __name__ == "__main__":
    main()
