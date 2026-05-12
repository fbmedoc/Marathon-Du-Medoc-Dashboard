# Le Marathon Du Médoc 26

A daily-updating Strava dashboard for 13 amateurs trying to survive 18 weeks
of marathon training before tackling the **Marathon du Médoc** on **5 September
2026** — 42.2 km through Bordeaux vineyards with 23 wine châteaux along the
way. Built for a stag-do running group (groom: Louis Illig); the dashboard
publishes a fully-automated leaderboard tracking every km of the journey.

**Live site:** <https://fbmedoc.github.io/Marathon-Du-Medoc-Dashboard/>
**Mobile view:** <https://fbmedoc.github.io/Marathon-Du-Medoc-Dashboard/mobile.html>
**Self-serve Strava connect page:** <https://fbmedoc.github.io/Marathon-Du-Medoc-Dashboard/connect.html>

## How it works

The dashboard rebuilds on three triggers:

1. **Strava webhook (primary)** — when any connected athlete logs/edits a run,
   Strava POSTs to a Cloudflare Worker that triggers a GitHub Actions rebuild.
   Updates appear within ~60 seconds of the run hitting Strava.
2. **Hourly cron (fallback)** — guarantees a refresh even if a webhook is missed.
3. **Manual trigger** — Actions tab → Run workflow.

Each build pulls every run logged since **1 May 2026** (the training-cycle
start) from Strava for each connected runner, computes the per-runner and
group stats, renders the desktop and mobile dashboards from Jinja2 templates,
and commits the updated HTML. GitHub Pages serves the latest version.

Webhook receiver setup: see [`webhook/SETUP.md`](webhook/SETUP.md).

## Adding a runner

1. **Send the friend the connect page** —
   <https://fbmedoc.github.io/Marathon-Du-Medoc-Dashboard/connect.html>.
   They authorise on Strava, copy the refresh token the page shows them,
   and send it to you on WhatsApp.
2. **Add it as a GitHub Secret** —
   Repo → Settings → Secrets and variables → Actions → New repository secret.
   The name must match the `secret` field in `runners.json`
   (e.g. `STRAVA_TOKEN_FRIEND7`).
3. **Update `runners.json`** —
   replace the `[Friend 7]` placeholder with their real name.
4. **Trigger a rebuild** — see below.

## Triggering a manual rebuild

Repo → Actions → "Daily dashboard rebuild" → Run workflow.

The site usually updates within 2–3 minutes.

## Secrets required

| Secret name | What it is |
| --- | --- |
| `STRAVA_CLIENT_ID` | Your Strava app's client ID |
| `STRAVA_CLIENT_SECRET` | Your Strava app's client secret |
| `STRAVA_TOKEN_LOUIS` | Refresh token for Louis |
| `STRAVA_TOKEN_BROTHER1` | Refresh token for Brother 1 |
| … | (one per runner — see `runners.json`) |

Missing secrets are tolerated — that runner just shows as "Not connected"
until their token is added.

## Local development

```bash
pip install -r requirements.txt
export STRAVA_CLIENT_ID=...
export STRAVA_CLIENT_SECRET=...
export STRAVA_TOKEN_LOUIS=...
# ...etc
python scripts/build_dashboard.py
open index.html
```

## Licence

Personal use only. Built for one wedding, one stag, one weekend in Pauillac.

*« On y va, doucement. »*
