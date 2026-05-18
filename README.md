# Le Marathon Du Médoc 26

A daily Strava dashboard for the friend-group running the **Marathon du
Médoc** on **5 September 2026** — 42.2 km through the Bordeaux vineyards,
twenty-three wine châteaux on the course, six-and-a-half hour cutoff
before they pack the oysters away.

## Live URLs

| Page | URL |
| --- | --- |
| Desktop dashboard | <https://fbmedoc.github.io/Marathon-Du-Medoc-Dashboard/> |
| Mobile | <https://fbmedoc.github.io/Marathon-Du-Medoc-Dashboard/mobile.html> |
| Connect page | <https://fbmedoc.github.io/Marathon-Du-Medoc-Dashboard/connect.html> |
| Cloudflare Worker | <https://medoc-26-webhook.bloem-fred.workers.dev> |

The connect page is gated by a shared access code distributed in the group
chat — strangers can't reach the Strava authorise step.

## Architecture

```
[Each runner creates their own personal Strava app]
        │  (sidesteps Strava's 1-athlete cap on shared apps)
        │
        ├─► client_id, client_secret, refresh_token
        │   (3 GitHub Secrets per runner, 39 total at full squad)
        ▼
[Hourly GitHub Actions cron]
        │  refresh each runner's access token (per-runner credentials)
        │  fetch activities since 1 May 2026
        │  compute stats, render Jinja2 templates
        │  commit HTML, deploy to Pages
        ▼
[GitHub Pages] ──► serves index.html + mobile.html + connect.html

[Cloudflare Worker]  ──► OAuth proxy + access-code gate for connect.html
                         (Strava's token endpoint has no CORS, so the
                         Worker stands between browser and Strava)
```

Trigger paths feeding the workflow:

1. **Hourly cron (primary)** — guarantees the dashboard is at most ~60
   minutes stale. With per-runner apps, webhooks aren't practical (each
   runner would have to subscribe their own app), so cron does the heavy
   lifting.
2. **Push to `main`** — every commit triggers a rebuild, useful for
   template/plan/config tweaks.
3. **Manual** — Actions tab → Run workflow.

Each build pulls every run since **1 May 2026** (the training-cycle
start) for each connected runner, computes per-runner and group stats,
renders the desktop and mobile dashboards from Jinja2 templates, and
commits the updated HTML. GitHub Pages serves the latest version.

## Why per-runner Strava apps

Strava's free tier caps a single app at 1 connected athlete. Getting that
limit raised requires brand-compliance review and a multi-week back-and-
forth with Strava developer relations. To avoid that whole song-and-dance,
each runner creates their own personal Strava app (free, takes ~5 min via
the connect page walkthrough). The dashboard handles 13 independent OAuth
contexts rather than 1 shared app with 13 tokens.

Trade-offs:

- **Pro**: no shared-app athlete cap, no Strava approval needed, each
  runner can revoke independently, separate rate-limit buckets per
  runner (200/15min, 2000/day each).
- **Con**: 3× the secrets per runner (39 at full squad), no webhooks
  (cron only), runners have to do a 5-minute self-setup.

## Security

- **Per-runner `client_secret`** flows through the Cloudflare Worker only
  during the one-time OAuth exchange. The Worker doesn't store it — it
  proxies the call to Strava (whose token endpoint has no CORS) and
  returns the refresh_token to the browser.
- **Access-code gate.** The connect page validates a shared friend-group
  code with the Worker before showing any Strava UI — protects against
  drive-by curiosity.
- **All three secrets per runner at rest** are encrypted GitHub Secrets.
  Access tokens are cached for Strava's 6-hour TTL in GitHub Actions
  cache to cut refresh-call volume in half.
- **Read-only scope** (`activity:read_all`) — no writes, no posting to
  athletes' feeds.
- **`noindex,nofollow`** meta on every page; the dashboard won't appear
  in search results.
- **Official "Powered by Strava" wordmark** in the footer of every page.

## Training plan

A standard 18-week plan targeting sub-4hr Médoc, encoded in `MARATHON_PLAN`
in `scripts/build_dashboard.py`:

- Phase I (Foundation, wks 1–4): 28→36 km/wk, long runs to 16 km
- Phase II (Build, wks 5–10): 38–53 km/wk, long runs to 26 km
- Phase III (Peak, wks 11–14): 50–60 km/wk, long runs to 32 km
- Phase IV (Taper, wks 15–18): 40→20 km/wk, race week

The dashboard auto-derives the current week's plan from
`RACE_DATE − today`. The squad's weekly target is **75 % of the full plan
total** (`GROUP_TARGET_RATIO`) — assumes not every runner hits 100 % every
week. Tweak in `build_dashboard.py` if it feels off.

## Awards

Ten awards in total, displayed in this order:

| Icon | Award | What it recognises |
| --- | --- | --- |
| 🤵 | Le Groom | Always Louis Illig (constant) |
| 👯 | The Sibling Rivalry | Pecking order of all three Illigs by total km |
| 👑 | Top Dog | Most km this week, any runner |
| 📊 | Biggest Shift | Largest positive week-on-week km jump |
| 📈 | Biggest Glow-Up | Largest 30-day pace improvement |
| 🔥 | La Flamme | Longest current streak |
| 🌅 | L'Aurore | Most pre-7am runs this week |
| 🌡️ | The Sufferer | Highest weekly avg HR (≥ 3 HR runs) |
| 👻 | The Ghost | Most days since last run |
| 🍷 | The Hangover Hero | Highest Sunday HR-to-pace ratio |

Every award falls back to a neutral one-liner when there isn't enough
data to crown a winner.

## Onboarding a runner

1. **Send them the connect URL + access code** via the group chat.
2. They follow the on-page walkthrough:
   1. Enter the access code.
   2. Create their own Strava app on
      <https://www.strava.com/settings/api> with the exact values shown
      — most importantly: `fbmedoc.github.io` as the Authorization
      Callback Domain.
   3. Paste their Client ID + Client Secret on the connect page, click
      *Connect with Strava*, authorise.
   4. Strava redirects back; the Worker proxies the OAuth exchange and
      returns the refresh_token.
   5. The page shows all three credentials and a *WhatsApp Freddy*
      button — they tap it and the message is pre-filled.
3. You receive their three credentials in WhatsApp.
4. You add them as **three GitHub Secrets** at
   <https://github.com/fbmedoc/Marathon-Du-Medoc-Dashboard/settings/secrets/actions/new>
   using the names from the table below.
5. Edit `runners.json` to replace the placeholder name with their real
   one.
6. Wait for the next build (≤ 1 h via cron, or trigger manually).

| Person | Secret names |
| --- | --- |
| Louis (groom) | `LOUIS_CLIENT_ID`, `LOUIS_CLIENT_SECRET`, `LOUIS_REFRESH_TOKEN` |
| Brother 1 / 2 | `BROTHER1_*` / `BROTHER2_*` (three suffixes as above) |
| Matt / Will / Freddy | `MATT_*` / `WILL_*` / `FREDDY_*` |
| Friends 7–13 | `FRIEND7_*` … `FRIEND13_*` |

Missing GitHub Secrets for a runner are tolerated — that runner simply
shows as "not connected" on the dashboard until all three are added.

## Repository layout

```
.
├── README.md                  ← this file
├── runners.json               ← runner config (name + 3 secret-name slots per runner)
├── requirements.txt           ← Python deps (requests, jinja2, pytz)
├── connect.html               ← Strava OAuth onboarding walkthrough (gated)
├── assets/
│   └── powered-by-strava-orange.svg  ← official Strava brand asset
├── scripts/
│   └── build_dashboard.py     ← Strava → stats → render
├── templates/
│   ├── desktop.html.j2        ← desktop dashboard template
│   └── mobile.html.j2         ← mobile dashboard template
├── webhook/
│   ├── worker.js              ← Cloudflare Worker (OAuth proxy + code gate)
│   ├── wrangler.toml          ← Worker deploy config
│   └── SETUP.md               ← one-time Worker setup walkthrough
├── .github/workflows/
│   └── daily.yml              ← scheduled + dispatch-triggered build
└── (generated)
    ├── index.html             ← rendered desktop dashboard
    └── mobile.html            ← rendered mobile dashboard
```

## Operational quick reference

| Task | How |
| --- | --- |
| Manually trigger a rebuild | Actions tab → *Daily dashboard rebuild* → *Run workflow* |
| Watch live Worker logs | `cd webhook && wrangler tail` |
| Add a runner's 3 secrets | Repo Settings → Secrets and variables → Actions |
| Rename a runner slot | Edit `runners.json` on GitHub (commits trigger a build) |
| Rotate the access code | `wrangler secret put ACCESS_CODE` in `webhook/`, redeploy |

## Required secrets

**GitHub Secrets** (encrypted; used by the workflow). Each runner needs
all three; missing any of the three marks them as not connected.

| Name pattern | Used for |
| --- | --- |
| `<RUNNER>_CLIENT_ID` | The runner's own Strava app client ID |
| `<RUNNER>_CLIENT_SECRET` | The runner's own Strava app client secret |
| `<RUNNER>_REFRESH_TOKEN` | Long-lived refresh token from OAuth exchange |

`<RUNNER>` is one of:
`LOUIS`, `BROTHER1`, `BROTHER2`, `MATT`, `WILL`, `FREDDY`,
`FRIEND7` … `FRIEND13`.

**Cloudflare Worker secrets** (encrypted; used by the Worker):

| Name | Used for |
| --- | --- |
| `ACCESS_CODE` | Friend-group gate on the connect page (e.g. `medoc26`) |
| `STRAVA_VERIFY_TOKEN` | Webhook handshake (vestigial under per-runner-app architecture) |
| `GITHUB_PAT` | (vestigial) Would trigger `repository_dispatch` if a webhook ever fires |

`STRAVA_CLIENT_SECRET` on the Worker is no longer required and can be
deleted — runners' personal client_secrets are passed through the
`/exchange-personal` endpoint at exchange time and never stored.

## Local development

```bash
pip install -r requirements.txt
# Set 3 env vars per runner you want to fetch:
export LOUIS_CLIENT_ID=...
export LOUIS_CLIENT_SECRET=...
export LOUIS_REFRESH_TOKEN=...
# … and so on for each runner you have credentials for
python scripts/build_dashboard.py
```

Runners whose env vars aren't set are silently skipped (logged as "missing
… — marking disconnected"). For Worker development:
`cd webhook && wrangler dev`.

## Licence

Personal use only. Built for one wedding, one stag, one weekend in
Pauillac.

*« On y va, doucement. »*
