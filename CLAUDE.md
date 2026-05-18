# Le Marathon Du Médoc 26 — Project memory

> Persistent context for Claude Code sessions. Update when major decisions
> change. Designed to survive `/compact`.

## What this is

A daily Strava dashboard for the friend-group running the **Marathon du
Médoc** on **5 September 2026** — 42.2 km through the Bordeaux vineyards,
23 wine châteaux on the course, 6:30 cutoff. Stag-do dashboard for the
groom Louis Illig. Crowd size: up to 13, however many actually sign up.

Training cycle starts **1 May 2026** — all cumulative stats are computed
from that date, not rolling windows.

## Key URLs / handles

| What | Value |
| --- | --- |
| Repo | <https://github.com/fbmedoc/Marathon-Du-Medoc-Dashboard> |
| Desktop | <https://fbmedoc.github.io/Marathon-Du-Medoc-Dashboard/> |
| Mobile | <https://fbmedoc.github.io/Marathon-Du-Medoc-Dashboard/mobile.html> |
| Connect | <https://fbmedoc.github.io/Marathon-Du-Medoc-Dashboard/connect.html> |
| Worker | <https://medoc-26-webhook.bloem-fred.workers.dev> |
| Owner | Fred Bloem · bloem.fred@gmail.com · WhatsApp +447511750773 |
| Access code | `medoc26` (shared friend-group gate, lowercase) |

## Architecture (post-May-2026 pivot)

**Each runner runs their own personal Strava app.** This sidesteps the
1-athlete cap on shared apps — the original architecture had one app with
13 tokens, which Strava blocked. Now: 13 independent OAuth contexts.

```
Each runner → their own Strava app → 3 GitHub Secrets per runner
                                       (CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN)
                                       39 secrets at full squad
                                                 │
                       GitHub Actions cron every 5 minutes (no webhooks —
                       impractical under per-runner-app architecture)
                                                 │
                       Refresh tokens, fetch activities since 1 May 2026,
                       compute stats, render Jinja2 templates, commit HTML,
                       deploy GitHub Pages
                                                 │
                       Cloudflare Worker:
                         POST /check-code         → friend-group gate
                         POST /exchange-personal  → OAuth proxy (Strava /oauth/token
                                                    has no CORS, so the Worker
                                                    relays). Doesn't store secrets.
```

## File layout

```
.
├── CLAUDE.md                          ← this file (memory)
├── README.md                          ← public docs
├── runners.json                       ← 13 entries; 3 secret-name slots each
├── connect.html                       ← 3-step walkthrough for personal app
├── scripts/build_dashboard.py         ← Strava → stats → templates
├── templates/desktop.html.j2          ← desktop dashboard
├── templates/mobile.html.j2           ← mobile dashboard
├── webhook/worker.js                  ← Cloudflare Worker
├── webhook/wrangler.toml              ← Worker deploy config
├── webhook/SETUP.md                   ← one-time Worker setup notes
├── .github/workflows/daily.yml        ← hourly cron + push + manual
├── assets/powered-by-strava-orange.svg
└── (generated) index.html, mobile.html
```

## Runners

13 entries in `runners.json`:

| id | name | tag | notes |
| --- | --- | --- | --- |
| `louis` | Louis Illig | ★ The Groom | `is_groom: true` |
| `brother1` | [Brother 1] | Sibling Rivalry | `is_brother: true` |
| `brother2` | [Brother 2] | Sibling Rivalry | `is_brother: true` |
| `matt` | Matt M. | — | |
| `will` | Will J. | — | |
| `freddy` | Freddy B. | — | the maintainer |
| `friend7`–`friend13` | placeholders | — | rename as friends join |

Each entry has `client_id_secret`, `client_secret_secret`,
`refresh_token_secret` → names of GitHub Secrets the workflow reads from.
Convention: `<RUNNER>_CLIENT_ID`, `<RUNNER>_CLIENT_SECRET`,
`<RUNNER>_REFRESH_TOKEN` (uppercase). Missing any of the three → that
runner is silently shown as "Not connected".

## Key constants (in `build_dashboard.py`)

| Constant | Value | Meaning |
| --- | --- | --- |
| `RACE_DATE` | `date(2026, 9, 5)` | Marathon du Médoc 2026 |
| `TRAINING_START` | `date(2026, 5, 1)` | Cumulative-stat baseline |
| `UK_TZ` | `Europe/London` | Activities timestamped in UK time |
| `SUB_4_SECONDS` | `14400` | Sub-4hr marathon reference |
| `MEDOC_PENALTY` | `1.10` | Médoc time = marathon × this (wine stops) |
| `GROUP_TARGET_RATIO` | `0.75` | Squad target = 75% of full plan total |
| `MARATHON_PLAN` | dict, keyed by weeks-to-race | 18-week plan |

## 10 Awards (in order on dashboard)

🤵 Le Groom · 👯 Sibling Rivalry · 👑 Top Dog · 📊 Biggest Shift ·
📈 Biggest Glow-Up · 🔥 La Flamme · 🌅 L'Aurore · 🌡️ The Sufferer ·
👻 The Ghost · 🍷 The Hangover Hero

Each has a neutral fallback when not enough data.

## Worker endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/check-code` | Validate access code |
| POST | `/exchange-personal` | OAuth proxy: takes `{code, client_id, client_secret, access_code}`, returns `{refresh_token, athlete_name}` |
| GET | `/` (with `hub.*` params) | Webhook subscription handshake (vestigial) |
| POST | `/` | Webhook event handler (vestigial — fires `repository_dispatch`) |
| GET | `/` (bare) | Liveness probe |

Worker env bindings:
- Vars: `GITHUB_REPO`, `ALLOWED_ORIGIN`
- Secrets: `ACCESS_CODE` (required), `STRAVA_VERIFY_TOKEN` and `GITHUB_PAT` (vestigial)
- `STRAVA_CLIENT_SECRET` previously held; **delete it** — no longer used.

## Gotchas / lessons learned

- **PowerShell 5.1, not Core.** `&&` and `||` don't work — use `;` with `if ($?)`. Ternary, null-coalescing, null-conditional all absent. Default file encoding is UTF-16 LE — pass `-Encoding utf8` for files other tools read.
- **`wrangler secret put` via PowerShell stdin** appends a trailing newline that Strava rejects (403 on verify). Workaround: write to a temp file with `[System.IO.File]::WriteAllText` (no BOM, no newline) and pipe via `cmd /c "wrangler secret put NAME < file"`.
- **PowerShell ExecutionPolicy** can block npm scripts. Fix: `Set-ExecutionPolicy RemoteSigned -Scope CurrentUser -Force`.
- **Workflow auto-commits regenerated HTML** — `git pull --rebase` before pushing template/script changes, or stash `index.html`/`mobile.html` first.
- **The `cd` Bash trick** prepending `cd <dir>; git ...` triggers a permission prompt — `git` already uses CWD.
- **Strava `/oauth/token` has no CORS.** That's why the Worker proxies the exchange instead of the browser calling Strava direct.
- **GitHub Pages auto-readme**: when you push first to a brand-new repo that GitHub auto-created a README in, you'll need `--force-with-lease`. (Already done long ago.)

## Operational quick reference

| Task | Command |
| --- | --- |
| Manually trigger a rebuild | Actions tab → *Daily dashboard rebuild* → Run |
| Watch live Worker logs | `cd webhook && wrangler tail` |
| Deploy worker | `cd webhook && wrangler deploy` |
| Add Worker secret | `wrangler secret put NAME` in `webhook/` |
| Rotate access code | `wrangler secret put ACCESS_CODE`, redeploy |
| Add runner GitHub secrets | Repo Settings → Secrets and variables → Actions |
| Rename a runner slot | Edit `runners.json` on GitHub |

## Onboarding a new runner

1. Fred sends connect URL + `medoc26` via WhatsApp.
2. Runner: access code → create their own Strava app at
   <https://www.strava.com/settings/api> (Auth Callback Domain
   `fbmedoc.github.io`) → paste Client ID + Secret → authorise → page
   exchanges and shows all 3 credentials → WhatsApp Freddy.
3. Fred adds 3 GitHub Secrets (`<NAME>_CLIENT_ID`, `_CLIENT_SECRET`,
   `_REFRESH_TOKEN`).
4. Fred edits `runners.json` to replace placeholder name with real one.
5. Next hourly build (or manual trigger) → runner appears on dashboard.

Per runner: ~5 min runner side + ~2 min Fred side.

## Current state (2026-05-18)

Per-runner-app pivot is **written locally but not committed**. Cadence
choice: **Option B — cron every 5 minutes** (vs. the old webhook-driven
~60s cadence). Lowest cron interval GitHub Actions supports; balances
freshness with onboarding simplicity.

The following files have uncommitted changes:

- `runners.json` (new 3-slot schema)
- `scripts/build_dashboard.py` (per-runner token refresh)
- `.github/workflows/daily.yml` (39 secrets in env block)
- `webhook/worker.js` (added `/exchange-personal`, removed `/exchange`)
- `webhook/wrangler.toml` (dropped `STRAVA_CLIENT_ID` var)
- `connect.html` (rewrite as 3-step walkthrough)
- `README.md` (updated for new architecture)

Priority checklist still outstanding:

1. Commit + push the 7 changed files.
2. Deploy worker: `cd webhook && wrangler deploy`.
3. Delete old Worker secret: `wrangler secret delete STRAVA_CLIENT_SECRET`.
4. Delete old GitHub secrets (`STRAVA_CLIENT_ID`, `STRAVA_CLIENT_SECRET`,
   `STRAVA_TOKEN_*`) via web UI or `gh secret delete`.
5. Fred re-onboards himself through the new flow → adds `FREDDY_CLIENT_ID`,
   `FREDDY_CLIENT_SECRET`, `FREDDY_REFRESH_TOKEN` as GitHub Secrets.
6. Fred sends connect URL + `medoc26` to the group.

## Style / brand

- Wine palette: `#4a0e1f` (wine), `#f4ead5` (cream), `#c9a961` (gold),
  `#fc4c02` (Strava orange).
- Fonts: Cormorant Garamond (body), Fraunces (italic display),
  DM Mono (mono).
- Runner crest SVG: stick-figure runner with wine glass raised aloft.
- Official "Powered by Strava" wordmark in every footer (brand-compliance).
- `noindex,nofollow` meta on every page.
