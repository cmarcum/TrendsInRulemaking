#!/usr/bin/env python3
"""
Date each rulemaking docket by its earliest document.

regulations.gov does not expose when a docket was created, so the best date
available is the earliest posted date among the docket's documents (a proposed
rule, a notice, a supporting document), read from the mirror's documents table.
scripts/build_site.py uses it to order the page by "Newest docket first" and
"Oldest docket first" more finely than the year in the docket ID.

Output (in --out, default data/)
  _docket_dates.csv    docket_id, first_document_posted (YYYY-MM-DD)

Only the docket_id and posted_date columns are read, which takes a few seconds.
Dockets with no usable document date are left out of the file. Dates before
1970 or in the future are treated as data-entry errors.

Setup: pip install duckdb pandas
"""

import argparse
import datetime
import os
import sys

try:
    import duckdb
    import pandas as pd
except ImportError:
    sys.exit("pip install duckdb pandas")

SPICY_BASE = "https://data.spicy-regs.dev/"
OUT_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "data"))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=OUT_DIR)
    ap.add_argument("--data", default=SPICY_BASE, help="base URL or folder of the parquet files")
    a = ap.parse_args()

    ids = set(pd.read_csv(os.path.join(a.out, "_rulemaking_dockets.csv"), dtype=str, usecols=["docket_id"])["docket_id"])
    extra = os.path.join(a.out, "_topup_dockets.csv")
    if os.path.exists(extra):
        ids |= set(pd.read_csv(extra, dtype=str, usecols=["docket_id"])["docket_id"])

    base = a.data.rstrip("/") + "/"
    con = duckdb.connect()
    if base.startswith("http"):
        con.execute("INSTALL httpfs; LOAD httpfs;")
    this_year = datetime.date.today().year
    df = con.execute(f"""
        SELECT coalesce(docket_id, regexp_replace(document_id, '-[0-9]+$', '')) AS docket_id,
               min(substr(posted_date, 1, 10)) AS first_document_posted
        FROM read_parquet('{base}documents.parquet')
        WHERE try_cast(substr(posted_date, 1, 4) AS INTEGER) BETWEEN 1970 AND {this_year}
        GROUP BY 1
    """).df()
    df = df[df["docket_id"].isin(ids)].sort_values("docket_id")
    df.to_csv(os.path.join(a.out, "_docket_dates.csv"), index=False)
    print(f"dated {len(df):,} of {len(ids):,} rulemaking dockets")


if __name__ == "__main__":
    main()
