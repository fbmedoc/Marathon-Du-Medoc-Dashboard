# Le Marathon Du Médoc 26

A real-time, webhook-driven Strava dashboard for thirteen amateurs trying to
survive eighteen weeks of marathon training before tackling the
**Marathon du Médoc** on **5 September 2026** — 42.2 km through the Bordeaux
vineyards, twenty-three wine châteaux on the course, six-and-a-half hour
cutoff before they pack the oysters away.

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
[Strava activity event]
        │
        ▼
[Cloudflare Worker]  ──► subscription verification, OAuth code exchange,
        │                  athlete deauth handling, webhook dispatch
        │
        │  POST /repos/.../dispatches
        ▼
[GitHub Actions] ──► refresh tokens, fetch activities, render templates,
        │              commit HTML, deploy to Pages
        ▼
[GitHub Pages]   ──► serves index.html + mobile.html + connect.html
```

Three trigger paths feed the workflow:

1. **Strava webhook (primary)** — activity create / update / delete, or athlete
   deauthorisation. The Worker forwards to GitHub via `repository_dispatch`.
   Dashboard typically reflects new runs within ~60 seconds.
2. **Hourly cron (safety net)** — guarantees a daily-fresh dashboard even if a
   webhook is missed or the Worker is down.
3. **Manual** — Actions tab → Run workflow.

Each build pulls every run since **1 May 2026** (the training-cycle start) for
each connected runner, computes per-runner and group stats, renders the
desktop and mobile dashboards from Jinja2 templates, and commits the updated
HTML. GitHub Pages serves the latest version.

## Security

- **OAuth code exchange is server-side.** The browser-facing `connect.html`
  never sees the Strava `client_secret`; the Worker holds it as an encrypted
  secret and forwards code exchanges.
- **Access-code gate.** The connect page validates a shared friend-group code
  with the Worker before showing the Strava authorise button — protects the
  Strava athlete-limit quota from random visitors.
- **Refresh tokens at rest** live as encrypted GitHub Secrets (one per
  runner). Access tokens are cached for Strava's 6-hour TTL in GitHub Actions
  cache to minimise refresh-call volume.
- **Read-only scope** (`activity:read_all`) — no writes, no posting to
  athletes' feeds.
- **`noindex,nofollow`** meta on every page; the dashboard won't appear in
  search results.
- **Official "Powered by Strava" wordmark** in the footer of every page.
- **Athlete deauthorisation events** are processed by the Worker; the next
  build marks the runner as disconnected.

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

Every award falls back to a neutral one-liner when there isn't enough data
to crown a winner.

## Onboarding a runner

1. **Send them the connect URL + access code** via the group chat.
2. They authorise Strava, the Worker exchanges the code server-side, the
   page displays their refresh token.
3. They tap **WhatsApp Freddy** — token arrives in your DMs.
4. You add it as a **GitHub Secret** at
   <https://github.com/fbmedoc/Marathon-Du-Medoc-Dashboard/settings/secrets/actions/new>
   with the name from the table below.
5. Edit `runners.json` to replace the placeholder name with their real one.
6. Wait for the next build (≤ 1 h via cron, or trigger manually).

| Person | Secret name |
| --- | --- |
| Louis (groom) | `STRAVA_TOKEN_LOUIS` |
| Brother 1 / 2 | `STRAVA_TOKEN_BROTHER1` / `STRAVA_TOKEN_BROTHER2` |
| Matt / Will / Freddy | `STRAVA_TOKEN_MATT` / `_WILL` / `_FREDDY` |
| Friends 7–13 | `STRAVA_TOKEN_FRIEND7` … `_FRIEND13` |

## Repository layout

```
.
├── README.md                  ← this file
├── runners.json               ← runner config (name + secret slot mapping)
├── requirements.txt           ← Python deps (requests, jinja2, pytz)
├── connect.html               ← Strava OAuth onboarding page (gated)
├── assets/
│   └── powered-by-strava-orange.svg  ← official Strava brand asset
├── scripts/
│   └── build_dashboard.py     ← Strava → stats → render
├── templates/
│   ├── desktop.html.j2        ← desktop dashboard template
│   └── mobile.html.j2         ← mobile dashboard template
├── webhook/
│   ├── worker.js              ← Cloudflare Worker source
│   ├── wrangler.toml          ← Worker deploy config
│   └── SETUP.md               ← one-time webhook setup walkthrough
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
| Add a runner secret | Repo Settings → Secrets and variables → Actions |
| Rename a runner slot | Edit `runners.json` on GitHub (commits trigger a build) |
| Inspect Strava subscription | `curl "https://www.strava.com/api/v3/push_subscriptions?client_id=…&client_secret=…"` |
| Rotate the access code | `wrangler secret put ACCESS_CODE` in `webhook/`, redeploy |

## Required secrets

**GitHub Secrets** (encrypted; used by the workflow):

| Name | Used for |
| --- | --- |
| `STRAVA_CLIENT_ID` | Refresh-token calls during builds |
| `STRAVA_CLIENT_SECRET` | Refresh-token calls during builds |
| `STRAVA_TOKEN_<runner>` | One per runner (see table above) |

**Cloudflare Worker secrets** (encrypted; used by the Worker):

| Name | Used for |
| --- | --- |
| `STRAVA_CLIENT_SECRET` | Server-side OAuth code exchange |
| `STRAVA_VERIFY_TOKEN` | Strava webhook subscription verification |
| `ACCESS_CODE` | Friend-group gate on the connect page |
| `GITHUB_PAT` | Triggers `repository_dispatch` to rebuild the dashboard |

Missing GitHub Secrets for runners are tolerated — that runner simply shows
as "not connected" on the dashboard until their token is added.

## Local development

```bash
pip install -r requirements.txt
export STRAVA_CLIENT_ID=...
export STRAVA_CLIENT_SECRET=...
export STRAVA_TOKEN_LOUIS=...
# … one per runner
python scripts/build_dashboard.py
```

For Worker development: `cd webhook && wrangler dev`.

## Licence

Personal use only. Built for one wedding, one stag, one weekend in Pauillac.

*« On y va, doucement. »*
