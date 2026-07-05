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
├── runners.json                       ← 14 entries; 3 secret-name slots each
├── connect.html                       ← 3-step walkthrough for personal app
├── drinks.html                        ← self-select drinks logger (access-code gated)
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
| `brother1` | Fred Illig | Sibling Rivalry | `is_brother: true` |
| `brother2` | [Brother 2] | Sibling Rivalry | placeholder |
| `matt` | Matt M. | — | |
| `will` | Will J. | — | |
| `freddy` | Freddy B. | — | the maintainer |
| `friend7` | Dan Christie | — | |
| `friend8` | Blaise Bacquet | — | |
| `friend9` | Danny Arthur | — | |
| `friend10` | Jack L. | — | |
| `friend11` | Oskar K | — | |
| `friend12` | Will H. | — | |
| `friend13` | Timothy L. | — | |
| `friend14` | Marcus G. | — | added 26 May — squad now 14 |

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
| `DRINK_WINDOW_START` | `date(2026, 6, 1)` | Drinks-tracker window opens |
| `DRINKS_DATA_URL` | Worker `/drinks-data` | Build-time drinks fetch |
| `UK_TZ` | `Europe/London` | Activities timestamped in UK time |
| `SUB_4_SECONDS` | `14400` | Sub-4hr marathon reference |
| `MEDOC_PENALTY` | `1.10` | Médoc time = marathon × this (wine stops) |
| `GROUP_TARGET_RATIO` | `0.75` | Squad target = 75% of full plan total |
| `MARATHON_PLAN` | dict, keyed by weeks-to-race | 18-week plan |

## 12 Awards (order on dashboard)

🤵 Le Groom · 👯 Sibling Rivalry · 👑 Top Dog · 📊 Biggest Shift ·
📈 Biggest Glow-Up · 🔥 La Flamme · 🌅 L'Aurore · 🚱 L'Abstinent ·
🌡️ The Sufferer · 👻 The Ghost · 🍷 The Hangover Hero · 🍷 La Soif

Top Dog is a podium-style tile: leader + 2 closest chasers, ordered by
**`week_km`** (calendar week, Mon→today). Each line shows two numbers:
this-week km first, then `trailing_7d_km` for context. Gap behind the
leader is on the week_km figure. Scope chip: `this wk / 7d`.

**Drinks awards** (from the self-select tracker, not Strava):
- **🍷 La Soif** ("The Thirst") — most self-logged drinks in trailing 7d.
  Shame award. Only counts runners who've logged something.
- **🚱 L'Abstinent** — driest runner *among those who ran in the last 7d*
  (running is the entry ticket; fewest drinks wins, ties → most km).

The award loop + `award_scopes` map live in BOTH templates and must be
edited together. The loop order interleaves shame awards near the end.

Each award has a neutral fallback when not enough data.

## Worker endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/check-code` | Validate access code |
| POST | `/exchange-personal` | OAuth proxy: takes `{code, client_id, client_secret, access_code}`, returns `{refresh_token, athlete_name}` |
| POST | `/log-drinks` | Drinks logger write (access-code gated). Body `{access_code, runner_id, entries:{date:int}}` → merges into `drinks:<id>` KV blob |
| GET | `/drinks-data` | **Open** read. Returns `{runner_id: {date: int}}` for the build script + logger prefill |
| GET | `/` (with `hub.*` params) | Webhook subscription handshake (vestigial) |
| POST | `/` | Webhook event handler (vestigial — fires `repository_dispatch`) |
| GET | `/` (bare) | Liveness probe |

Worker env bindings:
- Vars: `GITHUB_REPO`, `ALLOWED_ORIGIN`
- Secrets: `ACCESS_CODE` (required), `STRAVA_VERIFY_TOKEN` and `GITHUB_PAT` (vestigial)
- KV: `DRINKS` namespace (binding in `wrangler.toml`; for the drinks tracker)
- `STRAVA_CLIENT_SECRET` previously held; **delete it** — no longer used.

### Drinks tracker (added 2026-06-01)

Self-select honour-system tracker. `drinks.html` (access-code gated) →
`POST /log-drinks` → `DRINKS` KV. Build script `fetch_drinks()` reads
`GET /drinks-data` at build time (graceful `{}` if Worker/KV unreachable,
so the dashboard never breaks). Window opens **1 June 2026**
(`DRINK_WINDOW_START`). Bands map est. drink count → vibe: 0 Sec ·
1 Social · 2–3 Éméché · 4+ Bourré (stored as capped ints 0/1/2/4 — the
top two bands are capped so one big night can't dominate the tables; the
tracker logs a rolling 14-day window so the preceding ~week can be backfilled).
Surfaces as: standings "Drinks 7d" column (predicted finish demoted to a
"Predicted Finish" mini-board), wine-glass overlay on the 14-day form
dots, and the two awards above.

**Deploy steps** (one-time KV creation): in `webhook/`, run
`wrangler kv namespace create DRINKS` and `... --preview`, paste the two
returned ids into `wrangler.toml` (`id` / `preview_id`, currently
`REPLACE_WITH_*` placeholders), then `wrangler deploy`. No new GitHub
Secrets needed (reads are open).

## Gotchas / lessons learned

- **Strava now requires a paid subscription for API app owners (June 2026).**
  Standard-tier apps whose owner has no Strava subscription are set to
  status "Inactive" — every API call returns 403 with body
  `{"errors":[{"resource":"Application","field":"Status","code":"Inactive"}]}`.
  Token refresh still succeeds, so the runner shows connected but with
  "no runs since 1 May". Enforcement rolled out 29 June–2 July 2026 and
  knocked out 6 of 12 runners (freddy, matt, friend9, friend11, friend12,
  friend13). Fix: the app *owner* subscribes to Strava, then the app
  reactivates. The build script logs the 403 body since commit `43b7...`.

- **PowerShell 5.1, not Core.** `&&` and `||` don't work — use `;` with `if ($?)`. Ternary, null-coalescing, null-conditional all absent. Default file encoding is UTF-16 LE — pass `-Encoding utf8` for files other tools read.
- **`wrangler secret put` via PowerShell stdin** appends a trailing newline that Strava rejects (403 on verify). Workaround: write to a temp file with `[System.IO.File]::WriteAllText` (no BOM, no newline) and pipe via `cmd /c "wrangler secret put NAME < file"`.
- **`git commit -m "..."` in PowerShell mangles embedded double quotes** when passed to native exes. Symptom: git treats the rest of the message as pathspecs and errors `did not match any file(s) known to git`. Fix: write the message to a temp file with `[System.IO.File]::WriteAllText(..., UTF8Encoding($false))` and use `git commit -F <file>`. PowerShell here-strings (`@'...'@`) also work if the closing `'@` is at column 0 with no leading whitespace, but the temp-file route is bulletproof.
- **GitHub Actions cron is heavily throttled below ~1 hour intervals.** Configured `*/5 * * * *` but actual fire interval is more like every 4-7 hours on this repo. This is a known GitHub Actions limitation — they explicitly state scheduled workflows are delayed under load. Workarounds: bump cron to `0 * * * *` (hourly, more reliably honoured), set up an external pinger (cron-job.org) to hit a workflow_dispatch endpoint, or use Cloudflare's scheduled Worker triggers calling the GitHub API.
- **PowerShell ExecutionPolicy** can block npm scripts. Fix: `Set-ExecutionPolicy RemoteSigned -Scope CurrentUser -Force`.
- **Workflow auto-commits regenerated HTML** — `git pull --rebase` before pushing template/script changes, or stash `index.html`/`mobile.html` first.
- **Adding a new runner slot requires THREE edits, not just `runners.json`:** (1) add the entry to `runners.json`, (2) add 3 lines to `env:` in `.github/workflows/daily.yml` so the secrets are injected into the build step (forgetting this silently marks the runner "Not connected"), (3) add the 3 GitHub Secrets themselves. Missing step (2) is the easy-to-miss one — symptom is "Not connected" with no Actions-log error because the env vars are simply absent.
- **Strava OAuth scope gotcha:** `connect.html` requests `activity:read,activity:read_all` — both, deliberately. `activity:read_all` is opt-in (an unticked-by-default checkbox on Strava's consent screen). If a runner unticks it AND we requested only read_all, the token comes back with no activity scope at all, and `/athlete/activities` returns 401 while `/athlete` still works. Symptom in build log: `raw=0` together with an `activity fetch failed (page 1): 401` line at the top. Fix is the user's: revoke the app, redo the connect flow, tick the activity checkbox. No GitHub-side workaround.
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

## Current state (2026-05-25)

Per-runner-app pivot **landed and live**. Cadence: Cloudflare Worker
cron `*/5 * * * *` fires `repository_dispatch` to GitHub (the Worker's
scheduled trigger is honoured to the minute, unlike GitHub Actions cron
which is throttled to 4-7h on free tier).

**Connected / named slots (11/13):** Freddy B. (maintainer), Fred Illig
(brother1), Louis Illig (groom), Matt M. (matt), Will J. (will),
Dan Christie (friend7), Blaise Bacquet (friend8), Danny Arthur (friend9),
Jack L. (friend10), Oskar K (friend11), Will H. (friend12). Placeholders
remaining: `brother2`, `friend13`.

**Onboarding ops cadence:** runner sends 3 credentials via WhatsApp →
Fred adds three GitHub Secrets (`<NAME>_CLIENT_ID`, `_CLIENT_SECRET`,
`_REFRESH_TOKEN`) → Fred updates the placeholder name in `runners.json`
→ next 5-min cron tick picks them up.

**Sociability / motivation upgrades (May 25):**
- **Live activity ticker** between newsflash and hero-strip — 8 newest
  runs across the squad, runner-coloured borders (gold = groom, rust =
  brothers), avatar or first-letter fallback. Built by
  `build_activity_ticker()` in `build_dashboard.py`.
- **Newsflash variety expanded**: MILESTONE WATCH (within 12 km of
  50/100/150…), PACE LEADER, BIG WEEK (≥25 km/7d), ASCENDED (≥500 m/7d),
  LONG RUN OF THE WEEK, RACE-DAY (top-3 predicted), GROUP TREND
  (week-on-week tone bands keyed off `week_history[1].summary.wow_pct`,
  e.g. "taper week — some might be slacking").
- **Standings sort changed**: now ranks on **last-7-day km** rather than
  cumulative. Total km column dropped from grid (still in the meta line
  under each name). Column header reads "Last 7d". Sort key:
  `(not connected, -trailing_7d_km, -total_km, name)`.
- **Mini-boards**: new "Most km" board added; all boards now show
  every connected runner (no `[:5]` truncation). `roman_ranks` extended
  to 13 (i…xiii) with `|default(loop.index)` safety.
- **Week in Verse**: desktop now has prev/next nav across 4 weeks of
  history (`build_week_history`), mobile remains current-week-only.
- **Hangover Hero**: Sat OR Sun runs, "Highest weekend HR".
- **Award scope chips**: small gold chip per award showing time window.

**Glanceability upgrades (June 9):**
- **Momentum arrows in standings**: per-runner ▲/▼ + km delta under the
  Last-7d figure (from `wow_shift_km`; hidden when |shift| < 1 km).
- **Long Run Watch mini-board**: `longest_14d_km` vs the current week's
  plan `long_km` target; ✓ + moss when met. Sits after "Longest Single Run".
- **Copy squad update button** (`#share-btn`, both dashboards): copies a
  WhatsApp-ready text built by `build_share_text()` (countdown, squad 7d
  totals, top-3 by 7d km, La Soif) to the clipboard.

**Activity filter**: `RUN_SPORT_TYPES = {Run, TrailRun, VirtualRun}`,
checks both `type` and `sport_type` (defensive after Freddy's
watch↔Strava sync hiccup; not a site bug).

**Outstanding:**
- 3 placeholder slots (`brother2`, `friend12`, `friend13`).
- Optional: rotate `FREDDY_CLIENT_SECRET` (pasted in chat during dogfood).

## Style / brand

- Wine palette: `#4a0e1f` (wine), `#f4ead5` (cream), `#c9a961` (gold),
  `#fc4c02` (Strava orange).
- Fonts: Cormorant Garamond (body), Fraunces (italic display),
  DM Mono (mono).
- Runner crest SVG: stick-figure runner with wine glass raised aloft.
- Official "Powered by Strava" wordmark in every footer (brand-compliance).
- `noindex,nofollow` meta on every page.
