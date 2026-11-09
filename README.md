# Trends in rulemaking comments

An interactive page listing every rulemaking docket on regulations.gov with its number of **published** comments, linked back to the docket. Dockets that are open for comment are marked in green with their closing date and the number of comments received so far.

The data comes from the [Spicy Regs](https://github.com/civictechdc/spicy-regs) mirror of regulations.gov (built on [Mirrulations](https://github.com/MoravianUniversity/mirrulations)), topped up from the raw Mirrulations bucket and the live regulations.gov API. Counts are published comments, not comments submitted.

## Layout

```
.github/workflows/refresh.yml   scheduled refresh, rebuild and deploy
scripts/fetch_mirror.py         full pull of the mirror into data/ (also --verify against the API)
scripts/topup_from_mirrulations.py   comments in the raw bucket the mirror lacks
scripts/topup_open_dockets.py   open, recently closed and new dockets from the API
scripts/docket_dates.py         earliest document date per docket, for the newest/oldest sort
scripts/build_site.py           data + template -> dist/index.html
analysis/analyze_rulemaking_trends.R   all analysis, tables, figures and power-law fits
site/template.html              the page
data/                           tracked: the scan and top-up state the refresh builds on
build/                          not tracked: tables and figures written by the R script
dist/                           not tracked: the site that gets deployed
```

## Run it locally

```bash
pip install -r requirements.txt
export REGSGOV_API_KEY=...        # PowerShell: $env:REGSGOV_API_KEY = "..."
python scripts/topup_open_dockets.py
python scripts/docket_dates.py
Rscript analysis/analyze_rulemaking_trends.R --site-only
python scripts/build_site.py      # open dist/index.html
```

`--site-only` writes just the tables the page needs and needs only the `data.table` package. Without it the R script also draws the figures and fits the power laws, which needs `ggplot2`, `patchwork` and `ragg` and takes a few minutes.

To start from scratch, run `python scripts/fetch_mirror.py --fetch` (about two minutes over the network) and `python scripts/topup_from_mirrulations.py` (about 15 minutes) before the steps above. Both delete the top-ups computed against the older base.

## What a refresh does

The daily run only touches dockets that need it. It asks the API for dockets that are open now, dockets whose comment period closed in the last 30 days (agencies can take that long to post every comment), and rulemaking dockets added since the last full pull, then adds the difference to the stored scan. New rulemaking dockets are added to the page whether or not they have a comment period. Every other docket keeps the numbers from the last full pull. The weekly run (Sunday) first re-pulls the mirror and the raw bucket, which picks up late comments on older dockets. The refreshed files in `data/` are committed back to the repository, which is how the next run knows what it already checked.

A refresh can also be started by hand from the Actions tab, with an option for the full re-pull. A push that changes the page, the analysis or the workflow rebuilds and deploys without calling the API.

## Set up GitHub and Cloudflare

1. **Cloudflare Pages project.** Create a project for direct upload once, for example `wrangler pages project create rulemaking-comments` (or in the dashboard under Workers & Pages, Create, Pages, Direct Upload). Add the custom domain to the project afterwards.
2. **Cloudflare API token.** At dash.cloudflare.com/profile/api-tokens create a token with the permission Account, Cloudflare Pages, Edit. Copy your Account ID from the dashboard sidebar.
3. **regulations.gov API key.** Sign up at https://api.data.gov/signup/. A registered key allows 1,000 requests an hour, and a refresh uses a few hundred.
4. **Repository settings, Secrets and variables, Actions:**
   - secrets `REGSGOV_API_KEY`, `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`
   - variable `CLOUDFLARE_PAGES_PROJECT` set to the project name from step 1
5. **Repository settings, Actions, General, Workflow permissions:** allow read and write, so the run can commit the refreshed data.

Without `REGSGOV_API_KEY` the refresh still runs, but open dockets keep their old counts and show no received count.

## Notes

- Dockets whose title mentions an information collection (Paperwork Reduction Act notices) or whose ID contains FRDOC (bundled Federal Register notices) are left out. Dockets first posted before the year in their ID are kept and flagged in the page footer. `analysis/analyze_rulemaking_trends.R` writes the counts to `build/data_exclusions.csv`.
- `data/` holds about 10 MB of CSV and changes in full on the weekly run, so the repository grows by a few MB a week. If that becomes a problem, move the two large files (`_docket_year_scan.csv`, `_rulemaking_dockets.csv`) to a release asset or object storage.
- Open-docket detection comes from the mirror's documents table, which can be a day behind regulations.gov.
