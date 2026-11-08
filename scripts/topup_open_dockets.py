#!/usr/bin/env python3
"""
Mark rulemaking dockets that are open for comment and refresh their counts
from the live regulations.gov API, along with the dockets that closed since the
last run and any rulemaking dockets that opened since.

An open docket gains comments every day, so the mirror and even the raw
Mirrulations bucket (see topup_from_mirrulations.py) can be a day or more
behind. A docket that closes can still be missing its last comments, so
dockets whose comment period ended after the previous run (--since, by default
the api_checked_on date in the previous _open_dockets.csv) are refreshed once
more and then dropped from the open list. Dockets that are neither open nor
recently closed are not touched, and their earlier refresh in
_api_topup_scan.csv is carried forward. For these dockets, this script:

  1. asks the API for the docket's total number of PUBLISHED comments
     (GET /v4/comments?filter[docketId]=..., meta.totalElements) and adds the
     difference to what the scan already holds. Rankings, bars and every
     figure stay on this published count.
  2. asks the API for the number of comments RECEIVED on each document that is
     open for comment (GET /v4/document-comments-received-counts/{documentId})
     and sums them per docket. This is the figure regulations.gov shows on an
     open docket. It includes comments the agency has not posted yet, so it is
     only displayed next to the published count and is never added to it.

Which dockets are open: a document of a rulemaking docket whose comment period
has started and ends in the future, read from the mirror's documents table
(refreshed daily, no API key needed). Comment end dates more than
--max-open-days away (placeholders such as 2099) are ignored. Withdrawn
documents are ignored.

Dockets the mirror does not list as rulemaking yet (because it is a day or
more behind) are looked up in the mirror's dockets table and, if still absent
and holding a proposed or final rule document, in the API; those that are
Rulemaking are appended to _topup_dockets.csv. Dockets whose ID contains FRDOC
or whose title mentions an information collection are skipped, as
analysis/analyze_rulemaking_trends.R drops them anyway.

Outputs (in --out):
  _open_dockets.csv      docket_id, comment_end_date (Eastern, last open
                         document), comments_received, api_checked_on, for the
                         dockets open now. Used by the interactive pages.
                         Written even without a key (the last two columns are
                         then empty).
  _api_topup_scan.csv    one row per docket whose live published total exceeds
                         the scan, in the layout of _docket_year_scan.csv,
                         posted_year = the year of the check (the per-docket
                         total is exact, the year of those comments is an
                         assumption). analysis/analyze_rulemaking_trends.R adds these to
                         the scan. Rows of dockets not checked in this run are
                         kept.
  _topup_dockets.csv     new rulemaking dockets found here are added to it.

The API key is read from the environment (default REGSGOV_API_KEY, from
https://api.data.gov/signup/); never put it in this file. Calls to the API
need a key even where the website does not show one. The hourly limit (1,000
requests for a registered key) is respected: on a 429 the script waits and
retries, then stops and keeps what it finished. About 2 to 3 requests are made
per docket.

Workflow (run after --fetch and topup_from_mirrulations.py, which each delete
this top-up because it is computed against their output):
  python scripts/topup_open_dockets.py
  Rscript analysis/analyze_rulemaking_trends.R
  python scripts/build_site.py

Setup: pip install duckdb pandas
"""

import argparse
import concurrent.futures as cf
import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    import duckdb
    import pandas as pd
except ImportError:
    sys.exit("pip install duckdb pandas")

API = "https://api.regulations.gov/v4/"
SPICY_BASE = "https://data.spicy-regs.dev/"
OUT_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "data"))
SCAN_COLUMNS = ["docket_id", "posted_year", "published_comments", "first_posted", "last_posted", "first_received"]


def eastern_date(utc_stamp):
    """Comment periods end at the close of an Eastern day, e.g. 2026-11-10T04:59:59Z."""
    t = datetime.datetime.fromisoformat(utc_stamp.replace("Z", "+00:00"))
    return (t - datetime.timedelta(hours=6)).date().isoformat()


def stamp(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def window_documents(con, documents_pq, since, now, max_days):
    """Documents whose comment period has started and ends after `since` (open now or closed since)."""
    cap = now + datetime.timedelta(days=max_days)
    return con.execute(f"""
        SELECT coalesce(docket_id, regexp_replace(document_id, '-[0-9]+$', '')) AS docket_id,
               document_id, comment_end_date AS end_utc, document_type
        FROM read_parquet('{documents_pq}')
        WHERE comment_end_date > '{stamp(since)}' AND comment_end_date <= '{stamp(cap)}'
          AND (comment_start_date IS NULL OR comment_start_date <= '{stamp(now)}')
          AND coalesce(lower(withdrawn), 'false') <> 'true'
    """).df()


def excluded(docket_id, title):
    return "frdoc" in docket_id.lower() or bool(re.search(r"information\s+collection", title or "", re.I))


def find_new_rulemaking(con, dockets_pq, docs, known, key):
    """Rulemaking dockets in `docs` that the dockets list does not have yet: (docket_id, agency_code, docket_title)."""
    unknown = sorted(set(docs["docket_id"]) - known)
    if not unknown:
        return pd.DataFrame(columns=["docket_id", "agency_code", "docket_title"])
    listing = ",".join("'" + d.replace("'", "''") + "'" for d in unknown)
    table = con.execute(f"""
        SELECT docket_id, agency_code, title AS docket_title, docket_type
        FROM read_parquet('{dockets_pq}') WHERE docket_id IN ({listing})
    """).df()
    found = table[table["docket_type"] == "Rulemaking"].drop(columns="docket_type")
    absent = set(unknown) - set(table["docket_id"])
    ruled = set(docs.loc[docs["document_type"].isin(["Proposed Rule", "Rule"]), "docket_id"])
    asked = sorted(absent & ruled) if key else []
    rows = []
    for d in asked:
        j = api_get("dockets/" + urllib.parse.quote(d), None, key)
        attr = ((j or {}).get("data") or {}).get("attributes") or {}
        if attr.get("docketType") == "Rulemaking":
            rows.append({"docket_id": d, "agency_code": attr.get("agencyId") or d.split("-")[0],
                         "docket_title": attr.get("title")})
    print(f"{len(unknown):,} dockets in the window are not on the rulemaking list: {len(found):,} Rulemaking in the "
          f"mirror, {len(absent):,} not in its dockets table yet ({len(asked):,} with a rule document asked of the "
          f"API, {len(rows):,} Rulemaking)")
    return pd.concat([found, pd.DataFrame(rows, columns=found.columns)], ignore_index=True)


def api_get(path, params, key, retries=4):
    """Parsed JSON, or None if the API has nothing usable for this request."""
    url = API + path + (("?" + urllib.parse.urlencode(params)) if params else "")
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={"X-Api-Key": key})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(int(e.headers.get("Retry-After") or 0) or 120)
                continue
            if e.code in (401, 403):
                sys.exit(f"the API rejected the key (HTTP {e.code})")
            if e.code == 404:
                return None
        except Exception:
            pass
        time.sleep(2 * (attempt + 1))
    return None


def published_total(docket, key):
    j = api_get("comments", {"filter[docketId]": docket, "page[size]": 5}, key)
    meta = (j or {}).get("meta", {})
    if not meta.get("filters", {}).get("docketId"):
        return None
    return int(meta["totalElements"])


def received_count(document_id, key):
    j = api_get(f"document-comments-received-counts/{urllib.parse.quote(document_id)}", None, key)
    try:
        return int(j["data"]["attributes"]["count"])
    except (TypeError, KeyError, ValueError):
        return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=OUT_DIR)
    ap.add_argument("--data", default=SPICY_BASE, help="base URL or folder of the parquet files")
    ap.add_argument("--key-env", default="REGSGOV_API_KEY", help="environment variable holding the API key")
    ap.add_argument("--max-open-days", type=int, default=366,
                    help="ignore comment end dates further away than this (placeholder dates)")
    ap.add_argument("--since", help="YYYY-MM-DD: refresh dockets whose comment period ended after this date "
                                    "(default: the api_checked_on date of the previous _open_dockets.csv)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0, help="only query this many dockets (testing)")
    a = ap.parse_args()

    now = datetime.datetime.now(datetime.timezone.utc)
    today = now.date().isoformat()
    base = a.data.rstrip("/") + "/"
    stale = os.path.join(a.out, "_api_topup_scan.csv")
    listed = os.path.join(a.out, "_open_dockets.csv")
    extra = os.path.join(a.out, "_topup_dockets.csv")
    key = os.environ.get(a.key_env)

    previous = pd.read_csv(listed, dtype=str) if os.path.exists(listed) else pd.DataFrame(columns=["docket_id"])
    checked_before = previous.get("api_checked_on", pd.Series(dtype=str)).dropna()
    since_s = a.since or (checked_before.max() if len(checked_before) else today)
    since = datetime.datetime.fromisoformat(since_s).replace(tzinfo=datetime.timezone.utc)

    listing = pd.read_csv(os.path.join(a.out, "_rulemaking_dockets.csv"), dtype=str)
    if os.path.exists(extra):
        listing = pd.concat([listing, pd.read_csv(extra, dtype=str)], ignore_index=True)
    titles = dict(zip(listing["docket_id"], listing["docket_title"]))

    con = duckdb.connect()
    if base.startswith("http"):
        con.execute("INSTALL httpfs; LOAD httpfs;")
    window = window_documents(con, base + "documents.parquet", since, now, a.max_open_days)
    new = find_new_rulemaking(con, base + "dockets.parquet", window, set(titles), key)
    if len(new):
        pd.concat([pd.read_csv(extra, dtype=str) if os.path.exists(extra) else None, new],
                  ignore_index=True).drop_duplicates("docket_id").to_csv(extra, index=False)
        titles.update(zip(new["docket_id"], new["docket_title"]))
    docs = window[window["docket_id"].isin(titles)].copy()
    skip = {d for d in set(docs["docket_id"]) | set(previous["docket_id"]) if excluded(d, titles.get(d))}
    docs = docs[~docs["docket_id"].isin(skip)]
    open_docs = docs[docs["end_utc"] > stamp(now)]
    opened = open_docs.groupby("docket_id")["end_utc"].max().map(eastern_date).rename("comment_end_date").reset_index()
    opened["comments_received"] = pd.NA
    opened["api_checked_on"] = ""
    closed = sorted(set(docs["docket_id"]) - set(opened["docket_id"]))
    print(f"{len(opened):,} rulemaking dockets are open for comment and {len(closed):,} closed since {since_s} "
          f"({len(new):,} new to the list, {len(skip):,} skipped as FRDOC or information collection)")

    if not key:
        opened.to_csv(listed, index=False)
        print(f"{a.key_env} is not set: wrote _open_dockets.csv without refreshing counts from the API")
        return

    totals = pd.read_csv(os.path.join(a.out, "_docket_year_scan.csv"), dtype={"docket_id": str})
    topup = os.path.join(a.out, "_topup_scan.csv")
    if os.path.exists(topup):
        totals = pd.concat([totals, pd.read_csv(topup, dtype={"docket_id": str})])
    have = totals.groupby("docket_id")["published_comments"].sum()

    targets = sorted((set(opened["docket_id"]) | set(closed) | set(previous["docket_id"])) - skip)
    if a.limit:
        targets = targets[:a.limit]
    target_docs = open_docs[open_docs["docket_id"].isin(targets)]
    print(f"asking the API for {len(targets):,} dockets and {len(target_docs):,} open documents")
    with cf.ThreadPoolExecutor(a.workers) as pool:
        live = dict(zip(targets, pool.map(lambda d: published_total(d, key), targets)))
        got = dict(zip(target_docs["document_id"], pool.map(lambda d: received_count(d, key),
                                                           target_docs["document_id"])))

    rows, ahead, failed, checked = [], [], [], set()
    for docket in targets:
        total = live[docket]
        if total is None:
            failed.append(docket)
            continue
        checked.add(docket)
        known = int(have.get(docket, 0))
        if total > known:
            rows.append({"docket_id": docket, "posted_year": now.year, "published_comments": total - known,
                         "first_posted": None, "last_posted": None, "first_received": None})
        elif total < known:
            ahead.append((docket, known, total))

    received, partial = {}, []
    for docket, group in target_docs.groupby("docket_id"):
        counts = [got[d] for d in group["document_id"]]
        if any(c is None for c in counts):
            partial.append(docket)
        else:
            received[docket] = sum(counts)

    carried = pd.read_csv(stale, dtype={"docket_id": str}) if os.path.exists(stale) else pd.DataFrame(columns=SCAN_COLUMNS)
    carried = carried[~carried["docket_id"].isin(checked)]
    fresh = pd.DataFrame(rows, columns=SCAN_COLUMNS)
    pd.concat([carried, fresh], ignore_index=True)[SCAN_COLUMNS].astype({"posted_year": "Int64"}).to_csv(stale, index=False)
    opened["comments_received"] = opened["docket_id"].map(received).astype("Int64")
    opened.loc[opened["docket_id"].isin(checked), "api_checked_on"] = today
    opened.to_csv(listed, index=False)

    added = sum(r["published_comments"] for r in rows)
    print(f"published: checked {len(checked):,} dockets, {len(rows):,} gained {added:,} comments over the scan")
    for r in sorted(rows, key=lambda r: -r["published_comments"])[:8]:
        print(f"  {r['docket_id']:<26}{r['published_comments']:>8,}")
    both = [d for d in received if d in checked]
    if both:
        pub = sum(live[d] for d in both)
        print(f"received: {len(received):,} dockets, {sum(received[d] for d in both):,} received vs "
              f"{pub:,} published across the {len(both):,} that have both")
    if ahead:
        print(f"{len(ahead)} dockets have more comments in the scan than the API reports (left as is):")
        for d, k, l in sorted(ahead, key=lambda x: x[2] - x[1])[:5]:
            print(f"  {d:<26} scan {k:,} vs API {l:,}")
    if failed:
        print(f"WARNING: no usable published total for {len(failed)} dockets (not refreshed): "
              + ", ".join(failed[:6]) + (" ..." if len(failed) > 6 else ""))
    if partial:
        print(f"WARNING: received count incomplete for {len(partial)} dockets (left blank): "
              + ", ".join(partial[:6]) + (" ..." if len(partial) > 6 else ""))
    print("next: analysis/analyze_rulemaking_trends.R, then scripts/build_site.py")


if __name__ == "__main__":
    main()
