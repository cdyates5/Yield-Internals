# US 10-year yield: market internals dashboard

A self-refreshing dashboard that asks whether market internals confirm the move in the US 10-year Treasury yield.
Nine cross-asset ratios in four pillars (equity leadership, inflation pricing, commodities, rate-differential FX)
are screened, vol-scaled and combined into a composite. That composite is mapped to an internals-implied 13-week yield
change and compared with the actual move. The page also carries the full candidate screen, regime stability,
lead/lag, divergence and robustness tests. The methodology lives in the dashboard's own Method section.

GitHub Actions rebuilds it every US trading day from public data. It deploys only when the build,
a freshness guard and a headless render check all pass.

```
.
├── rebuild.py                       # data fetch + model + single-file HTML (template embedded)
├── requirements.txt                 # pinned: numpy, pandas, playwright
├── tests/check_render.py            # headless Chromium: zero console errors, all charts, no mobile overflow
├── .github/workflows/refresh.yml    # schedule, build, render check, deploy
└── .github/dependabot.yml           # monthly PRs for GitHub Action version bumps
```

Outputs of each run (`site/`): `index.html` (the dashboard), `latest.json` (the headline readings), `.nojekyll`.

---

## 1. Decide who should see it (do this first)

**A GitHub Pages site is public on the internet even when the repository is private.** The one exception is an organisation on
GitHub Enterprise Cloud that sets Pages visibility to private. Pages from a private repository also needs a paid plan
(Pro for personal accounts, Team or Enterprise for organisations).

| Option | Who can see it | Set up |
|---|---|---|
| **A. Private Pages** (Enterprise Cloud org) | Only people with read access to the repo | Private repo, Pages source *GitHub Actions*, then Settings → Pages → Visibility: *Private* |
| **B. Artifact only** (any plan) | Only repo members, by downloading the run artifact | Private repo; add repository variable `DEPLOY_TARGET` = `artifact`; skip Pages entirely |
| **C. Public Pages** | Anyone with the URL | Any repo; Pages source *GitHub Actions* |

All inputs are public market data, but the construction is Acheron research. Pick A or B unless publishing it is intended.

## 2. One-time setup (about 10 minutes)

1. **Create the repository** (private for options A and B) and push these files to `main`:
   ```bash
   git init -b main
   git add .
   git commit -m "Yield internals dashboard"
   git remote add origin git@github.com:<owner>/<repo>.git
   git push -u origin main
   ```
2. **Pages (options A and C).** Settings → Pages → *Build and deployment* → Source: **GitHub Actions**.
3. **Artifact-only (option B).** Settings → Secrets and variables → Actions → *Variables* → New repository variable:
   `DEPLOY_TARGET` = `artifact`.
4. **First run.** Actions → *Refresh yield internals dashboard* → **Run workflow**. The first run takes about 5 minutes
   (full history download plus Chromium install); later runs take 3–4 minutes.
5. **Open it.**
   - Pages: the URL appears on the `deploy` job and under Settings → Pages. It is usually `https://<owner>.github.io/<repo>/`.
   - Artifact: open the run, then under *Artifacts* download `yield-internals-<n>` and open `index.html`.

The push in step 1 also triggers a build, so a deployment may already be running.

## 2b. Add the market-data token (required for the automated build)

GitHub's shared Action runners share IP addresses with thousands of other jobs, and Yahoo Finance rate-limits those
addresses (`HTTP 429`). Locally the dashboard uses Yahoo and needs no key; on GitHub it uses **Tiingo** instead, which
has a free tier that covers every ETF and both yen crosses used here, with 30-plus years of history.

1. **Get a free token.** Sign up at <https://www.tiingo.com>, confirm your email, then copy the API token from your
   account page (Account → API → Token). The free tier is ample: this build makes about 30 requests once a day.
2. **Add it to the repository.** Settings → **Secrets and variables** → **Actions** → **Secrets** tab →
   **New repository secret**. Name it exactly `TIINGO_TOKEN`, paste the token as the value, and save.

That is the whole setup. The workflow reads the secret automatically. Nothing about the token appears in the built page
or the logs.

**What comes from where on the automated build:**

| Data | Source | Note |
|---|---|---|
| 23 equity and style ETFs | Tiingo | total-return adjusted closes |
| USD/JPY, AUD/JPY | Tiingo FX | |
| Copper, gold, crude, silver | Tiingo, via the ETFs CPER, GLD, USO, SLV | the model uses log-change ratios, where these ETFs track the front-month future at 0.92–0.98 weekly-change correlation; CPER history starts 2011, USO 2006 |
| 10-year nominal, real, breakeven yields | Treasury.gov (FRED fallback) | |
| Dollar index (context only, not in the composite) | FRED broad USD index | ICE DXY is not on Tiingo |

The commodity ETF proxies and the dollar substitution are stated on the dashboard itself, in the Method section and the footer.

**Without the token** the automated build still runs, but it falls back to Yahoo and will usually fail on GitHub with the
429 above. Add the token before relying on the schedule.

## 3. Schedule

```yaml
- cron: "30 23 * * 1-5"    # 23:30 UTC, Monday–Friday
```

That is 19:30 New York in summer (18:30 in winter), after the US close and Treasury's end-of-day yield curve.
It lands at **09:30 AEST / 10:30 AEDT** in Melbourne the next morning. GitHub treats cron as best-effort, so runs can start late
at busy times. Change the line to move it; cron is always UTC. Each run also re-builds on any push to `main` that touches
the code, and you can trigger one manually from the Actions tab. The manual trigger has a *clear cache* option that
refetches all history.

The model is weekly, so on each weekday the current week's bar is partial. It is labelled with its last trading date.

## 4. What happens when something breaks

The last good page stays live unless a run passes every check.

| Situation | Behaviour | Deploys? |
|---|---|---|
| A data source fails for a ticker (rate limit, outage) | Tries the next source, then the previous run's cached copy; an orange banner lists any stale tickers | Yes |
| Treasury.gov unreachable | Falls back to FRED (which lags one business day) and says so in the Method section | Yes |
| Data older than 5 calendar days (persistent outage) | `rebuild.py` exits 2, the run fails | **No** |
| Model invariants break (yield out of range, composite gaps, member count, fit collapse) | Exits 3, the run fails | **No** |
| Render check fails (console error, missing charts, mobile overflow) | The run fails | **No** |
| Outage and no cache to fall back on | Exits 1, the run fails | **No** |

Every run writes a readings table to the run's **summary page**, uploads the site as an artifact (kept 30 days),
and links its build log from the dashboard footer.

**Notifications.** GitHub emails the person who last edited the `cron` line when a scheduled run fails. Check your
settings under Settings (profile) → Notifications → Actions.

## 5. Keeping it running

- **Public repositories only:** GitHub disables scheduled workflows after 60 days without repository activity.
  The workflow does not commit, so push anything, or re-enable it from the Actions tab when warned. Private repos are unaffected.
- **Action versions** are pinned to majors that run on Node 24. GitHub removed Node 20 from hosted runners on 16 Sep 2026,
  so older majors fail. Dependabot opens a PR when new majors ship. PRs build and render-check but never deploy,
  so merge once the check is green.
- **Python packages** are pinned. A pandas or numpy upgrade can shift the statistics, so bump them deliberately in a PR
  and compare `latest.json` before merging.
- **Actions minutes:** roughly 4 minutes × ~22 runs ≈ 90–110 minutes a month. Free for public repos; for private repos,
  check your plan's included minutes.
- **Model membership is frozen** in `MEMBERS` inside `rebuild.py`. Every rebuild re-runs the screen. If current data would
  admit or drop an internal, the page shows a drift banner instead of changing the index. To change membership, edit
  `MEMBERS`, push, and the push build redeploys.
- **Yahoo Finance is an unofficial endpoint.** The cache, stale banner and freshness guard absorb short outages. If it breaks
  for good, replace the `yahoo()` function with a licensed feed that returns the same daily adjusted-close series.

## 6. Run it locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
python rebuild.py                          # -> site/index.html, site/latest.json  (uses Yahoo locally)
# to mirror the automated build locally, export your token first:
#   export TIINGO_TOKEN=xxxx   &&   python rebuild.py
python tests/check_render.py site/index.html
```

Options: `--out DIR`, `--cache DIR`, `--ttl-hours 10` (re-download files older than this), `--max-age-days 5`.
Exit codes: `0` ok, `2` stale, `3` sanity failure, `1` fetch/runtime error.

## 7. `latest.json`

Published next to the page on every deploy, so other tools can poll the reading without scraping HTML:

```json
{
 "asof": "2026-09-15", "y10": 5.0,
 "change_13w_bp": 54.0, "implied_13w_bp": 14.2, "unconfirmed_13w_bp": 39.8, "unconfirmed_z": 1.22,
 "composite_13w_z": 0.33, "beta_bp_per_unit": 11.85,
 "pillar_contrib": {"EQ": 0.30, "INF": 1.28, "CMD": 0.31, "FX": -0.70},
 "members": {"USDJPY": {"ratio_13w": -0.043, "contrib": -0.43}, "...": {}},
 "stale": {}, "drift": [], "yield_source": "Treasury.gov", "run_url": "https://github.com/..."
}
```

It is a natural hook for an alert, for example a Slack message when `|unconfirmed_z|` exceeds 1.5. Such alerts are not built in.

## Data sources

- **Tiingo** (automated build): daily total-return adjusted closes for the sector and style ETFs, the two yen crosses (FX endpoint),
  and the commodity ETFs CPER/GLD/USO/SLV that proxy copper, gold, crude and silver. Requires the free `TIINGO_TOKEN` secret.
- **Yahoo Finance** (local build, and last-resort fallback): the same ETFs, FX and COMEX/NYMEX front-month futures, adjusted closes,
  dated to the exchange-local session. Yahoo blocks GitHub's runner IPs, which is why CI uses Tiingo.
- **Treasury.gov**: daily par (nominal) and real yield curves, 10-year column. These are the series FRED republishes as DGS10 and DFII10.
  The breakeven is nominal minus real.
- **FRED**: the broad trade-weighted dollar (the context-only dollar internal), and the yield fallback if Treasury.gov is unreachable.
- **Fonts**: fontsource via jsDelivr, embedded in the page. **Chart.js 4.4.1** loads from cdnjs.

Every run records the source used for each series; the footer of the dashboard summarises them, and `latest.json` is unaffected.
