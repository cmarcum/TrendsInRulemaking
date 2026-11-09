#!/usr/bin/env python3
"""
Top up the Spicy Regs scan with comments that are already in the raw
Mirrulations bucket but not yet in the Spicy Regs mirror.

Spicy Regs is built from Mirrulations (public S3 bucket `mirrulations`,
anonymous read) and runs a day or two behind it. This script finds the gap
for recently active rulemaking dockets and for brand-new rulemaking dockets,
reads only the missing comment files, and writes the extra counts next to the
cached scan:

  _topup_scan.csv      docket x posted-year rows in the same layout as
                       _docket_year_scan.csv; analysis/analyze_rulemaking_trends.R adds
                       them to the base scan
  _topup_dockets.csv   rulemaking dockets that are not in dockets.parquet yet

How the gap is found: for each candidate docket, the comment ids in the mirror
(parquet) are compared with the comment files in the bucket. The bucket keeps
snapshot copies of changed comments ("ID(1).json"); those count once. Only ids
missing from the mirror are downloaded.

Workflow (from the repository root; --out is the folder holding the scan, data/):
  python scripts/fetch_mirror.py --fetch           (base scan; removes old top-ups)
  python scripts/topup_from_mirrulations.py        (this script)
  python scripts/topup_open_dockets.py             (live counts for open and recently closed dockets)
  Rscript analysis/analyze_rulemaking_trends.R
  python scripts/build_site.py

A top-up is only valid against the base scan it was computed from, so running
--fetch again deletes it. Dockets whose last posted comment is older than
--lookback days before the scan's newest date are not re-checked.

Dockets that are in the bucket but missing from dockets.parquet are treated as
new only when the year in their id is recent (--new-since-years); the bucket
holds roughly 140,000 older dockets the mirror never carried, and those are not
a freshness gap.

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
import xml.etree.ElementTree as ET

try:
    import duckdb
    import pandas as pd
except ImportError:
    sys.exit("pip install duckdb pandas")

BUCKET = "https://mirrulations.s3.amazonaws.com/"
NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"
SPICY_BASE = "https://data.spicy-regs.dev/"
OUT_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "data"))
MIN_YEAR = 1970
THIS_YEAR = datetime.date.today().year
SNAPSHOT_SUFFIX = re.compile(r"\(\d+\)$")
ID_YEAR = re.compile(r"^.*-((?:19[7-9]|20[0-3])\d)-")


def get(url, tries=5):
    for i in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if i == tries - 1:
                raise
        except Exception:
            if i == tries - 1:
                raise
        time.sleep(1.5 * (i + 1))


def list_bucket(prefix, delimiter=None):
    keys, prefixes, token = [], [], None
    while True:
        q = {"list-type": "2", "prefix": prefix}
        if delimiter:
            q["delimiter"] = delimiter
        if token:
            q["continuation-token"] = token
        root = ET.fromstring(get(BUCKET + "?" + urllib.parse.urlencode(q)))
        keys += [c.findtext(NS + "Key") for c in root.findall(NS + "Contents")]
        prefixes += [p.findtext(NS + "Prefix") for p in root.findall(NS + "CommonPrefixes")]
        if root.findtext(NS + "IsTruncated") != "true":
            return keys, prefixes
        token = root.findtext(NS + "NextContinuationToken")


def bucket_dockets(pool):
    """{docket_id: agency folder} for every docket in the bucket."""
    _, agencies = list_bucket("raw-data/", "/")
    found = {}
    for _, prefixes in pool.map(lambda a: list_bucket(a, "/"), agencies):
        for p in prefixes:
            _, agency, docket = p.rstrip("/").split("/")
            found[docket] = agency
    return found


def bucket_comment_ids(docket, agency):
    keys, _ = list_bucket(f"raw-data/{agency}/{docket}/text-{docket}/comments/")
    ids = set()
    for k in keys:
        name = k.rsplit("/", 1)[-1]
        if name.endswith(".json"):
            ids.add(SNAPSHOT_SUFFIX.sub("", name[:-5]))
    return docket, ids


def read_json(docket, agency, folder, name):
    key = f"raw-data/{agency}/{docket}/text-{docket}/{folder}/{name}.json"
    raw = get(BUCKET + urllib.parse.quote(key, safe="/"))
    return json.loads(raw)["data"]["attributes"] if raw else None


def year_ok(date):
    try:
        return MIN_YEAR <= int(date[:4]) <= THIS_YEAR
    except (TypeError, ValueError):
        return False


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=OUT_DIR)
    ap.add_argument("--data", default=SPICY_BASE, help="base URL or folder of the parquet files")
    ap.add_argument("--lookback", type=int, default=30,
                    help="re-check dockets whose newest posted comment is within this many days of the scan's newest date")
    ap.add_argument("--new-since-years", type=int, default=1,
                    help="a docket missing from dockets.parquet counts as new only if the year in its id is "
                         "this many years back or later (the bucket holds ~140k older dockets the mirror lacks)")
    ap.add_argument("--workers", type=int, default=24)
    ap.add_argument("--dry-run", action="store_true", help="list the gap but download nothing")
    a = ap.parse_args()

    base = a.data.rstrip("/") + "/"
    comments_pq, dockets_pq = base + "comments.parquet", base + "dockets.parquet"
    scan = pd.read_csv(os.path.join(a.out, "_docket_year_scan.csv"), dtype={"docket_id": str})
    known_rule = pd.read_csv(os.path.join(a.out, "_rulemaking_dockets.csv"), dtype=str)

    newest = scan["last_posted"].dropna().max()
    since = (pd.Timestamp(newest) - pd.Timedelta(days=a.lookback)).strftime("%Y-%m-%d")
    last = scan.groupby("docket_id")["last_posted"].max()
    recent = set(last[last >= since].index)
    print(f"scan newest posted date {newest}; re-checking {len(recent):,} dockets active since {since}")

    con = duckdb.connect()
    if comments_pq.startswith("http"):
        con.execute("INSTALL httpfs; LOAD httpfs;")
    known_ids = {r[0] for r in con.execute(f"SELECT docket_id FROM read_parquet('{dockets_pq}')").fetchall()}

    pool = cf.ThreadPoolExecutor(a.workers)
    in_bucket = bucket_dockets(pool)
    unknown = sorted(set(in_bucket) - known_ids)
    new_ids = [d for d in unknown if (m := ID_YEAR.match(d)) and int(m.group(1)) >= THIS_YEAR - a.new_since_years]
    print(f"bucket holds {len(in_bucket):,} dockets; {len(unknown):,} are not in dockets.parquet, "
          f"{len(new_ids):,} of them with an id year of {THIS_YEAR - a.new_since_years} or later")

    new_rule = []
    for docket, attrs in zip(new_ids, pool.map(lambda d: read_json(d, in_bucket[d], "docket", d), new_ids)):
        if attrs and str(attrs.get("docketType", "")).lower() == "rulemaking":
            new_rule.append({"docket_id": docket, "agency_code": attrs.get("agencyId") or in_bucket[docket],
                             "docket_title": attrs.get("title")})
    print(f"new rulemaking dockets: {len(new_rule)}")

    candidates = sorted((recent & set(in_bucket)) | {d["docket_id"] for d in new_rule})
    print(f"checking {len(candidates):,} dockets against the mirror")

    con.register("cand", pd.DataFrame({"docket_id": candidates}))
    mirror = {}
    for docket, cid in con.execute(
            f"SELECT docket_id, comment_id FROM read_parquet('{comments_pq}') "
            "WHERE docket_id IN (SELECT docket_id FROM cand)").fetchall():
        mirror.setdefault(docket, set()).add(SNAPSHOT_SUFFIX.sub("", cid))

    missing = {}
    for i, (docket, ids) in enumerate(pool.map(lambda d: bucket_comment_ids(d, in_bucket[d]), candidates), 1):
        gap = ids - mirror.get(docket, set())
        if gap:
            missing[docket] = sorted(gap)
        if i % 250 == 0:
            print(f"  listed {i:,}/{len(candidates):,}")
    total = sum(len(v) for v in missing.values())
    print(f"comments in the bucket but not the mirror: {total:,} across {len(missing):,} dockets")
    for d in sorted(missing, key=lambda d: -len(missing[d]))[:8]:
        print(f"  {d:<26}{len(missing[d]):>8,}")
    if a.dry_run:
        return

    jobs = [(d, c) for d, ids in missing.items() for c in ids]

    def fetch(job):
        docket, cid = job
        return docket, read_json(docket, in_bucket[docket], "comments", cid)

    rows, failed = {}, 0
    for i, (docket, attrs) in enumerate(pool.map(fetch, jobs), 1):
        if not attrs:
            failed += 1
            continue
        posted, received = (attrs.get("postedDate") or "")[:10], (attrs.get("receiveDate") or "")[:10]
        key = (docket, int(posted[:4]) if year_ok(posted) else None)
        r = rows.setdefault(key, {"n": 0, "first": None, "last": None, "recv": None})
        r["n"] += 1
        if year_ok(posted):
            r["first"] = min(r["first"] or posted, posted)
            r["last"] = max(r["last"] or posted, posted)
        if year_ok(received):
            r["recv"] = min(r["recv"] or received, received)
        if i % 2000 == 0:
            print(f"  read {i:,}/{len(jobs):,}")

    out = pd.DataFrame([{"docket_id": d, "posted_year": y, "published_comments": r["n"],
                         "first_posted": r["first"], "last_posted": r["last"], "first_received": r["recv"]}
                        for (d, y), r in sorted(rows.items(), key=lambda kv: (kv[0][0], kv[0][1] or 0))],
                       columns=["docket_id", "posted_year", "published_comments", "first_posted",
                                "last_posted", "first_received"])
    out["posted_year"] = out["posted_year"].astype("Int64")
    out.sort_values(["docket_id", "posted_year"], na_position="last").to_csv(
        os.path.join(a.out, "_topup_scan.csv"), index=False)
    pd.DataFrame(new_rule, columns=["docket_id", "agency_code", "docket_title"]).to_csv(
        os.path.join(a.out, "_topup_dockets.csv"), index=False)
    api_topup = os.path.join(a.out, "_api_topup_scan.csv")
    if os.path.exists(api_topup):
        os.remove(api_topup)
        print("removed _api_topup_scan.csv (computed against the previous top-up); rerun topup_open_dockets.py")
    print(f"wrote {len(out):,} top-up rows covering {int(out['published_comments'].sum()):,} comments "
          f"and {len(new_rule)} new dockets to {a.out}; newest posted {out['last_posted'].max()}")
    if failed:
        print(f"WARNING: {failed:,} comment files could not be read and are not counted")
    print("next: scripts/topup_open_dockets.py, then analysis/analyze_rulemaking_trends.R and scripts/build_site.py")


if __name__ == "__main__":
    main()
