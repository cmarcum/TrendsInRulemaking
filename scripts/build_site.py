#!/usr/bin/env python3
"""
Build the dashboard: one self-contained HTML page listing every rulemaking
docket (zero-comment ones included), paged, filterable and linked to
regulations.gov. No network is needed, here or to view the page.

Inputs
  build/rulemaking_dockets_ranked.csv   from analysis/analyze_rulemaking_trends.R
  build/data_exclusions.csv             from the same run (footer text)
  data/_open_dockets.csv                from scripts/topup_open_dockets.py
  data/_api_topup_scan.csv              same (decides whether the footer mentions the API check)
  data/_docket_dates.csv                from scripts/docket_dates.py (orders dockets within a year)
  site/template.html                    the page; __DATA__, __ASOF__, __N__ and
                                        __OPEN_NOTE__ are filled in

Output
  dist/index.html                       what gets deployed to Cloudflare Pages

Run from anywhere: python scripts/build_site.py
"""

import argparse
import datetime
import json
import os
import sys
import datetime

try:
    import pandas as pd
except ImportError:
    sys.exit("pip install pandas")

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))


def end_date(value):
    return value[:10] if isinstance(value, str) else ""


def received(value):
    try:
        return int(value) if value == value and value is not None else 0
    except (TypeError, ValueError):
        return 0


def scope_note(build_dir):
    path = os.path.join(build_dir, "data_exclusions.csv")
    if not os.path.exists(path):
        return ""
    x = pd.read_csv(path)
    if len(x) < 5:
        return ""
    both, legacy, no_year = x.iloc[2], x.iloc[3], x.iloc[4]
    return (f"Left out: {int(both['dockets']):,} dockets ({int(both['comments']):,} comments) whose title mentions an"
            " information collection (Paperwork Reduction Act notices) or whose ID contains FRDOC (bundled Federal"
            f" Register notices). Years may be unreliable for {int(legacy['dockets']):,} legacy dockets first posted"
            f" before the year in their ID, and {int(no_year['dockets']):,} dockets whose ID has no year show none"
            f" ({int(no_year['comments']):,} comment in all).")


def open_note(ranked, data_dir):
    if "comment_end_date" not in ranked or ranked["comment_end_date"].dropna().empty:
        return ""
    note = "Dockets whose comment period is still open are marked in green."
    listed = os.path.join(data_dir, "_open_dockets.csv")
    if os.path.exists(listed) and os.path.exists(os.path.join(data_dir, "_api_topup_scan.csv")):
        checked = pd.read_csv(listed, dtype=str).get("api_checked_on", pd.Series(dtype=str)).dropna()
        if len(checked):
            note += f" Their published counts were checked against the regulations.gov API on {checked.max()}."
    if "comments_received" in ranked and ranked["comments_received"].notna().any():
        note += (" “Received” is the number regulations.gov reports for the documents now open for comment;"
                 " it includes comments the agency has not posted yet, so rankings and bars use published counts only.")
    return note


def docket_dates(data_dir):
    path = os.path.join(data_dir, "_docket_dates.csv")
    if not os.path.exists(path):
        return {}
    x = pd.read_csv(path, dtype=str)
    return dict(zip(x["docket_id"], x["first_document_posted"]))


def sort_date(docket_id, year, dates):
    """Date to order a docket by within the opening year in its ID: the date of its earliest document
    when that falls in the same year, otherwise the start or end of that year, mid-year if undated.
    Empty when the docket has no year, so it sorts last."""
    if not year:
        return ""
    first = dates.get(docket_id)
    if isinstance(first, str) and len(first) >= 10:
        y = int(first[:4])
        return first if y == year else (f"{year}-01-01" if y < year else f"{year}-12-31")
    return f"{year}-07-01"


def build(ranked, template, build_dir, data_dir):
    dates = docket_dates(data_dir)
    rank = ranked["published_comments"].rank(method="min", ascending=False).astype(int)
    rows = []
    for rk, r in zip(rank, ranked.itertuples(index=False)):
        agency = r.agency_code if isinstance(r.agency_code, str) else r.docket_id.split("-")[0]
        rows.append([
            int(rk), r.docket_id,
            r.docket_title if isinstance(r.docket_title, str) else "",
            agency,
            int(r.docket_year) if r.docket_year == r.docket_year else 0,
            int(r.published_comments),
            str(r.first_posted)[:10] if isinstance(r.first_posted, str) else "",
            str(r.last_posted)[:10] if isinstance(r.last_posted, str) else "",
            end_date(getattr(r, "comment_end_date", None)),
            received(getattr(r, "comments_received", None)),
            sort_date(r.docket_id, int(r.docket_year) if r.docket_year == r.docket_year else 0, dates),
        ])
    asof = datetime.date.fromisoformat(str(ranked["last_posted"].dropna().max())[:10]).strftime("%B %d, %Y")
    with open(template, encoding="utf-8") as f:
        html = f.read()
    payload = json.dumps(rows, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    note = (scope_note(build_dir) + " " + open_note(ranked, data_dir)).strip()
    return (html.replace("__DATA__", payload).replace("__ASOF__", asof)
                .replace("__OPEN_NOTE__", note).replace("__N__", f"{len(rows):,}")
                .replace("__BUILT__", datetime.datetime.now(datetime.timezone.utc).strftime("%B %d, %Y")))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default=os.path.join(ROOT, "data"))
    ap.add_argument("--build", default=os.path.join(ROOT, "build"))
    ap.add_argument("--template", default=os.path.join(ROOT, "site", "template.html"))
    ap.add_argument("--dist", default=os.path.join(ROOT, "dist"))
    a = ap.parse_args()

    ranked = pd.read_csv(os.path.join(a.build, "rulemaking_dockets_ranked.csv"), dtype={"docket_id": str})
    html = build(ranked, a.template, a.build, a.data)
    os.makedirs(a.dist, exist_ok=True)
    path = os.path.join(a.dist, "index.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"wrote {path} ({os.path.getsize(path) / 1e6:.1f} MB, {len(ranked):,} dockets)")


if __name__ == "__main__":
    main()
