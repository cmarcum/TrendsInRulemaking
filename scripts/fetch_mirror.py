#!/usr/bin/env python3
"""
Fetch stage for the trend in PUBLISHED comments on RULEMAKING dockets, as far
back as the Spicy Regs mirror of regulations.gov goes. The analysis lives in
analysis/analyze_rulemaking_trends.R and the page in scripts/build_site.py.

Why "published": the number of comments *submitted* to a docket could be
inflated until GSA closed the loophole behind it, so it is not a safe basis
for a long-run comparison. The mirror (Spicy Regs / Mirrulations) holds only
comments that were actually posted on regulations.gov, so counting its rows
counts published comments.

Workflow (from the repository root; --out is the folder holding the scan, data/):
  1. python scripts/fetch_mirror.py --fetch
       writes _docket_year_scan.csv and _rulemaking_dockets.csv to data/: a
       small docket x posting-year table and the list of rulemaking dockets.
       This is the full re-pull and discards the top-ups below.
  2. top-ups, in this order (each deletes the ones after it, since they are
     computed against the one before):
       python scripts/topup_from_mirrulations.py   comments in the raw Mirrulations
                                                   bucket the mirror has not caught up on
       python scripts/topup_open_dockets.py        open and recently closed dockets,
                                                   refreshed from the live API
  3. Rscript analysis/analyze_rulemaking_trends.R
  4. python scripts/build_site.py

Setup:
  pip install -r requirements.txt
  By default the parquet files are read straight from
  https://data.spicy-regs.dev/ with HTTP range requests, so only the few
  columns used here are transferred (no 2.4 GB download). To use local
  copies instead, pass --source <folder> holding comments.parquet and
  dockets.parquet.

Stages:
  --inspect   schema, row count, freshness of the comments table
  --fetch     scan the comments and cache the raw tables (the default)
  --verify    re-check the largest dockets plus a few seeds against the live
              API (needs REGSGOV_API_KEY in the environment; never paste a key
              into this file)
  --all       --inspect, --fetch and --verify

Credits: data via Spicy Regs (github.com/civictechdc/spicy-regs) and
Mirrulations (Moravian University). Counts are posted comment records, not
comments received.
"""

import argparse
import csv
import datetime
import glob
import os
import sys
import time

try:
    import duckdb
except ImportError:
    sys.exit("pip install duckdb")

SPICY_BASE = "https://data.spicy-regs.dev/"
OUT_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "data"))
THIS_YEAR = datetime.date.today().year
MIN_YEAR = 1970           # earlier posted dates in the mirror are data-entry errors


def resolve_sources(data):
    """(comments, dockets) parquet locations: local folder, else the mirror."""
    if data and not data.startswith(("http://", "https://")):
        found = {}
        for name in ("comments", "dockets"):
            hits = sorted(glob.glob(os.path.join(data, "**", f"{name}*.parquet"),
                                    recursive=True))
            if not hits:
                sys.exit(f"No {name}*.parquet under {data}. Download from {SPICY_BASE}")
            found[name] = hits[0]
        return found["comments"], found["dockets"]
    base = (data or SPICY_BASE).rstrip("/") + "/"
    return base + "comments.parquet", base + "dockets.parquet"


def connect(*sources):
    con = duckdb.connect()
    if any(s.startswith("http") for s in sources):
        con.execute("INSTALL httpfs; LOAD httpfs;")
    return con


def q(path):
    return path.replace("'", "''")


def stage_inspect(con, comments, dockets):
    n, d = con.execute(
        f"SELECT COUNT(*), COUNT(DISTINCT docket_id) "
        f"FROM read_parquet('{q(comments)}')").fetchone()
    print(f"comment rows: {n:,}; distinct dockets with >=1 comment: {d:,}")
    lo, hi = con.execute(
        f"SELECT MIN(posted_date) FILTER (WHERE try_cast(substr(posted_date,1,4) AS INT) >= {MIN_YEAR}), "
        f"MAX(posted_date) FROM read_parquet('{q(comments)}')").fetchone()
    print(f"posted_date range (>= {MIN_YEAR}): {lo} .. {hi}")
    print("docket types:", con.execute(
        f"SELECT docket_type, COUNT(*) FROM read_parquet('{q(dockets)}') "
        "GROUP BY 1 ORDER BY 2 DESC").fetchall())


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def scan_docket_years(con, comments, dockets, cache, reuse):
    """One pass over the comments table -> a small docket x posted-year table
    for rulemaking dockets. Reads only docket_id, posted_date, receive_date."""
    import pandas as pd
    if reuse and os.path.exists(cache):
        print(f"reusing {cache}")
        return pd.read_csv(cache, dtype={"docket_id": str})
    print("scanning comments (a minute or two over the network)...")
    df = con.execute(f"""
        WITH c AS (
            SELECT docket_id,
                   try_cast(substr(posted_date, 1, 4) AS INTEGER)  AS py,
                   try_cast(substr(receive_date, 1, 4) AS INTEGER) AS ry,
                   substr(posted_date, 1, 10)  AS pd,
                   substr(receive_date, 1, 10) AS rd
            FROM read_parquet('{q(comments)}')
            WHERE docket_id IS NOT NULL
        )
        SELECT c.docket_id,
               CASE WHEN py BETWEEN {MIN_YEAR} AND {THIS_YEAR} THEN py END AS posted_year,
               COUNT(*) AS published_comments,
               MIN(pd) FILTER (WHERE py BETWEEN {MIN_YEAR} AND {THIS_YEAR}) AS first_posted,
               MAX(pd) FILTER (WHERE py BETWEEN {MIN_YEAR} AND {THIS_YEAR}) AS last_posted,
               MIN(rd) FILTER (WHERE ry BETWEEN {MIN_YEAR} AND {THIS_YEAR}) AS first_received
        FROM c
        JOIN read_parquet('{q(dockets)}') d ON d.docket_id = c.docket_id
        WHERE lower(d.docket_type) = 'rulemaking'
        GROUP BY 1, 2
    """).df()
    df.to_csv(cache, index=False)
    return df


def rulemaking_dockets(con, dockets):
    return con.execute(f"""
        SELECT docket_id, agency_code, title AS docket_title
        FROM read_parquet('{q(dockets)}')
        WHERE lower(docket_type) = 'rulemaking'
    """).df()


def stage_fetch(con, comments, dockets, out_dir, reuse):
    os.makedirs(out_dir, exist_ok=True)
    cache = os.path.join(out_dir, "_docket_year_scan.csv")
    dy = scan_docket_years(con, comments, dockets, cache, reuse)
    dk = rulemaking_dockets(con, dockets)
    dk.to_csv(os.path.join(out_dir, "_rulemaking_dockets.csv"), index=False)
    for stale in ("_topup_scan.csv", "_topup_dockets.csv", "_api_topup_scan.csv"):
        if os.path.exists(os.path.join(out_dir, stale)):
            os.remove(os.path.join(out_dir, stale))
            print(f"removed {stale} (computed against the previous base scan)")
    print(f"wrote raw tables to {out_dir}: {len(dk):,} rulemaking dockets, "
          f"{int(dy['published_comments'].sum()):,} published comments")
    print("next: scripts/topup_from_mirrulations.py and scripts/topup_open_dockets.py, then analysis/analyze_rulemaking_trends.R and scripts/build_site.py")


# ---------------------------------------------------------------------------
# Live-API check (published counts, so it still applies)
# ---------------------------------------------------------------------------

def live_count(docket_id: str, api_key: str) -> int:
    """Published-comment count for one docket via the live API.

    Primary: single request using filter[docketId] on /v4/comments —
    undocumented but verified working (returns meta.totalElements for
    the whole docket; matches GSA's documented two-step result for
    EOIR-2020-0003: 88,061). Falls back to the documented
    objectId chain if the filter is ever removed.
    """
    import requests
    H = {"X-Api-Key": api_key}
    B = "https://api.regulations.gov/v4"
    r = requests.get(f"{B}/comments", headers=H, params={
        "filter[docketId]": docket_id, "page[size]": 5})
    if r.status_code == 200:
        j = r.json()
        if j.get("meta", {}).get("filters", {}).get("docketId"):
            return j["meta"]["totalElements"]
    # Fallback: documented two-step objectId chain
    total, page = 0, 1
    obj_ids = []
    while True:
        r = requests.get(f"{B}/documents", headers=H, params={
            "filter[docketId]": docket_id,
            "page[size]": 250, "page[number]": page})
        r.raise_for_status()
        j = r.json()
        obj_ids += [d["attributes"]["objectId"] for d in j["data"]
                    if d["attributes"].get("objectId")]
        if not j["meta"].get("hasNextPage"):
            break
        page += 1
        time.sleep(0.2)
    for oid in obj_ids:
        r = requests.get(f"{B}/comments", headers=H, params={
            "filter[commentOnId]": oid, "page[size]": 5})
        r.raise_for_status()
        total += r.json()["meta"]["totalElements"]
        time.sleep(0.2)
    return total


def stage_verify(ranked_csv: str, n_top: int, seeds):
    api_key = os.environ.get("REGSGOV_API_KEY")
    if not api_key:
        sys.exit("Set REGSGOV_API_KEY to run --verify")
    with open(ranked_csv, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    ranked = {r["docket_id"]: int(r["published_comments"]) for r in rows}
    top = [r["docket_id"] for r in rows[:n_top]]
    targets = top + [s for s in seeds if s not in top]
    print(f"{'docket':<28}{'mirror':>12}{'live':>12}{'delta%':>9}")
    for d in targets:
        try:
            live = live_count(d, api_key)
        except Exception as e:
            print(f"{d:<28}  ERROR: {e}")
            continue
        mirror = ranked.get(d, 0)
        delta = 100 * (live - mirror) / live if live else 0.0
        flag = "  <-- CHECK" if abs(delta) > 2 else ""
        print(f"{d:<28}{mirror:>12,}{live:>12,}{delta:>8.1f}%{flag}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=None,
                    help="folder with comments.parquet and dockets.parquet "
                         "(default: read from data.spicy-regs.dev)")
    ap.add_argument("--out", default=OUT_DIR, help="folder for the scan (default: data/)")
    ap.add_argument("--reuse", action="store_true",
                    help="reuse the cached scan in --out instead of re-reading the data")
    ap.add_argument("--inspect", action="store_true")
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--verify-top", type=int, default=15)
    ap.add_argument("--ranked", default=os.path.join(OUT_DIR, os.pardir, "build", "rulemaking_dockets_ranked.csv"),
                    help="ranked table that --verify checks against the API")
    ap.add_argument("--seeds", nargs="*", default=[
        "OMB-2026-0034",
        "EOIR-2020-0003",
        "CEQ-2019-0003",
    ])
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args()
    if a.all:
        a.inspect = a.fetch = a.verify = True
    if not (a.inspect or a.fetch or a.verify):
        a.fetch = True

    if a.inspect or a.fetch:
        comments, dockets = resolve_sources(a.source)
        con = connect(comments, dockets)
        if a.inspect:
            stage_inspect(con, comments, dockets)
        if a.fetch:
            stage_fetch(con, comments, dockets, a.out, a.reuse)
    if a.verify:
        stage_verify(a.ranked, a.verify_top, a.seeds)


if __name__ == "__main__":
    main()
