"""
Build the Le Marathon Du Médoc 26 dashboard.

Reads runner config from runners.json, refreshes each runner's Strava access
token, pulls every run since the training-cycle start (1 May 2026),
computes per-runner and group stats, then renders index.html (desktop) and
mobile.html from Jinja2 templates.

Designed to be tolerant: missing secrets, revoked tokens, runners with zero
activities, and Strava API hiccups all degrade gracefully to "—" rather
than crashing the build.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import time as time_module
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from html import escape as html_escape
from pathlib import Path
from statistics import median
from typing import Any
from urllib.parse import quote as url_quote

import pytz
import requests
from jinja2 import Environment, FileSystemLoader, select_autoescape


# ─── Config ────────────────────────────────────────────────────────────
ROOT             = Path(__file__).resolve().parent.parent
TEMPLATES_DIR    = ROOT / "templates"
RUNNERS_FILE     = ROOT / "runners.json"
INDEX_OUT        = ROOT / "index.html"
MOBILE_OUT       = ROOT / "mobile.html"
CACHE_DIR        = ROOT / "cache"
TOKEN_CACHE_FILE = CACHE_DIR / "strava_tokens.json"
TOKEN_BUFFER_S   = 300                          # refresh 5 min before expiry

RACE_DATE          = date(2026, 9, 5)        # Marathon du Médoc 2026
RACE_DATE_LABEL    = "5 September 2026"
TRAINING_START     = date(2026, 5, 1)        # All cumulative stats start here
TRAINING_START_LBL = "since 1 May"           # Short label for the UI

# ─── Drinks tracker ────────────────────────────────────────────────────
# Self-select "how many did you have" data, logged via drinks.html → Worker
# → DRINKS KV, read back here at build time. Window opens 1 June 2026.
# Bands map an estimated drink count to a vibe (honesty is the methodology):
#   0      Sec      — dry. A clean run tomorrow.
#   1      Social   — one glass. "Light workout."
#   2–3    Éméché   — alcohol in the system.
#   4+     Bourré   — a heavy night in the vineyards.
DRINK_WINDOW_START = date(2026, 6, 1)
DRINK_WINDOW_LBL   = "since 1 June"
DRINK_HEAVY_VALUE  = 4                       # top band (Bourré) — render as "4+"
DRINKS_DATA_URL    = "https://medoc-26-webhook.bloem-fred.workers.dev/drinks-data"
TOKENS_URL         = "https://medoc-26-webhook.bloem-fred.workers.dev/tokens"

# Shared Strava app (July-2026 pivot). Strava's new subscription requirement
# killed most per-runner personal apps, so runners now authorise against
# Fred's single subscribed app via connect.html → Worker /register → KV.
# The build pulls those tokens from the Worker; runners without a KV entry
# fall back to the legacy per-runner GitHub-secret credentials.
SHARED_CLIENT_ID     = os.environ.get("STRAVA_CLIENT_ID", "").strip()
SHARED_CLIENT_SECRET = os.environ.get("STRAVA_CLIENT_SECRET", "").strip()
TOKENS_API_KEY       = os.environ.get("TOKENS_API_KEY", "").strip()
UK_TZ              = pytz.timezone("Europe/London")
SUB_4_SECONDS      = 4 * 3600                # reference time for "to sub-4" deltas
MEDOC_PENALTY      = 1.10                    # Médoc time = marathon × this (wine stops!)
GROUP_TARGET_RATIO = 0.75                    # "good week" = 75% of full plan total
WEEK_DAYS          = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Strava's free tier caps shared apps at 1 connected athlete, so each runner
# brings their own personal Strava app. Three secrets per runner: client_id,
# client_secret, refresh_token. There is no shared app credential anymore.

# Non-Strava bits the dashboard wants. Edit these to taste — they don't
# come from any API and the templates expect them in this shape.
PROLOGUE_TEXT = (
    "One groom. One marathon through the most beautifully ridiculous "
    "course in France. Below: the mileage, the rivalries, the sub-4 "
    "dream, the costume vote. On y va."
)

FOOTER_QUOTE = "« On y va, doucement. »"

MEDOC_FACTS = [
    {"num": "23",        "lbl": "Wine Stops",  "desc": "Châteaux pouring along the route. Pace yourself."},
    {"num": "42.2",      "lbl": "The Distance","desc": "Through Pauillac vineyards. The most beautiful suffering.","unit_small": True, "unit": "km"},
    {"num": "6:30",      "lbl": "Time Limit",  "desc": "After that they pack up the oysters. Be earlier than that."},
]

# ─── 18-week marathon plan ────────────────────────────────────────────
# Keyed by "weeks before race day" — 18 = first week of plan, 0 = race week.
# Plan targets a sub-4 marathon (~5:40/km marathon pace). Long runs progress
# from 12 to 32km with deliberate cutback weeks every 3–4 weeks. Race day
# falls on Sunday 5 Sep 2026, so weeks_to_race = (race - today) // 7.
MARATHON_PLAN = {
    18: {"phase_num": "I",   "phase": "Foundation", "target_km": 28, "long_km": 12, "key_session": "Easy run + 6×100m strides",       "focus": "Build aerobic base. Easy effort, just consistency."},
    17: {"phase_num": "I",   "phase": "Foundation", "target_km": 32, "long_km": 14, "key_session": "6×400m @ 5K pace · 90s recovery",  "focus": "First intervals. Keep them short and snappy."},
    16: {"phase_num": "I",   "phase": "Foundation", "target_km": 36, "long_km": 16, "key_session": "5×800m @ 5K pace · 2 min recovery","focus": "Adding volume slowly. Easy days stay easy."},
    15: {"phase_num": "I",   "phase": "Foundation", "target_km": 30, "long_km": 12, "key_session": "Tempo 6km @ half-marathon pace",   "focus": "Cutback week. Recover before the build phase."},
    14: {"phase_num": "II",  "phase": "Build",      "target_km": 40, "long_km": 18, "key_session": "6×800m @ 5K pace",                 "focus": "Build phase begins. Long run lengthens."},
    13: {"phase_num": "II",  "phase": "Build",      "target_km": 43, "long_km": 20, "key_session": "Tempo 8km @ half-marathon pace",   "focus": "Threshold work is the engine builder."},
    12: {"phase_num": "II",  "phase": "Build",      "target_km": 46, "long_km": 22, "key_session": "5×1km @ 5K pace · 2 min recovery", "focus": "Quality session matters most this week."},
    11: {"phase_num": "II",  "phase": "Build",      "target_km": 38, "long_km": 16, "key_session": "Tempo 8km",                        "focus": "Cutback. Don't skip it — the next 6 weeks are heavy."},
    10: {"phase_num": "II",  "phase": "Build",      "target_km": 50, "long_km": 24, "key_session": "6×1km @ 5K pace",                  "focus": "First long over 22km. Slow it down."},
     9: {"phase_num": "II",  "phase": "Build",      "target_km": 53, "long_km": 26, "key_session": "12km @ marathon pace (5:40/km)",   "focus": "Practising race pace. This is the crucial one."},
     8: {"phase_num": "III", "phase": "Peak",       "target_km": 56, "long_km": 28, "key_session": "4×1.6km @ tempo pace",             "focus": "Peak phase. Hard yards. Sleep matters."},
     7: {"phase_num": "III", "phase": "Peak",       "target_km": 46, "long_km": 20, "key_session": "Tempo 10km",                       "focus": "Cutback. You've earned it."},
     6: {"phase_num": "III", "phase": "Peak",       "target_km": 60, "long_km": 30, "key_session": "16km @ marathon pace",             "focus": "Biggest week. Eat. Sleep. Run. Trust the work."},
     5: {"phase_num": "III", "phase": "Peak",       "target_km": 58, "long_km": 32, "key_session": "4×2km @ tempo pace",               "focus": "Final big long run. Pace yourself — it's a rehearsal."},
     4: {"phase_num": "III", "phase": "Peak",       "target_km": 50, "long_km": 24, "key_session": "12km @ marathon pace",             "focus": "Volume peaking. Niggles? Back off."},
     3: {"phase_num": "IV",  "phase": "Taper",      "target_km": 40, "long_km": 18, "key_session": "Tempo 8km",                        "focus": "Taper begins. Volume drops, intensity stays."},
     2: {"phase_num": "IV",  "phase": "Taper",      "target_km": 30, "long_km": 14, "key_session": "Tempo 6km",                        "focus": "Halve the volume. Keep legs fresh."},
     1: {"phase_num": "IV",  "phase": "Taper",      "target_km": 20, "long_km": 10, "key_session": "4×400m @ marathon pace",           "focus": "Race week ahead. Pack the costume."},
     0: {"phase_num": "IV",  "phase": "Race Week",  "target_km": 12, "long_km": "RACE", "key_session": "Shakeout 3km",                 "focus": "RACE DAY 🍷 · 42.2km · château by château. On y va."},
}

PHASE_META = {
    "I":   {"name": "Foundation", "when": "Wks 1–4 · May",      "desc_long": "Easy aerobic base. Build from 28 → 36km/wk. Just consistency.",         "desc_short": "Aerobic base · 28–36km/wk"},
    "II":  {"name": "Build",      "when": "Wks 5–10 · Jun–Jul", "desc_long": "Intervals and threshold enter the chat. Long runs to 26km.",            "desc_short": "Intervals · 26km longs"},
    "III": {"name": "Peak",       "when": "Wks 11–14 · Jul–Aug","desc_long": "The hard yards. 30–32km long runs at 60km weeks. Marathon-pace efforts.","desc_short": "32km longs · 60km/wk"},
    "IV":  {"name": "Taper",      "when": "Wks 15–18 · Aug–Sep","desc_long": "Volume drops by half. Carb load. Trust the work. Pack the costume.",    "desc_short": "Halve volume · carb up"},
}

PLAN_TARGETS = {
    "headline": "An honest path to 03:59:59",
    "blurb": (
        "Sub-four needs 5:40/km on race day. Médoc adds time at every château — "
        "treat it as a stretch goal. The realistic win: everyone finishes "
        "upright, in costume, with photos."
    ),
    "blurb_short": (
        "Sub-four needs 5:40/km on race day. Médoc adds time at every château — "
        "treat it as the stretch goal. Realistic win: everyone finishes in costume."
    ),
    "sub4_pace": "5:40",
    "realistic":  "4:30",
    "cutoff":     "6:30",
}


# ─── Strava API ────────────────────────────────────────────────────────
def load_token_cache() -> dict:
    """Read the persisted token cache (or return empty dict)."""
    if not TOKEN_CACHE_FILE.exists():
        return {}
    try:
        return json.loads(TOKEN_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  token cache unreadable, starting fresh: {e}", file=sys.stderr)
        return {}


def save_token_cache(cache: dict) -> None:
    """Persist the token cache so the next run can skip refresh calls."""
    try:
        CACHE_DIR.mkdir(exist_ok=True)
        TOKEN_CACHE_FILE.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"  token cache save failed: {e}", file=sys.stderr)


def _exchange_refresh_token(client_id: str, client_secret: str, refresh: str) -> dict | None:
    """POST to Strava's /oauth/token. Returns the full JSON response or None.

    Each runner has their own personal Strava app, so client_id/client_secret
    are per-runner — they're not module-level constants anymore.
    """
    if not (client_id and client_secret and refresh):
        return None
    try:
        r = requests.post(
            "https://www.strava.com/oauth/token",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh,
            },
            timeout=15,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        body = getattr(getattr(e, "response", None), "text", "")
        print(f"  token refresh failed: {e} · body={body[:300]}", file=sys.stderr)
        return None


def get_access_token(runner_id: str, client_id: str, client_secret: str,
                     secret_refresh: str, cache: dict,
                     on_rotate=None) -> str | None:
    """
    Get a working access token for the runner, using the cache to avoid
    unnecessary refreshes. Strava rotates refresh tokens, so we cache the
    latest one and prefer it; the GitHub-Secret refresh_token is a fallback
    for cold-cache cases.

    Each runner has their own Strava app — client_id and client_secret are
    per-runner credentials, not shared.
    """
    now = int(time_module.time())
    entry = cache.get(runner_id, {})

    # 1. Cached access_token still valid? Use it directly.
    if entry.get("access_token") and entry.get("expires_at", 0) > now + TOKEN_BUFFER_S:
        return entry["access_token"]

    # 2. Need to refresh. Prefer the cached (most recent) refresh_token.
    refresh_to_use = entry.get("refresh_token") or secret_refresh
    if not refresh_to_use:
        return None

    new_tokens = _exchange_refresh_token(client_id, client_secret, refresh_to_use)

    # 3. Cached refresh_token failed (rotated/revoked)? Fall back to the secret.
    if not new_tokens and entry.get("refresh_token") and entry["refresh_token"] != secret_refresh:
        print(f"  cached refresh token invalid, falling back to secret", file=sys.stderr)
        new_tokens = _exchange_refresh_token(client_id, client_secret, secret_refresh)

    if not new_tokens:
        return None

    cache[runner_id] = {
        "access_token":  new_tokens["access_token"],
        "refresh_token": new_tokens["refresh_token"],
        "expires_at":    new_tokens["expires_at"],
    }
    # Strava rotated the refresh token → persist it at the source (Worker KV
    # for shared-app runners) or the stored one eventually goes stale.
    if on_rotate and new_tokens["refresh_token"] != secret_refresh:
        on_rotate(new_tokens["refresh_token"])
    return new_tokens["access_token"]


# Activity types that count as "a run" for dashboard purposes. Strava has
# both a legacy `type` field ("Run", "TrailRun", etc.) and a newer
# `sport_type` field with more granular values. Newer activities sometimes
# only set sport_type correctly, so we check both. Includes treadmill and
# trail running because those are still marathon training mileage.
RUN_SPORT_TYPES = {"Run", "TrailRun", "VirtualRun"}


def fetch_activities_raw(access_token: str, after_ts: int) -> list[dict]:
    """Fetch raw activities since `after_ts` (unix) — no type filter."""
    activities: list[dict] = []
    page = 1
    while True:
        try:
            r = requests.get(
                "https://www.strava.com/api/v3/athlete/activities",
                headers={"Authorization": f"Bearer {access_token}"},
                params={"after": after_ts, "page": page, "per_page": 200},
                timeout=20,
            )
            r.raise_for_status()
            batch = r.json()
        except Exception as e:
            body = getattr(getattr(e, "response", None), "text", "")
            print(f"  activity fetch failed (page {page}): {e} · body={body[:300]}", file=sys.stderr)
            break
        if not batch:
            break
        activities.extend(batch)
        if len(batch) < 200:
            break
        page += 1
    return activities


def filter_runs(activities: list[dict]) -> list[dict]:
    """Keep only activities tagged as a run. Checks both legacy `type` and
    newer `sport_type` — Strava is inconsistent about which has the run
    designation depending on how/when the activity was recorded."""
    return [
        a for a in activities
        if a.get("type") in RUN_SPORT_TYPES or a.get("sport_type") in RUN_SPORT_TYPES
    ]


def fetch_athlete_summary(access_token: str) -> tuple[str, str]:
    """One-shot GET /athlete — returns (name, athlete_id) for diagnostic
    logging. Athlete ID is the durable identifier (names aren't unique on
    Strava) and can be compared against the URL of the user's profile page."""
    try:
        r = requests.get(
            "https://www.strava.com/api/v3/athlete",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        r.raise_for_status()
        j = r.json()
        fn = (j.get("firstname") or "").strip()
        ln = (j.get("lastname") or "").strip()
        name = f"{fn} {ln}".strip() or "(no name on account)"
        aid = str(j.get("id") or "?")
        return name, aid
    except Exception as e:
        return f"(athlete lookup failed: {e})", "?"


# ─── Drinks tracker data ───────────────────────────────────────────────
def drink_band(n: int) -> dict:
    """Map an estimated drink count to a band. `key` drives CSS/markers;
    empty key means a dry/no-glass day."""
    if n <= 0:
        return {"key": "",      "label": "Sec",    "cls": "b0"}
    if n == 1:
        return {"key": "light", "label": "Social", "cls": "b1"}
    if n <= 3:
        return {"key": "tipsy", "label": "Éméché", "cls": "b3"}
    return {"key": "heavy",     "label": "Bourré", "cls": "b5"}


def drinks_week_dot(total_7d: int) -> str:
    """CSS dot class for a runner's *7-day total* drinks (standings column)."""
    if total_7d <= 0:
        return "b0"
    if total_7d <= 3:
        return "b1"
    if total_7d <= 9:
        return "b3"
    return "b5"


def fmt_drink_total(total: int, heavy: bool) -> str:
    """Render a drink total for prose. If any day hit the capped top band
    (Bourré), the true count is unknown beyond the cap, so read as "N+"."""
    return f"{total}+" if heavy else str(total)


def fetch_shared_tokens() -> dict:
    """GET the Worker's /tokens → { runner_id: { refresh_token, ... } }.

    Authenticated with TOKENS_API_KEY (refresh tokens are credentials).
    Degrades gracefully: no key / Worker down / 401 → {} and every runner
    falls back to their legacy per-runner secrets.
    """
    if not TOKENS_API_KEY:
        return {}
    try:
        r = requests.get(
            TOKENS_URL,
            headers={"Authorization": f"Bearer {TOKENS_API_KEY}"},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict):
            print(f"Shared app: loaded tokens for {len(data)} runner(s).")
            return data
    except Exception as e:
        print(f"  shared-token fetch failed (falling back to legacy secrets): {e}", file=sys.stderr)
    return {}


def push_rotated_token(runner_id: str, refresh_token: str) -> None:
    """Write a rotated refresh token back to the Worker KV. Best-effort —
    Strava rotates refresh tokens, so whatever it returned last is the only
    valid one and must outlive this build."""
    if not TOKENS_API_KEY:
        return
    try:
        requests.post(
            TOKENS_URL,
            headers={
                "Authorization": f"Bearer {TOKENS_API_KEY}",
                "Content-Type": "application/json",
            },
            json={"runner_id": runner_id, "refresh_token": refresh_token},
            timeout=10,
        ).raise_for_status()
        print(f"  [{runner_id}] rotated refresh token persisted to Worker KV")
    except Exception as e:
        print(f"  [{runner_id}] rotated-token write-back failed: {e}", file=sys.stderr)


def fetch_drinks() -> dict:
    """GET the Worker's /drinks-data → { runner_id: { 'YYYY-MM-DD': int } }.

    Degrades gracefully: any failure (Worker not deployed yet, KV unbound,
    network hiccup) returns {} so the dashboard builds fine with no drinks
    overlay rather than crashing.
    """
    try:
        r = requests.get(DRINKS_DATA_URL, timeout=10)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict):
            return data
        print(f"  drinks-data: unexpected shape {type(data).__name__}, ignoring", file=sys.stderr)
    except Exception as e:
        print(f"  drinks-data unavailable ({e}) — building without drinks overlay", file=sys.stderr)
    return {}


def attach_drinks(rs: "RunnerStats", drinks_map: dict, today_uk: date) -> None:
    """Parse a runner's logged drinks and derive the trailing-7d total, the
    per-day overlay (aligned with the 14-day form dots), and today's band."""
    logs = drinks_map.get(rs.cfg["id"], {})
    by_day: dict[str, int] = {}
    if isinstance(logs, dict):
        for k, v in logs.items():
            try:
                d = date.fromisoformat(k)
            except (ValueError, TypeError):
                continue
            if d < DRINK_WINDOW_START or d > today_uk:
                continue
            try:
                by_day[k] = max(0, int(v))
            except (TypeError, ValueError):
                continue
    rs.drinks_by_day = by_day
    rs.has_drinks_log = bool(by_day)

    # Trailing 7 days (clamped to the drink window), inclusive of today.
    win_start = max(DRINK_WINDOW_START, today_uk - timedelta(days=6))
    total = 0
    days = 0
    heavy = False
    d = win_start
    while d <= today_uk:
        n = by_day.get(d.isoformat(), 0)
        total += n
        if n > 0:
            days += 1
        if n >= DRINK_HEAVY_VALUE:
            heavy = True
        d += timedelta(days=1)
    rs.drinks_7d = total
    rs.drink_days_7d = days
    rs.drinks_7d_heavy = heavy

    # Running totals across the whole window (a "session day" = any day with ≥1 drink).
    rs.drink_days_total = sum(1 for v in by_day.values() if v > 0)
    rs.drinks_total = sum(by_day.values())

    rs.today_drinks = by_day.get(today_uk.isoformat(), 0)
    rs.today_band   = drink_band(rs.today_drinks)["key"]

    # 14-day overlay aligned 1:1 with rs.form_dots (oldest → today).
    dots = []
    for i in range(13, -1, -1):
        d = today_uk - timedelta(days=i)
        if d < DRINK_WINDOW_START:
            dots.append("")
            continue
        dots.append(drink_band(by_day.get(d.isoformat(), 0))["key"])
    rs.drink_dots = dots


# ─── Helpers: time & formatting ────────────────────────────────────────
def utc_to_uk(iso_str: str) -> datetime:
    """Parse a Strava ISO-8601 UTC timestamp and convert to UK local time."""
    dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    return dt.astimezone(UK_TZ)


def fmt_pace(seconds_per_km: float | None) -> str:
    """Format pace seconds-per-km as 'M:SS'."""
    if seconds_per_km is None or seconds_per_km <= 0 or math.isnan(seconds_per_km):
        return "—"
    m, s = divmod(int(round(seconds_per_km)), 60)
    return f"{m}:{s:02d}"


def fmt_time_ago(when_uk: datetime, now_uk: datetime) -> str:
    """Render a human 'time ago' label for the activity ticker."""
    seconds = (now_uk - when_uk).total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        mins = int(seconds // 60)
        return f"{mins}m ago"
    if seconds < 86400:
        hours = int(seconds // 3600)
        return f"{hours}h ago"
    if seconds < 86400 * 2:
        return "yesterday"
    days = int(seconds // 86400)
    if days <= 7:
        return f"{days}d ago"
    # Past a week, show the date instead of "Nd ago" (Windows-safe formatting)
    return f"{when_uk.day} {when_uk.strftime('%b')}"


def fmt_hms(seconds: float | None) -> str:
    """Format duration as H:MM (drops seconds for marathon-time look)."""
    if seconds is None or seconds <= 0 or math.isnan(seconds):
        return "—"
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, _ = divmod(rem, 60)
    return f"{h}:{m:02d}"


def fmt_pace_delta(seconds_delta: float | None) -> str:
    """Format a pace delta (seconds, negative = faster) as '−0:18' / '+0:05'."""
    if seconds_delta is None or math.isnan(seconds_delta):
        return "—"
    sign = "−" if seconds_delta < 0 else ("+" if seconds_delta > 0 else "")
    s = int(round(abs(seconds_delta)))
    m, s = divmod(s, 60)
    return f"{sign}{m}:{s:02d}"


def sub4_delta_label(predicted_seconds: float | None) -> str:
    """E.g. 'on for sub-4', '+0:08 to sub-4', '+0:18 to sub-4'."""
    if predicted_seconds is None:
        return "—"
    diff = predicted_seconds - SUB_4_SECONDS
    if diff <= 0:
        return "on for sub-4"
    m, s = divmod(int(round(diff)), 60)
    return f"+{m}:{s:02d} to sub-4"


def build_display_names(runners: list) -> dict:
    """
    Returns a dict of runner_id -> short display name with surname-initial
    disambiguation when first names clash (or share a 3-character prefix).

    Examples (squad has Dan Christie + Danny Arthur, Fred Illig + Freddy B.):
      Dan Christie    -> "Dan C."     (clashes with Danny)
      Danny Arthur    -> "Danny A."   (clashes with Dan)
      Fred Illig      -> "Fred I."    (clashes with Freddy)
      Freddy B.       -> "Freddy B."  (clashes with Fred; surname already an initial)
      Matt M.         -> "Matt"       (no clash — just first name)
      Louis Illig     -> "Louis"      (no clash)

    Used by the activity ticker, weekly recap, and newsflash so people
    with similar first names don't get confused across the dashboard.
    """
    firsts = []
    for r in runners:
        name = r.cfg.get("name", "")
        first = name.split()[0] if name else ""
        # Skip placeholder rows like "[Brother 2]"
        if first.startswith("["):
            first = ""
        firsts.append((r.cfg["id"], first))

    out = {}
    for rid, first in firsts:
        if not first:
            out[rid] = ""
            continue
        prefix = first[:3].lower()
        clashes = any(
            other_first and not other_first.startswith("[")
            and other_first[:3].lower() == prefix
            for other_rid, other_first in firsts
            if other_rid != rid
        )
        if clashes:
            # Find the surname's first letter from the full name
            runner = next((r for r in runners if r.cfg["id"] == rid), None)
            if runner:
                parts = runner.cfg.get("name", "").split()
                if len(parts) >= 2 and parts[1] and not parts[1].startswith("["):
                    initial = parts[1][0].upper().rstrip(".")
                    out[rid] = f"{first} {initial}."
                else:
                    out[rid] = first
            else:
                out[rid] = first
        else:
            out[rid] = first
    return out


def to_roman(n: int) -> str:
    table = [
        (1000,"M"),(900,"CM"),(500,"D"),(400,"CD"),(100,"C"),(90,"XC"),
        (50,"L"),(40,"XL"),(10,"X"),(9,"IX"),(5,"V"),(4,"IV"),(1,"I"),
    ]
    out = ""
    for value, letter in table:
        while n >= value:
            out += letter
            n -= value
    return out


# ─── Per-runner stats ──────────────────────────────────────────────────
@dataclass
class RunnerStats:
    cfg: dict
    connected: bool = False
    activities: list[dict] = field(default_factory=list)
    # Derived
    total_km: float = 0.0
    week_km: float = 0.0
    longest_km: float = 0.0
    longest_seconds: float = 0.0
    avg_pace_s: float | None = None     # seconds per km, distance-weighted
    avg_hr: float | None = None         # bpm, duration-weighted
    elevation_m: float = 0.0
    streak: int = 0
    form_dots: list[str] = field(default_factory=list)  # 5 entries
    predicted_marathon_s: float | None = None
    predicted_medoc_s: float | None = None
    pace_improvement_s: float | None = None   # negative = faster · median pace on ≥5km runs, last 14d vs prior 14d
    pre7am_runs_week: int = 0
    trailing_7d_km: float = 0.0               # km in last 7 days (rolling), for Top Dog
    longest_14d_km: float = 0.0               # longest single run in last 14 days, for Long Run Watch
    pre7am_runs_trailing_7d: int = 0          # pre-7am runs in last 7 days, for L'Aurore
    days_since_last_run: int = 999
    hangover_score: float | None = None       # weekend (Sat/Sun) HR / speed; higher = more hungover
    wow_shift_km: float | None = None         # trailing 7 days' km minus the 7 days before that
    avg_hr_trailing_7d: float | None = None   # duration-weighted across last 7d, requires ≥3 HR runs
    hr_runs_trailing_7d: int = 0
    # Drinks tracker (self-selected, via drinks.html → Worker KV)
    drinks_by_day: dict[str, int] = field(default_factory=dict)  # 'YYYY-MM-DD' → est. drinks
    has_drinks_log: bool = False
    drinks_7d: int = 0                         # total est. drinks in trailing 7d
    drinks_7d_heavy: bool = False              # any trailing-7d day hit the capped top band
    drink_days_7d: int = 0                     # number of days drunk in trailing 7d
    drink_days_total: int = 0                  # running total of session-days since window start
    drinks_total: int = 0                      # running total of est. drinks since window start
    today_drinks: int = 0
    today_band: str = ""                       # "" | light | tipsy | heavy
    drink_dots: list[str] = field(default_factory=list)  # 14 entries aligned with form_dots


def compute_runner(cfg: dict, today_uk: date, token_cache: dict,
                   shared_tokens: dict | None = None) -> RunnerStats:
    """Compute every stat the dashboard needs for one runner."""
    rs = RunnerStats(cfg=cfg)

    # Credential source, in preference order:
    #   1. SHARED APP — runner registered via connect.html → Worker KV holds
    #      their refresh token; client credentials are Fred's subscribed app.
    #   2. LEGACY — the runner's own personal app via 3 GitHub secrets
    #      (kept alive so still-working per-runner apps keep flowing until
    #      everyone has migrated).
    shared = (shared_tokens or {}).get(cfg["id"])
    on_rotate = None
    if shared and shared.get("refresh_token") and SHARED_CLIENT_ID and SHARED_CLIENT_SECRET:
        client_id     = SHARED_CLIENT_ID
        client_secret = SHARED_CLIENT_SECRET
        refresh       = shared["refresh_token"]
        rid = cfg["id"]
        on_rotate = lambda new_rt, _rid=rid: push_rotated_token(_rid, new_rt)
        print(f"[{cfg['id']}] using shared app token")
    else:
        client_id     = os.environ.get(cfg["client_id_secret"], "").strip()
        client_secret = os.environ.get(cfg["client_secret_secret"], "").strip()
        refresh       = os.environ.get(cfg["refresh_token_secret"], "").strip()

        missing = [k for k, v in (
            ("client_id",     client_id),
            ("client_secret", client_secret),
            ("refresh_token", refresh),
        ) if not v]
        if missing:
            print(f"[{cfg['id']}] no shared token, missing {', '.join(missing)} — marking disconnected")
            return rs

    access = get_access_token(cfg["id"], client_id, client_secret, refresh, token_cache, on_rotate)
    if not access:
        print(f"[{cfg['id']}] token refresh failed — marking disconnected")
        return rs

    # Pull every activity since the training-cycle start date, then keep
    # only runs. We log raw vs filtered counts plus the type distribution so
    # if a runner shows 0 runs we can immediately see whether the issue is
    # (a) wrong Strava account on the token, (b) all activities tagged as
    # Workout/Hike/Walk instead of Run, or (c) genuine inactivity.
    cutoff_uk = UK_TZ.localize(datetime.combine(TRAINING_START, time.min))
    raw_activities = fetch_activities_raw(access, int(cutoff_uk.timestamp()))
    activities = filter_runs(raw_activities)

    athlete_name, athlete_id = fetch_athlete_summary(access)
    if raw_activities:
        type_counts = Counter(
            (a.get("sport_type") or a.get("type") or "Unknown") for a in raw_activities
        )
        type_summary = ", ".join(f"{k}={v}" for k, v in type_counts.most_common())
    else:
        type_summary = "(none)"
    print(
        f"[{cfg['id']}] account='{athlete_name}' id={athlete_id} "
        f"raw={len(raw_activities)} runs={len(activities)} "
        f"types={type_summary}"
    )

    rs.connected = True
    rs.activities = activities

    if not activities:
        rs.form_dots = [""] * 14   # neutral dots — no judgement on inactivity
        return rs

    # ─── Distances, elevation ──────────────────────────────────────
    rs.total_km    = sum(a.get("distance", 0) for a in activities) / 1000.0
    rs.elevation_m = sum(a.get("total_elevation_gain", 0) or 0 for a in activities)

    # ─── This week (UK Mon→Sun, today is in current week) ──────────
    monday_uk    = today_uk - timedelta(days=today_uk.weekday())
    sunday_uk    = monday_uk + timedelta(days=6)
    week_start   = UK_TZ.localize(datetime.combine(monday_uk, time.min))
    week_end     = UK_TZ.localize(datetime.combine(sunday_uk, time.max))
    week_runs    = [a for a in activities if week_start <= utc_to_uk(a["start_date"]) <= week_end]
    rs.week_km   = sum(a.get("distance", 0) for a in week_runs) / 1000.0

    # ─── Longest single run ────────────────────────────────────────
    longest = max(activities, key=lambda a: a.get("distance", 0))
    rs.longest_km      = longest.get("distance", 0) / 1000.0
    rs.longest_seconds = longest.get("moving_time", 0) or 0

    # ─── Weighted avg pace and HR ──────────────────────────────────
    total_time = sum(a.get("moving_time", 0) or 0 for a in activities)
    if rs.total_km > 0 and total_time > 0:
        rs.avg_pace_s = total_time / rs.total_km

    hr_num = 0.0
    hr_den = 0.0
    for a in activities:
        hr = a.get("average_heartrate")
        dur = a.get("moving_time", 0) or 0
        if hr and dur:
            hr_num += hr * dur
            hr_den += dur
    if hr_den > 0:
        rs.avg_hr = hr_num / hr_den

    # ─── Streak (consecutive days ending at most today) ────────────
    run_days = set()
    for a in activities:
        run_days.add(utc_to_uk(a["start_date"]).date())

    # Streak resets if previous calendar day has no run. Today is special:
    # if no run today, we still count starting from yesterday.
    streak = 0
    if today_uk in run_days:
        cursor = today_uk
    elif (today_uk - timedelta(days=1)) in run_days:
        cursor = today_uk - timedelta(days=1)
    else:
        cursor = None
    while cursor and cursor in run_days:
        streak += 1
        cursor -= timedelta(days=1)
    rs.streak = streak

    # ─── Form: rolling last 14 days ────────────────────────────────
    # Honest activity dots — no judgement of rest days.
    #   "on"      = solid run, day total >= 5km   (dark green)
    #   "partial" = short / shake-out, ran > 0    (light green / gold)
    #   ""        = rest day or no log            (neutral grey, default css)
    # Oldest (13 days ago) on the left, today on the right.
    by_day = defaultdict(float)
    for a in activities:
        d = utc_to_uk(a["start_date"]).date()
        by_day[d] += a.get("distance", 0) / 1000.0
    dots = []
    for i in range(13, -1, -1):
        d = today_uk - timedelta(days=i)
        km = by_day.get(d, 0.0)
        if km >= 5:
            dots.append("on")
        elif km > 0:
            dots.append("partial")
        else:
            dots.append("")
    rs.form_dots = dots

    # ─── Predicted marathon time (Riegel) ──────────────────────────
    # Threshold raised from 5km to 10km — short runs (warm-ups, recovery
    # shake-outs, sprint sessions) are excluded from the prediction input.
    # Riegel extrapolation from a short, fast workout is unreliable; runners
    # without a 10km+ run yet show "—" for predicted marathon time.
    if rs.longest_km >= 10 and rs.longest_seconds > 0:
        rs.predicted_marathon_s = rs.longest_seconds * (42.2 / rs.longest_km) ** 1.06
        rs.predicted_medoc_s    = rs.predicted_marathon_s * MEDOC_PENALTY

    # ─── Pace improvement: median pace on ≥5km runs, 14d vs prior 14d ─
    # The 5km filter excludes warm-downs, recovery shakeouts and sprint
    # sessions, leaving the runs that actually reflect aerobic fitness.
    # Median (vs mean) is robust to one-off outliers — a single bad long
    # run on a hot day doesn't kill the comparison. Prior window is
    # adaptive: 14 days, OR clamped to TRAINING_START if the cycle is
    # younger, so the award activates from ~day 10 of training rather
    # than waiting for a full 30 days of older data.
    recent_start = today_uk - timedelta(days=13)              # last 14d incl.
    recent_end   = today_uk
    prior_end    = recent_start - timedelta(days=1)
    prior_start  = max(prior_end - timedelta(days=13), TRAINING_START)

    def _long_paces_in_window(start_d: date, end_d: date) -> list[float]:
        """Pace (sec/km) for activities ≥ 5 km within [start, end] inclusive."""
        paces = []
        for a in activities:
            dist_km = (a.get("distance", 0) or 0) / 1000.0
            time_s  = a.get("moving_time", 0) or 0
            if dist_km < 5 or time_s <= 0:
                continue
            if start_d <= utc_to_uk(a["start_date"]).date() <= end_d:
                paces.append(time_s / dist_km)
        return paces

    recent_paces = _long_paces_in_window(recent_start, recent_end)
    older_paces  = _long_paces_in_window(prior_start,  prior_end)

    if len(recent_paces) >= 2 and len(older_paces) >= 2:
        rs.pace_improvement_s = median(recent_paces) - median(older_paces)  # negative = faster now

    # ─── Pre-7am runs this week ────────────────────────────────────
    rs.pre7am_runs_week = sum(
        1 for a in week_runs
        if utc_to_uk(a["start_date"]).hour < 7
    )

    # ─── Days since last run ───────────────────────────────────────
    last_day = max(by_day.keys())
    rs.days_since_last_run = (today_uk - last_day).days

    # ─── Rolling 7-day-vs-prior-7 shift (for Biggest Shift award) ─
    # Compare a runner's trailing 7 days against the 7 days before that.
    # Dodges the "Tuesday morning" problem where a calendar-week comparison
    # only has 1-2 days of data; rolling windows are always 7 days vs 7 days
    # regardless of when the build runs.
    trailing_start = today_uk - timedelta(days=6)
    trailing_end   = today_uk                       # inclusive
    prior_start    = today_uk - timedelta(days=13)
    prior_end      = today_uk - timedelta(days=7)   # inclusive

    trailing_7_km = sum(
        (a.get("distance", 0) / 1000.0) for a in activities
        if trailing_start <= utc_to_uk(a["start_date"]).date() <= trailing_end
    )
    prior_7_km = sum(
        (a.get("distance", 0) / 1000.0) for a in activities
        if prior_start <= utc_to_uk(a["start_date"]).date() <= prior_end
    )
    rs.wow_shift_km    = trailing_7_km - prior_7_km
    rs.trailing_7d_km  = trailing_7_km   # also used by Top Dog

    # Longest single run in the last 14 days (Long Run Watch board). The
    # all-time PB says who *has* gone long; this says who's long-run ready now.
    rs.longest_14d_km = max(
        (
            (a.get("distance", 0) / 1000.0) for a in activities
            if prior_start <= utc_to_uk(a["start_date"]).date() <= trailing_end
        ),
        default=0.0,
    )

    # Pre-7am runs in the trailing 7 days (for L'Aurore)
    rs.pre7am_runs_trailing_7d = sum(
        1 for a in activities
        if utc_to_uk(a["start_date"]).hour < 7
        and trailing_start <= utc_to_uk(a["start_date"]).date() <= trailing_end
    )

    # ─── Hangover Hero: highest avg HR per m/s on weekend runs ─────
    # Saturday or Sunday — catches the people who go big on Fri night too.
    wknd_scores = []
    for a in activities:
        if utc_to_uk(a["start_date"]).weekday() not in (5, 6):  # 5=Sat, 6=Sun
            continue
        hr = a.get("average_heartrate")
        spd = a.get("average_speed")
        if hr and spd and spd > 0:
            wknd_scores.append(hr / spd)
    if wknd_scores:
        rs.hangover_score = max(wknd_scores)

    # ─── Trailing-7-day avg HR (duration-weighted) for The Sufferer ──
    # Reuses the trailing_start/trailing_end window defined for Biggest
    # Shift. The ≥3 HR runs requirement stays — stops a single brutal
    # session deciding the award — but using a rolling 7-day window
    # instead of "this calendar week" means it's reachable from Monday
    # onward, not just by Thursday-Friday.
    hr_7d_runs = [
        a for a in activities
        if a.get("average_heartrate")
        and trailing_start <= utc_to_uk(a["start_date"]).date() <= trailing_end
    ]
    rs.hr_runs_trailing_7d = len(hr_7d_runs)
    if len(hr_7d_runs) >= 3:
        num = sum((a["average_heartrate"] * ((a.get("moving_time") or 0))) for a in hr_7d_runs)
        den = sum(((a.get("moving_time") or 0)) for a in hr_7d_runs)
        if den > 0:
            rs.avg_hr_trailing_7d = num / den

    return rs


# ─── Group-level calculations & awards ─────────────────────────────────
def build_group_stats(runners: list[RunnerStats], today_uk: date) -> dict:
    """Group totals + week-on-week change."""
    connected = [r for r in runners if r.connected]

    total_km     = sum(r.total_km for r in connected)
    elevation_m  = sum(r.elevation_m for r in connected)
    week_km      = sum(r.week_km   for r in connected)

    # Combined pace = total time / total distance
    total_time = sum(
        (a.get("moving_time", 0) or 0)
        for r in connected for a in r.activities
    )
    combined_pace_s = (total_time / total_km) if total_km > 0 else None

    # Group prediction = average of those with a prediction (weighted equally)
    preds = [r.predicted_medoc_s for r in connected if r.predicted_medoc_s]
    group_pred_s = sum(preds) / len(preds) if preds else None

    # Sessions this week
    monday_uk = today_uk - timedelta(days=today_uk.weekday())
    week_start = UK_TZ.localize(datetime.combine(monday_uk, time.min))
    sessions_week = sum(
        1 for r in connected for a in r.activities
        if utc_to_uk(a["start_date"]) >= week_start
    )

    # Week-on-week: this Mon→today vs last Mon→last (today−7)
    last_week_start = week_start - timedelta(days=7)
    last_week_end   = week_start - timedelta(microseconds=1)
    last_week_km = 0.0
    for r in connected:
        for a in r.activities:
            t = utc_to_uk(a["start_date"])
            if last_week_start <= t <= last_week_end:
                last_week_km += a.get("distance", 0) / 1000.0
    if last_week_km > 0:
        wow_pct = ((week_km - last_week_km) / last_week_km) * 100
    else:
        wow_pct = None

    return {
        "total_km":      round(total_km),
        "week_km":       round(week_km),
        "elevation_m":   round(elevation_m),
        "combined_pace": fmt_pace(combined_pace_s),
        "predicted":     fmt_hms(group_pred_s),
        "predicted_delta": sub4_delta_label(group_pred_s).replace("on for sub-4", "on for sub-4"),
        "sessions_week": sessions_week,
        "wow_pct":       round(wow_pct) if wow_pct is not None else None,
    }


def build_trailing_7d(runners: list[RunnerStats], today_uk: date) -> dict:
    """
    Stats for the rolling 7 calendar days ending today (inclusive). Used by
    the "Squad Total · Last 7 days" band — distinct from the Mon-Sun week
    grid which retains training-plan semantics.
    """
    end_now    = UK_TZ.localize(datetime.combine(today_uk + timedelta(days=1), time.min))
    start_now  = UK_TZ.localize(datetime.combine(today_uk - timedelta(days=6), time.min))
    start_prev = UK_TZ.localize(datetime.combine(today_uk - timedelta(days=13), time.min))

    def collect(start: datetime, end: datetime) -> tuple[float, float, int]:
        total_km = 0.0
        total_time = 0.0
        sessions = 0
        for r in runners:
            if not r.connected:
                continue
            for a in r.activities:
                t = utc_to_uk(a["start_date"])
                if start <= t < end:
                    total_km   += (a.get("distance", 0) or 0) / 1000.0
                    total_time += a.get("moving_time", 0) or 0
                    sessions   += 1
        return total_km, total_time, sessions

    cur_km, cur_time, cur_sessions = collect(start_now, end_now)
    prev_km, _, _                  = collect(start_prev, start_now)

    pace_s = (cur_time / cur_km) if cur_km > 0 else None
    wow_pct = round((cur_km - prev_km) / prev_km * 100) if prev_km > 0 else None

    return {
        "total_km":      round(cur_km),
        "sessions":      cur_sessions,
        "combined_pace": fmt_pace(pace_s),
        "wow_pct":       wow_pct,
    }


def build_activity_ticker(runners: list[RunnerStats], display_names: dict, limit: int = 8) -> list[dict]:
    """
    Latest runs across the squad, newest first, for the "Live" activity
    ticker at the top of the dashboard. Each entry includes runner name
    (disambiguated to "Dan C." vs "Danny A." when first names clash),
    avatar, distance, pace, optional activity title, and a human time-ago.
    """
    now_uk = datetime.now(UK_TZ)
    entries: list[dict] = []
    for r in runners:
        if not r.connected:
            continue
        display_name = display_names.get(r.cfg["id"]) or r.cfg["name"].split()[0]
        for a in r.activities:
            when_uk = utc_to_uk(a["start_date"])
            dist_km = (a.get("distance", 0) or 0) / 1000.0
            time_s  = a.get("moving_time", 0) or 0
            if dist_km <= 0 or time_s <= 0:
                continue
            entries.append({
                "runner_name":   r.cfg["name"],
                "runner_first":  display_name,
                "avatar":        r.cfg.get("avatar"),
                "is_groom":      bool(r.cfg.get("is_groom")),
                "is_brother":    bool(r.cfg.get("is_brother")),
                "distance":      f"{dist_km:.1f}",
                "pace":          fmt_pace(time_s / dist_km),
                "elev":          int(round(a.get("total_elevation_gain", 0) or 0)),
                "title":         (a.get("name") or "Run").strip()[:60],
                "when_uk":       when_uk,
                "time_ago":      fmt_time_ago(when_uk, now_uk),
            })
    entries.sort(key=lambda x: x["when_uk"], reverse=True)
    # Strip the raw datetime before returning — the template only needs the string.
    for e in entries[:limit]:
        del e["when_uk"]
    return entries[:limit]


def _build_week_view(runners: list[RunnerStats], monday_uk: date) -> tuple[list[dict], dict]:
    """Build the bar-chart grid + summary stats for a single Mon→Sun week
    starting on `monday_uk`. Pulled out so the same logic powers the
    current-week view and the historical-week navigation. WoW % compares
    against the week immediately before this one."""
    by_weekday: dict[date, float] = {monday_uk + timedelta(days=i): 0.0 for i in range(7)}
    total_sessions = 0
    total_time     = 0.0
    for r in runners:
        if not r.connected:
            continue
        for a in r.activities:
            d = utc_to_uk(a["start_date"]).date()
            if d in by_weekday:
                by_weekday[d] += a.get("distance", 0) / 1000.0
                total_sessions += 1
                total_time     += a.get("moving_time", 0) or 0

    max_km = max(by_weekday.values()) or 1.0
    grid = []
    for i, label in enumerate(WEEK_DAYS):
        day  = monday_uk + timedelta(days=i)
        km   = by_weekday[day]
        rest = km == 0
        grid.append({
            "lbl":        label,
            "km":         f"{km:.0f}" if km > 0 else "—",
            "height_pct": int(round((km / max_km) * 100)) if km > 0 else 0,
            "rest":       rest,
        })

    week_total_km  = sum(by_weekday.values())
    connected_n    = max(1, sum(1 for r in runners if r.connected))
    avg_per_runner = week_total_km / connected_n
    pace_s = (total_time / week_total_km) if week_total_km > 0 else None

    # WoW % against the immediately preceding Mon→Sun week.
    prev_start = monday_uk - timedelta(days=7)
    prev_end   = monday_uk - timedelta(days=1)
    prev_km = 0.0
    for r in runners:
        if not r.connected:
            continue
        for a in r.activities:
            d = utc_to_uk(a["start_date"]).date()
            if prev_start <= d <= prev_end:
                prev_km += a.get("distance", 0) / 1000.0
    if prev_km > 0:
        wow_pct = ((week_total_km - prev_km) / prev_km) * 100
    else:
        wow_pct = None

    summary = {
        "total_km":        round(week_total_km),
        "avg_per_runner":  round(avg_per_runner),
        "combined_pace":   fmt_pace(pace_s),
        "sessions":        total_sessions,
        "wow_pct":         round(wow_pct) if wow_pct is not None else None,
    }
    return grid, summary


def build_week_grid(runners: list[RunnerStats], today_uk: date) -> tuple[list[dict], dict]:
    """Group km per weekday Mon→Sun + summary, for the *current* week."""
    monday_uk = today_uk - timedelta(days=today_uk.weekday())
    return _build_week_view(runners, monday_uk)


def build_week_history(runners: list[RunnerStats], today_uk: date, num_weeks: int = 4) -> list[dict]:
    """Build a list of week views, newest first. weeks[0] is the current
    week; weeks[1] is last week; etc. Each entry carries its grid, summary,
    a human label, and a date range, so the template can render all weeks
    as siblings and a tiny JS toggle can switch which one is visible."""
    monday_uk = today_uk - timedelta(days=today_uk.weekday())
    history = []
    for offset in range(num_weeks):
        week_start = monday_uk - timedelta(days=offset * 7)
        week_end   = week_start + timedelta(days=6)
        grid, summary = _build_week_view(runners, week_start)
        if offset == 0:
            label = "This week"
        elif offset == 1:
            label = "Last week"
        else:
            label = f"{offset} weeks ago"
        # "5 May – 11 May" — Windows-safe day formatting (no %-d)
        date_range = f"{week_start.day} {week_start.strftime('%b')} – {week_end.day} {week_end.strftime('%b')}"
        history.append({
            "offset":     offset,
            "is_current": offset == 0,
            "label":      label,
            "date_range": date_range,
            "grid":       grid,
            "summary":    summary,
        })
    return history


def winner_html(before: str, name: str, after: str) -> str:
    """Build an award-detail string with the winner name wrapped in <b>."""
    return f"{html_escape(before)}<b>{html_escape(name)}</b>{html_escape(after)}"


def build_awards(runners: list[RunnerStats], total_km_rank: list[RunnerStats]) -> dict:
    """Compute every award winner (incl. the drinks-tracker awards La Soif
    and L'Abstinent). Tolerant of empty fields."""

    def first(predicate, key, reverse=True):
        """Return (runner, value) of the best runner by `key`, or (None, None)."""
        candidates = [r for r in runners if predicate(r)]
        if not candidates:
            return None, None
        # Skip None values
        scored = [(r, key(r)) for r in candidates if key(r) is not None]
        if not scored:
            return None, None
        scored.sort(key=lambda x: x[1], reverse=reverse)
        return scored[0]

    awards: dict[str, dict] = {}

    # Le Groom — always Louis, regardless of where he sits on the leaderboard
    groom = next((r for r in runners if r.cfg.get("is_groom")), None)
    if groom:
        first_name = groom.cfg["name"].split()[0]
        awards["le_groom"] = {
            "title":   "Le Groom",
            "icon":    "🤵",
            "image":   groom.cfg.get("avatar"),  # optional — falls back to the icon emoji
            "detail":  winner_html("The whole reason we're here — ", first_name, ". Looking glorious at every château."),
            "featured": True,
        }
    else:
        awards["le_groom"] = {"title": "Le Groom", "icon": "🤵", "detail": "—", "featured": True}

    # Sibling Rivalry — pecking order of all Illigs (groom + brothers) by
    # total km since 1 May. Live ranking, leader on top, others shown behind.
    illigs = sorted(
        [r for r in runners if (r.cfg.get("is_groom") or r.cfg.get("is_brother"))
                            and r.connected and r.total_km > 0],
        key=lambda r: r.total_km,
        reverse=True,
    )
    if illigs:
        leader = illigs[0]
        others = illigs[1:]
        if others:
            tail = " · Behind: " + ", ".join(
                f"{r.cfg['name']} ({int(round(r.total_km))}km)" for r in others
            ) + "."
        else:
            tail = ". The other Illigs haven't shown up yet."
        awards["sibling_rivalry"] = {
            "title":  "The Sibling Rivalry",
            "icon":   "👯",
            "detail": winner_html("Top Illig — ", leader.cfg["name"], f" ({int(round(leader.total_km))}km)" + tail),
        }
    else:
        awards["sibling_rivalry"] = {"title": "The Sibling Rivalry", "icon": "👯", "detail": "Waiting on the Illigs to lace up."}

    # Top Dog — single tile: leader + 2 closest chasers ordered by THIS
    # WEEK's km (Mon→today, calendar week). Each line carries two numbers:
    # this-week (the sort key, shown first) and trailing 7d (for context, so
    # the squad sees a Monday-fresh leaderboard alongside the rolling figure).
    chase_field = sorted(
        [r for r in runners if r.connected and r.week_km > 0],
        key=lambda r: -r.week_km,
    )
    if chase_field:
        leader = chase_field[0]
        chasers = chase_field[1:3]
        lines = [
            f"<b>{html_escape(leader.cfg['name'])}</b> · "
            f"{leader.week_km:.1f}km <small>(this wk)</small> · "
            f"{leader.trailing_7d_km:.1f}km <small>(7d)</small>"
        ]
        for idx, c in enumerate(chasers, start=2):
            gap = leader.week_km - c.week_km
            gap_txt = "level" if gap < 0.05 else f"–{gap:.1f}km"
            lines.append(
                f"#{idx} <b>{html_escape(c.cfg['name'])}</b> · "
                f"{c.week_km:.1f} / {c.trailing_7d_km:.1f} ({gap_txt})"
            )
        awards["top_dog"] = {
            "title":  "Top Dog",
            "icon":   "👑",
            "detail": "<br>".join(lines),
        }
    else:
        awards["top_dog"] = {"title": "Top Dog", "icon": "👑", "detail": "Up for grabs — first to log a run wins it."}

    # Biggest Shift — strictly the biggest positive week-on-week volume jump.
    # Pairs with Biggest Glow-Up: that's pace-change, this is volume-change.
    shift, shift_d = first(
        lambda r: r.connected and r.wow_shift_km is not None,
        lambda r: r.wow_shift_km,
    )
    if shift and shift_d is not None and shift_d > 0.5:
        awards["biggest_shift"] = {
            "title":  "Biggest Shift",
            "icon":   "📊",
            "detail": winner_html("Up most vs prior 7 days — ", shift.cfg["name"], f", +{shift_d:.0f}km. Momentum is a hell of a drug."),
        }
    else:
        awards["biggest_shift"] = {"title": "Biggest Shift", "icon": "📊", "detail": "Squad holding steady — no week-on-week jumps yet."}

    # Biggest Glow-Up — most negative pace_improvement_s (faster)
    # Threshold of -5 sec/km filters out trivial fluctuations; real
    # fitness gains over a fortnight will clear it comfortably.
    glow, glow_d = first(
        lambda r: r.connected,
        lambda r: r.pace_improvement_s,
        reverse=False,  # most negative wins
    )
    if glow and glow_d is not None and glow_d < -5:
        awards["glow_up"] = {
            "title":  "Biggest Glow-Up",
            "icon":   "📈",
            "detail": winner_html(f"Pace dropped {fmt_pace_delta(glow_d)}/km on long runs — ", glow.cfg["name"], " is cooking"),
        }
    else:
        awards["glow_up"] = {"title": "Biggest Glow-Up", "icon": "📈", "detail": "Nobody's clocking faster long runs yet — keep showing up."}

    # La Flamme — longest streak
    flame, flame_d = first(lambda r: r.connected and r.streak > 0, lambda r: r.streak)
    if flame:
        first_name = flame.cfg["name"].split()[0]
        days_s = "day" if flame_d == 1 else "days"
        awards["la_flamme"] = {
            "title":  "La Flamme",
            "icon":   "🔥",
            "detail": winner_html("Longest streak — ", first_name, f", {flame_d} {days_s}. Locked in."),
        }
    else:
        awards["la_flamme"] = {"title": "La Flamme", "icon": "🔥", "detail": "No active streaks. Embarrassing."}

    # L'Aurore — most pre-7am runs in the last 7 days (rolling)
    aurore, aurore_d = first(lambda r: r.connected, lambda r: r.pre7am_runs_trailing_7d)
    if aurore and aurore_d and aurore_d > 0:
        run_s = "run" if aurore_d == 1 else "runs"
        awards["laurore"] = {
            "title":  "L'Aurore",
            "icon":   "🌅",
            "detail": winner_html("Most pre-7am runs — ", aurore.cfg["name"], f", {aurore_d} {run_s} in last 7d"),
        }
    else:
        awards["laurore"] = {"title": "L'Aurore", "icon": "🌅", "detail": "Everyone's allergic to mornings — no pre-7am runs in 7d."}

    # The Sufferer — highest 7-day avg HR (≥3 HR runs required per runner)
    sufferer, hr_val = first(
        lambda r: r.connected and r.avg_hr_trailing_7d is not None,
        lambda r: r.avg_hr_trailing_7d,
    )
    if sufferer and hr_val:
        awards["the_sufferer"] = {
            "title":  "The Sufferer",
            "icon":   "🌡️",
            "detail": winner_html("Highest avg HR over 7 days — ", sufferer.cfg["name"], f", {int(round(hr_val))}bpm. Either training hard or chasing buses."),
        }
    else:
        awards["the_sufferer"] = {"title": "The Sufferer", "icon": "🌡️", "detail": "Not enough HR data yet — strap on the monitors."}

    # The Ghost — most days since last run (only counts connected runners)
    ghost, ghost_d = first(lambda r: r.connected, lambda r: r.days_since_last_run)
    if ghost and ghost_d is not None and ghost_d > 0:
        days_s = "day" if ghost_d == 1 else "days"
        awards["ghost"] = {
            "title":  "The Ghost",
            "icon":   "👻",
            "detail": winner_html("Most days since last run — ", ghost.cfg["name"], f", {ghost_d} {days_s}. 'Starts Monday.'"),
            "shame":  True,
        }
    else:
        awards["ghost"] = {"title": "The Ghost", "icon": "👻", "detail": "Everyone ran today. Miracle.", "shame": True}

    # Hangover Hero — highest weekend (Sat/Sun) HR / pace ratio
    hh, hh_d = first(lambda r: r.connected, lambda r: r.hangover_score)
    if hh and hh_d is not None:
        best_hr = 0
        for a in hh.activities:
            if utc_to_uk(a["start_date"]).weekday() in (5, 6) and a.get("average_heartrate"):
                if a["average_heartrate"] > best_hr:
                    best_hr = a["average_heartrate"]
        awards["hangover_hero"] = {
            "title":  "The Hangover Hero",
            "icon":   "🍷",
            "detail": winner_html(f"Highest weekend HR — ", hh.cfg["name"], f", {int(best_hr)}bpm. Suspicious."),
            "shame":  True,
        }
    else:
        awards["hangover_hero"] = {"title": "The Hangover Hero", "icon": "🍷", "detail": "No suspicious weekends. Yet.", "shame": True}

    # La Soif ("The Thirst") — most self-logged drinks in the trailing 7 days.
    # Pulls from the honour-system drinks tracker (drinks.html → KV), not from
    # Strava. Only counts runners who've actually logged something.
    soif_candidates = [r for r in runners if r.has_drinks_log and r.drinks_7d > 0]
    if soif_candidates:
        soif = max(soif_candidates, key=lambda r: (r.drinks_7d, r.drink_days_7d))
        nights = soif.drink_days_7d
        night_s = "night" if nights == 1 else "nights"
        soif_total = fmt_drink_total(soif.drinks_7d, soif.drinks_7d_heavy)
        awards["la_soif"] = {
            "title":  "La Soif",
            "icon":   "🍷",
            "detail": winner_html(
                "Most drinks logged (7d) — ",
                soif.cfg["name"],
                f", {soif_total} across {nights} {night_s}. Vive le Médoc.",
            ),
            "shame":  True,
        }
    else:
        awards["la_soif"] = {
            "title":  "La Soif",
            "icon":   "🍷",
            "detail": "Nobody's owned up yet — log your week at /drinks.html.",
            "shame":  True,
        }

    # L'Abstinent — the driest runner among those who actually RAN in the last
    # 7 days. Running is the entry ticket: you can't win sobriety on the sofa.
    # Fewest drinks wins; ties broken by who ran the most.
    abst_candidates = [
        r for r in runners
        if r.has_drinks_log and r.connected and r.trailing_7d_km > 0
    ]
    if abst_candidates:
        abst = min(abst_candidates, key=lambda r: (r.drinks_7d, -r.trailing_7d_km))
        tail = (
            f", bone dry while running {abst.trailing_7d_km:.0f}km (7d). Saint."
            if abst.drinks_7d == 0
            else f", just {fmt_drink_total(abst.drinks_7d, abst.drinks_7d_heavy)} drinks on {abst.trailing_7d_km:.0f}km (7d). Disciplined."
        )
        awards["labstinent"] = {
            "title":  "L'Abstinent",
            "icon":   "🚱",
            "detail": winner_html("Driest runner of the week — ", abst.cfg["name"], tail),
        }
    else:
        awards["labstinent"] = {
            "title":  "L'Abstinent",
            "icon":   "🚱",
            "detail": "No runner has both run and logged this week — the title waits.",
        }

    return awards


def build_mini_boards(runners: list[RunnerStats], today_uk: date) -> dict:
    """Per-metric leaderboards for the 'Detail' section. Each board ranks
    every connected runner, not just top 5 — the template caps visible rows
    via CSS max-height + scroll, so the data stays available."""
    connected = [r for r in runners if r.connected]

    def board(predicate, key, fmt, reverse=True, value_class=None):
        items = [(r, key(r)) for r in connected if predicate(r)]
        items = [(r, v) for r, v in items if v is not None]
        items.sort(key=lambda x: x[1], reverse=reverse)
        out = []
        for r, v in items:                # all connected runners with valid data
            row = {"name": r.cfg["name"], "value": fmt(v)}
            if value_class:
                row["class"] = value_class(v) if callable(value_class) else value_class
            out.append(row)
        return out

    # NEW: Most km since 1 May. Sits at the top of the mini-boards row as the
    # primary "who's putting the work in" board.
    most_km = board(
        lambda r: r.total_km > 0,
        lambda r: r.total_km,
        lambda v: f"{int(round(v))} km",
    )
    longest = board(
        lambda r: r.longest_km > 0,
        lambda r: r.longest_km,
        lambda v: f"{v:.1f} km",
    )
    pace_imp = board(
        lambda r: r.pace_improvement_s is not None,
        lambda r: r.pace_improvement_s,
        lambda v: fmt_pace_delta(v),
        reverse=False,
        value_class=lambda v: "good" if v < 0 else ("bad" if v > 0 else ""),
    )
    elevation = board(
        lambda r: r.elevation_m > 0,
        lambda r: r.elevation_m,
        lambda v: f"{int(round(v)):,} m",
    )
    hr = board(
        lambda r: r.avg_hr is not None and r.avg_hr > 0,
        lambda r: r.avg_hr,
        lambda v: f"{int(round(v))} bpm",
        reverse=False,           # lower HR = more aerobic = top
        value_class=lambda v: "good" if v < 155 else "",
    )
    # Predicted Médoc finish (Riegel × wine penalty). Moved here from the
    # standings — interesting, but secondary to current activity. Lower = top.
    predicted = board(
        lambda r: r.predicted_medoc_s is not None and r.predicted_medoc_s > 0,
        lambda r: r.predicted_medoc_s,
        lambda v: fmt_hms(v),
        reverse=False,
    )

    # Long Run Watch: longest single run in the last 14 days vs the plan's
    # long-run target for the current week. Hitting the target earns a tick
    # and the moss colour — the most marathon-relevant readiness signal.
    long_target = current_week_plan(today_uk)["long_km"]
    long_watch = board(
        lambda r: r.longest_14d_km > 0,
        lambda r: r.longest_14d_km,
        lambda v: f"{v:.1f} km" + (" ✓" if v >= long_target else ""),
        value_class=lambda v: "good" if v >= long_target else "",
    )

    # Drinking sessions: running total of session-days (any day with ≥1 drink)
    # since the drink window opened. Honour-system, fed by drinks.html → KV.
    # Most sessions on top; ties broken by total drinks. Spans ALL runners, not
    # just Strava-connected ones — logging a pint doesn't require a Strava link.
    sessions = []
    sess_items = sorted(
        [r for r in runners if r.has_drinks_log and r.drink_days_total > 0],
        key=lambda r: (r.drink_days_total, r.drinks_total),
        reverse=True,
    )
    for r in sess_items:
        d, n = r.drink_days_total, r.drinks_total
        sessions.append({
            "name": r.cfg["name"],
            "value": f"{d} day{'s' if d != 1 else ''} · {n} drink{'s' if n != 1 else ''}",
        })

    # Form: rank by count of solid-run days, ties broken by short-run days.
    # Rewards consistency over the last 14 days, no penalty for rest.
    # Now shows every connected runner (used to be top 5 only).
    def form_score(r):
        on_count      = sum(1 for d in (r.form_dots or []) if d == "on")
        partial_count = sum(1 for d in (r.form_dots or []) if d == "partial")
        return (on_count, partial_count)

    form_runners = sorted(connected, key=form_score, reverse=True)
    # Shared day-of-week labels for the form mini-board — same window for
    # every runner, so we render the header once at the top of the board.
    # dots[0] = 13 days ago, dots[13] = today.
    day_labels = [
        (today_uk - timedelta(days=13 - i)).strftime("%a")[:2]
        for i in range(14)
    ]
    form_rows = []
    for r in form_runners:
        dots = r.form_dots or [""] * 14
        run_count = sum(1 for c in dots if c in ("on", "partial"))
        # Wine-glass overlay aligned 1:1 with the form dots. Each entry is
        # "" | light | tipsy | heavy; the template shows a glass for any
        # non-empty band on the matching day.
        drink_dots = r.drink_dots if r.drink_dots else [""] * 14
        form_rows.append({
            "name": r.cfg["name"],
            "dots": dots,
            "drink_dots": drink_dots,
            "run_count": run_count,
        })
    form = {"day_labels": day_labels, "rows": form_rows}

    return {
        "most_km":     most_km,
        "longest":     longest,
        "long_watch":  long_watch,
        "long_target": long_target,
        "pace_imp":    pace_imp,
        "elevation":   elevation,
        "hr":          hr,
        "predicted":   predicted,
        "sessions":    sessions,
        "form":        form,
    }


# ─── News flash auto-generator ─────────────────────────────────────────
def build_newsflash(
    runners: list[RunnerStats],
    group: dict,
    days_until: int,
    this_week: dict,
    plan_status: dict,
    today_uk: date,
    week_history: list[dict],
    display_names: dict,
) -> list[dict]:
    """
    A marquee biased toward people-driven banter — rivalries, heroes, ghosts.
    Roughly half the items call out connected runners by first name (with
    surname-initial disambiguation when needed). Evergreen Médoc lore is
    kept thin so the data does most of the talking.
    """
    items: list[dict] = []
    connected = [r for r in runners if r.connected]
    not_connected_n = sum(1 for r in runners if not r.connected)

    def first_name(r: RunnerStats) -> str:
        return display_names.get(r.cfg["id"]) or r.cfg["name"].split()[0]

    # ── COUNTDOWN ─────────────────────────────────────────────────
    if days_until > 84:
        items.append({"label": "COUNTDOWN", "text": f"{days_until} days till Pauillac · {days_until // 7} long Sundays to suffer through"})
    elif days_until > 28:
        items.append({"label": "COUNTDOWN", "text": f"{days_until} days till the start gun · stop scrolling, start running"})
    elif days_until > 14:
        items.append({"label": "COUNTDOWN", "text": f"{days_until} days · taper season approaches, try not to peak too early"})
    elif days_until > 0:
        items.append({"label": "COUNTDOWN", "text": f"{days_until} days · we are now officially in the panic zone"})
    else:
        items.append({"label": "RACE DAY",  "text": "Today is the day · on y va, doucement"})

    # ── PHASE ───────────────────────────────────────────────────
    items.append({"label": "PHASE", "text": f"Week {this_week['week_num']} of 18 — {this_week['phase']} · {this_week['focus']}"})

    # ── ROLL CALL — sharper ─────────────────────────────────────
    if not_connected_n > 0:
        items.append({"label": "ROLL CALL", "text": f"{not_connected_n} of 13 still ghosting the dashboard · we see you, lads"})

    # Pre-sort by total km — used by several leaderboard-driven items below.
    by_km = sorted(connected, key=lambda r: -r.total_km)

    # ── LEADERBOARD TOP 3 ───────────────────────────────────────
    if len(by_km) >= 3:
        names = " > ".join(first_name(r) for r in by_km[:3])
        items.append({"label": "LEADERBOARD", "text": f"Top three this season: {names} · everyone below has months to do something about it"})

    # ── GAP TO LEADER — bottom of connected pile gets called out ─
    if len(by_km) >= 3:
        leader = by_km[0]
        bottom = by_km[-1]
        gap = leader.total_km - bottom.total_km
        if gap > 15:
            items.append({"label": "GAP", "text": f"{first_name(bottom)} sits {int(round(gap))}km behind {first_name(leader)} · the gap is now a chasm"})

    # ── HEAD TO HEAD — closest pair on the leaderboard ──────────
    if len(by_km) >= 3:
        smallest_gap = None
        pair = None
        for i in range(len(by_km) - 1):
            g = by_km[i].total_km - by_km[i+1].total_km
            if g > 0 and (smallest_gap is None or g < smallest_gap):
                smallest_gap = g
                pair = (by_km[i], by_km[i+1])
        if pair and smallest_gap is not None and smallest_gap < 5:
            items.append({"label": "HEAD TO HEAD", "text": f"{first_name(pair[0])} only {smallest_gap:.1f}km ahead of {first_name(pair[1])} · this is the rivalry to watch"})

    # ── LOUIS LONG RUN ──────────────────────────────────────────
    louis = next((r for r in runners if r.cfg.get("is_groom")), None)
    if louis and louis.connected and louis.longest_km > 0:
        items.append({"label": "BREAKING", "text": f"Louis just clocked a {louis.longest_km:.1f}km long run · the groom is putting in the work — what's everyone else's excuse?"})

    # ── SIBLING RIVALRY ─────────────────────────────────────────
    bros = [r for r in connected if r.cfg.get("is_brother")]
    if bros and louis and louis.connected and louis.total_km > 0:
        best_bro = max(bros, key=lambda r: r.total_km)
        gap = louis.total_km - best_bro.total_km
        if gap > 0.5:
            items.append({"label": "RIVALRY", "text": f"{first_name(best_bro)} {int(round(gap))}km behind the groom · Christmas dinner is going to be tense"})
        elif gap < -0.5:
            items.append({"label": "RIVALRY", "text": f"{first_name(best_bro)} OUTRUNNING the groom by {int(round(-gap))}km · Louis, where's the alpha energy?"})
        else:
            items.append({"label": "RIVALRY", "text": f"Illig brothers within a km of each other · the family WhatsApp is presumably on fire"})

    # ── DARK HORSE: non-Illig stacking quietly ──────────────────
    non_illig = [r for r in connected if not r.cfg.get("is_brother") and not r.cfg.get("is_groom")]
    if non_illig:
        dh = max(non_illig, key=lambda r: r.total_km)
        if dh.total_km >= 30:
            items.append({"label": "DARK HORSE", "text": f"{first_name(dh)} quietly stacking {int(round(dh.total_km))}km · the non-Illigs have arrived"})

    # ── PACE GLOW-UP ────────────────────────────────────────────
    glow_cand = [r for r in connected if r.pace_improvement_s is not None and r.pace_improvement_s < -10]
    if glow_cand:
        g = min(glow_cand, key=lambda r: r.pace_improvement_s)
        items.append({"label": "PB", "text": f"{first_name(g)} drops {fmt_pace_delta(g.pace_improvement_s)}/km on long runs · the engine is warming up"})

    # ── PACE WATCH: roast the slowest established runner ────────
    pace_cand = [r for r in connected if r.avg_pace_s is not None and r.total_km >= 15]
    if len(pace_cand) >= 2:
        slowest = max(pace_cand, key=lambda r: r.avg_pace_s)
        items.append({"label": "PACE WATCH", "text": f"{first_name(slowest)} averaging {fmt_pace(slowest.avg_pace_s)}/km · brisk if you're carrying the shopping"})

    # ── STRUGGLE: lowest-km connected runner gets a nudge ───────
    if len(by_km) >= 3:
        weakest = by_km[-1]
        if 0 < weakest.total_km < 15:
            items.append({"label": "STRUGGLE", "text": f"{first_name(weakest)} at {weakest.total_km:.1f}km since 1 May · 'starts Monday' has become a season"})

    # ── GHOST WATCH ─────────────────────────────────────────────
    ghosts = [r for r in connected if r.days_since_last_run >= 5]
    if ghosts:
        g = max(ghosts, key=lambda r: r.days_since_last_run)
        items.append({"label": "GHOST", "text": f"{first_name(g)} hasn't logged a run in {g.days_since_last_run} days · the dashboard has a long memory"})

    # ── STREAK ──────────────────────────────────────────────────
    streakers = sorted([r for r in connected if r.streak >= 3], key=lambda r: r.streak, reverse=True)
    if streakers:
        s = streakers[0]
        items.append({"label": "ON FIRE", "text": f"{first_name(s)} on a {s.streak}-day streak · is now the time to mention rest days?"})

    # ── DAWN PATROL ─────────────────────────────────────────────
    early = sorted([r for r in connected if r.pre7am_runs_trailing_7d > 0], key=lambda r: r.pre7am_runs_trailing_7d, reverse=True)
    if early:
        e = early[0]
        run_s = "run" if e.pre7am_runs_trailing_7d == 1 else "runs"
        items.append({"label": "DAWN PATROL", "text": f"{first_name(e)} clocked {e.pre7am_runs_trailing_7d} pre-7am {run_s} in 7d · the rest of you were horizontal"})

    # ── LA SOIF: thirstiest runner over the trailing 7d ─────────
    # Honour-system drinks tracker (drinks.html → KV), not Strava. Top band
    # is capped, so a heavy night reads as "4+".
    soif_pool = [r for r in runners if r.has_drinks_log and r.drinks_7d > 0]
    if soif_pool:
        thirsty = max(soif_pool, key=lambda r: (r.drinks_7d, r.drink_days_7d))
        tot = fmt_drink_total(thirsty.drinks_7d, thirsty.drinks_7d_heavy)
        nights = thirsty.drink_days_7d
        night_s = "night" if nights == 1 else "nights"
        items.append({"label": "LA SOIF", "text": f"{first_name(thirsty)} logged {tot} drinks across {nights} {night_s} (7d) · the vineyards are calling"})

    # ── SQUAD TARGET PROGRESS ───────────────────────────────────
    if plan_status["connected_n"] > 0 and plan_status["total_target"] > 0:
        if plan_status["pct"] >= 100:
            items.append({"label": "TARGET", "text": f"Squad smashed this week's {plan_status['total_target']}km target — {plan_status['actual']}km logged · the oysters are earned"})
        elif plan_status["pct"] >= 70:
            items.append({"label": "TARGET", "text": f"Squad at {plan_status['actual']}/{plan_status['total_target']}km · the silent runners are letting the team down"})
        elif plan_status["pct"] >= 30:
            items.append({"label": "TARGET", "text": f"Squad at {plan_status['pct']}% of this week's target · respectable for amateurs, embarrassing for trainees"})
        else:
            items.append({"label": "TARGET", "text": f"Squad at {plan_status['pct']}% of target this week · the wine isn't going anywhere, but neither are you"})

    # ── GROUP PROJECTION ────────────────────────────────────────
    if group["predicted"] != "—":
        if "on for sub-4" in group["predicted_delta"]:
            items.append({"label": "PROJECTION", "text": f"Group on for sub-4 at {group['predicted']} · don't blow it now"})
        else:
            items.append({"label": "PROJECTION", "text": f"Group projection {group['predicted']} · {group['predicted_delta']} — but we still have months"})

    # ── KM MILESTONE ────────────────────────────────────────────
    if group["total_km"] >= 1000:
        items.append({"label": "MILESTONE", "text": f"Squad past {group['total_km']}km combined · London to Bordeaux on foot, ironically"})
    elif group["total_km"] >= 200:
        items.append({"label": "MILESTONE", "text": f"{group['total_km']}km combined · {int(group['total_km'] / 42.2)} marathons' worth between the squad"})

    # ── PERSONAL MILESTONE WATCH: who's nearest a round-number km total ─
    # Calls out runners within striking distance of 50/100/150/etc. km.
    # Picks up to two of these to avoid the marquee being all milestones.
    personal_milestones = [50, 100, 150, 200, 250, 300, 400, 500, 750, 1000]
    milestone_candidates = []
    for r in connected:
        if r.total_km <= 0:
            continue
        for m in personal_milestones:
            if r.total_km < m and (m - r.total_km) <= 12:
                milestone_candidates.append((m - r.total_km, r, m))
                break  # one milestone per runner
    milestone_candidates.sort(key=lambda x: x[0])  # closest first
    for gap, r, m in milestone_candidates[:2]:
        items.append({"label": "MILESTONE WATCH", "text": f"{first_name(r)} is {gap:.1f}km from {m}km · go and grab it"})

    # ── PACE LEADER: fastest avg pace over connected pile (10km+ data) ─
    pace_pool = [r for r in connected if r.avg_pace_s is not None and r.total_km >= 10]
    if len(pace_pool) >= 2:
        fastest = min(pace_pool, key=lambda r: r.avg_pace_s)
        items.append({"label": "PACE LEADER", "text": f"{first_name(fastest)} averaging {fmt_pace(fastest.avg_pace_s)}/km across the season · the gears are clean"})

    # ── BIG WEEK: who just had their biggest 7d block ──────────
    # Uses trailing_7d_km — top of that pile gets the shout.
    big_week_pool = [r for r in connected if r.trailing_7d_km > 0]
    if big_week_pool:
        bw = max(big_week_pool, key=lambda r: r.trailing_7d_km)
        if bw.trailing_7d_km >= 25:
            items.append({"label": "BIG WEEK", "text": f"{first_name(bw)} stacked {int(round(bw.trailing_7d_km))}km in the last 7 days · the calves know"})

    # ── ELEVATION CALLOUT: anyone who's done serious climbing ──
    if connected:
        climber = max(connected, key=lambda r: r.elevation_m)
        if climber.elevation_m >= 500:
            items.append({"label": "ASCENDED", "text": f"{first_name(climber)} climbed {int(round(climber.elevation_m))}m total · half a Snowdon and counting"})

    # ── LONGEST RUN OF THE WEEK ─────────────────────────────────
    # Find the single longest run in the trailing 7 days across the squad.
    week_long_best = None
    for r in connected:
        for a in r.activities:
            try:
                day = utc_to_uk(a["start_date"]).date()
            except Exception:
                continue
            if (today_uk - day).days > 7 or (today_uk - day).days < 0:
                continue
            km = (a.get("distance", 0) or 0) / 1000.0
            if km <= 0:
                continue
            if week_long_best is None or km > week_long_best[1]:
                week_long_best = (r, km)
    if week_long_best and week_long_best[1] >= 12:
        r, km = week_long_best
        items.append({"label": "LONG RUN OF THE WEEK", "text": f"{first_name(r)} clocked {km:.1f}km in a single session this week · respect"})

    # ── GROUP WoW: last completed week vs the week before ─────
    # Uses week_history (newest first). week_history[1] is last week
    # (Mon-Sun, completed); its summary.wow_pct compares to the week
    # before that. Banter-tier varies by magnitude — taper acknowledged,
    # slacking implied where appropriate.
    if len(week_history) >= 2:
        last_wk = week_history[1]
        pct = last_wk["summary"]["wow_pct"]
        last_total = last_wk["summary"]["total_km"]
        if pct is not None and last_total > 0:
            if pct >= 25:
                items.append({"label": "GROUP TREND", "text": f"Last week ({last_wk['date_range']}): {last_total}km, up {pct}% on the week before · the engine is firing"})
            elif pct >= 8:
                items.append({"label": "GROUP TREND", "text": f"Last week ({last_wk['date_range']}): {last_total}km, up {pct}% on the prior week · solid build"})
            elif pct >= -8:
                items.append({"label": "GROUP TREND", "text": f"Last week ({last_wk['date_range']}): {last_total}km, within a few percent of the prior week · consistency is also a flex"})
            elif pct >= -25:
                items.append({"label": "GROUP TREND", "text": f"Last week ({last_wk['date_range']}): {last_total}km, down {-pct}% · taper week, or some are slacking"})
            else:
                items.append({"label": "GROUP TREND", "text": f"Last week ({last_wk['date_range']}): {last_total}km, down {-pct}% on the prior week · either the plan said cutback or seven people are 'starting Monday'"})

    # ── PREDICTED FINISH ORDER preview ──────────────────────────
    finish_order = [r for r in connected if r.predicted_marathon_s]
    finish_order.sort(key=lambda r: r.predicted_marathon_s)
    if len(finish_order) >= 3:
        top3 = " · ".join(f"{i+1}. {first_name(r)}" for i, r in enumerate(finish_order[:3]))
        items.append({"label": "RACE-DAY", "text": f"If they ran Médoc today: {top3} · everyone else has weeks to fight for it"})

    # ── Evergreen Médoc lore — trimmed to the punchiest 3 ───────
    items.extend([
        {"label": "DRESS CODE", "text": "Costumes are mandatory · this is non-negotiable, yes it'll chafe"},
        {"label": "RUMOUR",     "text": "One château allegedly pours grand cru · find the queue, join it, lose 12 minutes"},
        {"label": "PSA",        "text": "If you can hold a conversation on your long run the pace is right · if you're explaining politics, you're going too easy"},
    ])

    # Defensive fallback (shouldn't fire — we always have countdown + phase)
    if not items:
        items = [{"label": "STATUS", "text": "The training files are being assembled. Stand by."}]

    return items


# ─── Weekly Recap ──────────────────────────────────────────────────────
def build_weekly_recap(runners: list[RunnerStats], week_history: list[dict], today_uk: date, display_names: dict, target_offset: int | None = None) -> dict | None:
    """
    Build a sociable, insight-led recap for a single Mon-Sun week.

    target_offset selects which week to recap:
      None (default): "this past week" — Sunday => current week, otherwise
                      the last completed Mon-Sun.
      0: current (in-progress) week
      1: last completed Mon-Sun
      2+: further back (used by the "Previous recaps" navigation)

    For offsets older than the default ("strictly historical"), insights
    that depend on CURRENT runner state (active streak, days-since-last-run,
    rolling 14d pace improvement) are skipped — they'd otherwise leak
    today's data into a recap of weeks past.
    """
    if not week_history:
        return None
    connected = [r for r in runners if r.connected]

    monday_uk = today_uk - timedelta(days=today_uk.weekday())
    is_sunday = today_uk.weekday() == 6
    default_offset = 0 if is_sunday else 1

    if target_offset is None:
        target_offset = default_offset
    if target_offset >= len(week_history):
        return None

    target = week_history[target_offset]
    prior  = week_history[target_offset + 1] if target_offset + 1 < len(week_history) else None
    week_start = monday_uk - timedelta(days=target_offset * 7)
    week_end = week_start + timedelta(days=6)
    prior_start = week_start - timedelta(days=7)
    prior_end   = week_start - timedelta(days=1)
    is_historical = target_offset > default_offset

    def km_in_window(r: RunnerStats, start: date, end: date) -> float:
        total = 0.0
        for a in r.activities:
            try:
                d = utc_to_uk(a["start_date"]).date()
            except Exception:
                continue
            if start <= d <= end:
                total += (a.get("distance", 0) or 0) / 1000.0
        return total

    def longest_in_window(r: RunnerStats, start: date, end: date) -> float:
        best = 0.0
        for a in r.activities:
            try:
                d = utc_to_uk(a["start_date"]).date()
            except Exception:
                continue
            if start <= d <= end:
                km = (a.get("distance", 0) or 0) / 1000.0
                if km > best:
                    best = km
        return best

    def drinks_in_window(r: RunnerStats, start: date, end: date) -> tuple[int, int, bool]:
        """(total drinks, session-days, heavy) a runner self-logged in
        [start, end]. `heavy` marks any day at the capped top band (Bourré),
        so totals render as "N+". Honour-system data; by_day is already
        clamped to the drink window, so weeks before the tracker opened
        naturally return (0, 0, False)."""
        total = 0
        days = 0
        heavy = False
        for k, v in (r.drinks_by_day or {}).items():
            try:
                d = date.fromisoformat(k)
            except (ValueError, TypeError):
                continue
            if start <= d <= end and v > 0:
                total += v
                days += 1
                if v >= DRINK_HEAVY_VALUE:
                    heavy = True
        return total, days, heavy

    def first_name(r): return display_names.get(r.cfg["id"]) or r.cfg["name"].split()[0]

    def emit(tag, name, rest, score):
        """Build an insight dict with both HTML and plain (WhatsApp) text."""
        safe_name = html_escape(name)
        safe_rest = html_escape(rest)
        return {
            "tag":   tag,
            "html":  f"<strong>{safe_name}</strong> {safe_rest}",
            "plain": f"*{name}* {rest}",
            "score": score,
        }

    last_wk_km = {r.cfg["id"]: km_in_window(r, week_start, week_end) for r in connected}
    prior_wk_km = {r.cfg["id"]: km_in_window(r, prior_start, prior_end) for r in connected}

    def emit_cat(category, tag, name, rest, score):
        """Like emit() but also tags with a category for cap-by-category dedupe."""
        ins = emit(tag, name, rest, score)
        ins["category"] = category
        return ins

    def squad_insight(category, tag, html_text, plain_text, score):
        return {
            "category": category,
            "tag":      tag,
            "html":     html_text,
            "plain":    plain_text,
            "score":    score,
        }

    insights: list[dict] = []

    # ── TOP DOG OF THE WEEK ───────────────────────────────────────
    # Highest km in the target Mon-Sun window. Boosted score if the lead
    # changed vs the prior week (different runner topped the prior week).
    top_pool = sorted(
        [(r, last_wk_km[r.cfg["id"]]) for r in connected if last_wk_km[r.cfg["id"]] > 0],
        key=lambda x: -x[1],
    )
    prior_top_pool = sorted(
        [(r, prior_wk_km[r.cfg["id"]]) for r in connected if prior_wk_km[r.cfg["id"]] > 0],
        key=lambda x: -x[1],
    )
    if top_pool:
        top_r, top_km = top_pool[0]
        prior_top_r = prior_top_pool[0][0] if prior_top_pool else None
        if prior_top_r and prior_top_r.cfg["id"] != top_r.cfg["id"]:
            insights.append(emit_cat("top_dog", "👑", first_name(top_r),
                f"took Top Dog with {top_km:.0f}km — knocking {first_name(prior_top_r)} off the perch.", 100))
        else:
            insights.append(emit_cat("top_dog", "👑", first_name(top_r),
                f"holds Top Dog with {top_km:.0f}km this week. Hard to dislodge.", 88))

    # ── NEW JOINER — anyone whose first ever logged run is in this window.
    # Distinct from a comeback because they have no prior activities.
    for r in connected:
        if not r.activities:
            continue
        try:
            earliest = min(utc_to_uk(a["start_date"]).date() for a in r.activities)
        except Exception:
            continue
        if week_start <= earliest <= week_end:
            insights.append(emit_cat("joiner", "🎉", first_name(r),
                "logged their first runs this week — welcome aboard.", 96))

    # ── RANK MOVEMENT — biggest climber on this week's km ranking.
    def rank_dict(km_dict):
        ranked = sorted([(rid, km) for rid, km in km_dict.items() if km > 0],
                        key=lambda x: -x[1])
        return {rid: idx + 1 for idx, (rid, _) in enumerate(ranked)}
    cur_rank  = rank_dict(last_wk_km)
    prev_rank = rank_dict(prior_wk_km)
    runner_by_id = {r.cfg["id"]: r for r in connected}
    rank_changes = []
    for rid, cur in cur_rank.items():
        prev = prev_rank.get(rid)
        if prev is not None and prev != cur:
            rank_changes.append((runner_by_id[rid], prev - cur, prev, cur))  # delta>0 = climbed
    climbers = sorted([c for c in rank_changes if c[1] >= 2], key=lambda x: -x[1])
    if climbers:
        r, delta, prev, cur = climbers[0]
        insights.append(emit_cat("rank", "⬆", first_name(r),
            f"climbed {delta} places to #{cur} — was #{prev} a week ago. The chase is on.", 94))
    fallers = sorted([c for c in rank_changes if c[1] <= -2], key=lambda x: x[1])
    if fallers:
        r, delta, prev, cur = fallers[0]
        insights.append(emit_cat("rank", "⬇", first_name(r),
            f"dropped {-delta} places to #{cur} — was #{prev} a week ago. The leaderboard moves on without you.", 78))

    # ── COMEBACK — runner who'd been quiet ≥6 days then ran this week.
    comeback_candidates = []
    for r in connected:
        if last_wk_km[r.cfg["id"]] <= 0:
            continue
        # Skip new joiners (no prior activity at all)
        pre_week_dates = []
        for a in r.activities:
            try:
                d = utc_to_uk(a["start_date"]).date()
            except Exception:
                continue
            if d < week_start:
                pre_week_dates.append(d)
        if not pre_week_dates:
            continue
        last_pre = max(pre_week_dates)
        # First run in target week
        in_week_dates = []
        for a in r.activities:
            try:
                d = utc_to_uk(a["start_date"]).date()
            except Exception:
                continue
            if week_start <= d <= week_end:
                in_week_dates.append(d)
        if not in_week_dates:
            continue
        first_in = min(in_week_dates)
        gap = (first_in - last_pre).days
        if gap >= 6:
            comeback_candidates.append((r, gap))
    comeback_candidates.sort(key=lambda x: -x[1])
    if comeback_candidates:
        r, gap = comeback_candidates[0]
        insights.append(emit_cat("comeback", "🔁", first_name(r),
            f"is back — first run in {gap} days. The chase is rejoined.", 90))

    # ── STREAK MILESTONE — hit a notable streak number THIS WEEK.
    # We approximate by checking current streak (as of today) against milestones.
    # Only fires for the default/current recap; historical recaps would
    # incorrectly use today's streak as a stand-in for that week's streak.
    if not is_historical:
        streak_milestones = {5, 7, 10, 14, 21, 30}
        streakers = sorted([r for r in connected if r.streak >= 3], key=lambda r: -r.streak)
        if streakers:
            s = streakers[0]
            if s.streak in streak_milestones:
                insights.append(emit_cat("streak", "🔥", first_name(s),
                    f"hit a {s.streak}-day streak. La Flamme.", 86))
            elif s.streak >= 5:
                insights.append(emit_cat("streak", "🔥", first_name(s),
                    f"riding a {s.streak}-day streak — La Flamme leader.", 64))

    # ── PERSONAL BEST LONG RUN this week — runner's longest-ever single run.
    pb_candidates = []
    for r in connected:
        wk_longest = longest_in_window(r, week_start, week_end)
        if wk_longest >= 10 and abs(wk_longest - r.longest_km) < 0.05:
            pb_candidates.append((r, wk_longest))
    pb_candidates.sort(key=lambda x: -x[1])
    if pb_candidates:
        r, dist = pb_candidates[0]
        insights.append(emit_cat("pb", "🥇", first_name(r),
            f"set a personal-best long run: {dist:.1f}km. The engine is warming.", 92))

    # ── MILESTONE(S) — consolidate ALL milestones crossed this week into ONE
    # insight. Three people crossing 100km this week shouldn't take three slots.
    km_milestones = [50, 100, 150, 200, 250, 300, 400, 500, 750, 1000]
    crossed = []
    for r in connected:
        last_km = last_wk_km[r.cfg["id"]]
        pre_total = r.total_km - last_km
        for m in km_milestones:
            if pre_total < m and r.total_km >= m:
                crossed.append((r, m))
                break
    crossed.sort(key=lambda x: -x[1])  # highest milestone first
    if crossed:
        if len(crossed) == 1:
            r, m = crossed[0]
            insights.append(emit_cat("milestone", "🎖", first_name(r),
                f"crossed {m}km total this week. Onwards.", 84))
        else:
            # Multi-milestone line, mentioning everyone
            pieces = [f"{first_name(r)} ({m}km)" for r, m in crossed[:4]]
            joined = ", ".join(pieces)
            text_plain = f"Milestone party — {joined} all crossed thresholds this week."
            text_html  = f"Milestone party — {html_escape(joined)} all crossed thresholds this week."
            insights.append(squad_insight("milestone", "🎖", text_html, text_plain, 88))

    # ── SIBLING RIVALRY status — current Illig pecking order with gaps.
    illigs = [r for r in connected if r.cfg.get("is_groom") or r.cfg.get("is_brother")]
    if len(illigs) >= 2:
        illigs.sort(key=lambda r: -r.total_km)
        louis = next((r for r in connected if r.cfg.get("is_groom")), None)
        top_illig = illigs[0]
        if louis and top_illig.cfg["id"] == louis.cfg["id"]:
            # Louis leading
            second = illigs[1]
            gap_to_second = top_illig.total_km - second.total_km
            insights.append(emit_cat("rivalry", "👯", "Louis",
                f"holds the Illig crown · {first_name(second)} is {gap_to_second:.0f}km back. Brother in pursuit.", 78))
        elif louis:
            # A brother leading
            gap_to_louis = top_illig.total_km - louis.total_km
            insights.append(emit_cat("rivalry", "👯", first_name(top_illig),
                f"leads the Illigs by {gap_to_louis:.0f}km. Louis, the groom needs to defend his crown.", 82))

    # ── L'AURORE OF THE WEEK — pre-7am leader for the target Mon-Sun window.
    pre7am_counts = {}
    for r in connected:
        cnt = 0
        for a in r.activities:
            try:
                t = utc_to_uk(a["start_date"])
            except Exception:
                continue
            if week_start <= t.date() <= week_end and t.hour < 7:
                cnt += 1
        if cnt > 0:
            pre7am_counts[r.cfg["id"]] = (r, cnt)
    pre7am_sorted = sorted(pre7am_counts.values(), key=lambda x: -x[1])
    if pre7am_sorted and pre7am_sorted[0][1] >= 2:
        r, n = pre7am_sorted[0]
        run_s = "run" if n == 1 else "runs"
        insights.append(emit_cat("laurore", "🌅", first_name(r),
            f"is L'Aurore this week — {n} pre-7am {run_s}. The rest of you were horizontal.", 72))

    # ── BIGGEST WEEKLY JUMP — significant km increase on prior week.
    jump_pool = []
    for r in connected:
        cur = last_wk_km[r.cfg["id"]]
        pre = prior_wk_km[r.cfg["id"]]
        if pre >= 3 and cur - pre >= 8:
            jump_pool.append((r, cur, pre, cur - pre))
    jump_pool.sort(key=lambda x: -x[3])
    if jump_pool:
        r, cur, pre, delta = jump_pool[0]
        insights.append(emit_cat("jump", "📈", first_name(r),
            f"stepped up — {cur:.0f}km this week vs {pre:.0f}km the week before. Momentum.", 76))

    # ── BIGGEST WEEKLY DROP — significant km decrease.
    drop_pool = []
    for r in connected:
        cur = last_wk_km[r.cfg["id"]]
        pre = prior_wk_km[r.cfg["id"]]
        if pre >= 15 and (pre - cur) >= 10:
            drop_pool.append((r, cur, pre, pre - cur))
    drop_pool.sort(key=lambda x: -x[3])
    if drop_pool:
        r, cur, pre, delta = drop_pool[0]
        insights.append(emit_cat("drop", "📉", first_name(r),
            f"dropped to {cur:.0f}km from {pre:.0f}km the week before. Taper, illness, or selective memory?", 70))

    # ── PACE GLOW-UP — biggest pace improvement on long runs.
    # Pace improvement is rolling-14d-as-of-today, so it doesn't apply to
    # historical weeks (it'd describe today's improvement, not that week's).
    if not is_historical:
        glow = [r for r in connected
                if r.pace_improvement_s is not None and r.pace_improvement_s < -5]
        glow.sort(key=lambda r: r.pace_improvement_s)
        if glow:
            g = glow[0]
            insights.append(emit_cat("pace", "⚡", first_name(g),
                f"dropped {fmt_pace_delta(g.pace_improvement_s)}/km on long runs. Proper progress.", 68))

    # ── DARK HORSE — non-Illig outside the top 3 who still logged 20km+.
    if len(top_pool) >= 4:
        top_three_ids = {r.cfg["id"] for r, _ in top_pool[:3]}
        dark = [(r, km) for r, km in top_pool[3:]
                if km >= 20
                and not r.cfg.get("is_groom") and not r.cfg.get("is_brother")
                and r.cfg["id"] not in top_three_ids]
        if dark:
            r, km = dark[0]
            insights.append(emit_cat("dark_horse", "🧱", first_name(r),
                f"quietly stacking {km:.0f}km outside the top three · the engine you didn't see coming.", 60))

    # ── GHOST WATCH — biggest current gap among connected runners.
    # days_since_last_run is "as of today" — would be misleading for
    # a historical recap.
    if not is_historical:
        ghost_pool = [(r, r.days_since_last_run) for r in connected if r.days_since_last_run >= 5]
        ghost_pool.sort(key=lambda x: -x[1])
        if ghost_pool:
            ghost_r, days = ghost_pool[0]
            insights.append(emit_cat("ghost", "👻", first_name(ghost_r),
                f"hasn't logged a run in {days} days. The dashboard remembers.", 58))

    # ── SILENT DAY — was there any day nobody ran across the whole squad?
    silent_days = []
    for i in range(7):
        d = week_start + timedelta(days=i)
        any_run = False
        for r in connected:
            for a in r.activities:
                try:
                    if utc_to_uk(a["start_date"]).date() == d:
                        any_run = True
                        break
                except Exception:
                    continue
            if any_run:
                break
        if not any_run:
            silent_days.append(d.strftime("%A"))
    if silent_days and len(silent_days) <= 2:
        silent_str = " and ".join(silent_days)
        insights.append(squad_insight("silent",
            "🤫",
            f"Squad was silent on {html_escape(silent_str)} — not a single run logged.",
            f"Squad was silent on {silent_str} — not a single run logged.",
            54))

    # ── LA SOIF OF THE WEEK — thirstiest self-logged drinker in the window.
    # Honour-system (drinks.html → KV); spans all runners, not just connected.
    # by_day is clamped to the drink window, so weeks before the tracker
    # opened stay bone-dry and this simply doesn't fire.
    soif_pool = []
    squad_heavy = False
    for r in runners:
        t, d, h = drinks_in_window(r, week_start, week_end)
        if t > 0:
            soif_pool.append((r, t, d, h))
            if h:
                squad_heavy = True
    soif_pool.sort(key=lambda x: (-x[1], -x[2]))
    if soif_pool:
        soif_r, soif_drinks, soif_days, soif_heavy = soif_pool[0]
        soif_tot = fmt_drink_total(soif_drinks, soif_heavy)
        night_s = "night" if soif_days == 1 else "nights"
        insights.append(emit_cat("soif", "🍷", first_name(soif_r),
            f"led the bar — {soif_tot} drinks across {soif_days} {night_s}. La Soif salutes you.", 72))
        # Squad tally, only when more than the leader contributed — otherwise
        # it just restates the individual line above.
        if len(soif_pool) > 1:
            squad_drinks = sum(t for _, t, _, _ in soif_pool)
            squad_sessions = sum(d for _, _, d, _ in soif_pool)
            squad_tot = fmt_drink_total(squad_drinks, squad_heavy)
            insights.append(squad_insight("drinks_squad", "🍻",
                f"Squad sank <strong>{squad_tot}</strong> drinks across {squad_sessions} thirsty sessions this week.",
                f"Squad sank {squad_tot} drinks across {squad_sessions} thirsty sessions this week.",
                50))

    # Squad WoW one-liner (always rendered separately at the top of the recap).
    squad_wow = None
    last_total = target["summary"]["total_km"]
    if prior and prior["summary"]["total_km"] > 0:
        prior_total = prior["summary"]["total_km"]
        pct = round((last_total - prior_total) / prior_total * 100)
        if pct >= 25:
            squad_wow = f"Squad clocked {last_total}km — up {pct}% on the prior week. The engine is firing."
        elif pct >= 8:
            squad_wow = f"Squad clocked {last_total}km — up {pct}% on the prior week. Solid build."
        elif pct >= -8:
            squad_wow = f"Squad clocked {last_total}km — within a few percent of the prior week. Steady."
        elif pct >= -25:
            squad_wow = f"Squad clocked {last_total}km — down {-pct}% on the prior week. Taper or collective amnesia?"
        else:
            squad_wow = f"Squad clocked {last_total}km — down {-pct}% on the prior week. Either the plan said cutback or seven people are 'starting Monday'."
    elif last_total > 0:
        squad_wow = f"Squad clocked {last_total}km this week across {target['summary']['sessions']} sessions."

    # Score-sort, dedupe (one insight per runner max), keep top 5.
    insights.sort(key=lambda x: -x["score"])
    picked: list[dict] = []
    seen_names: set[str] = set()
    for insight in insights:
        # Extract a rough "first word inside bold" to identify the runner
        # — squad-level insights without a bolded name pass freely.
        m = re.search(r"\*([^*]+)\*", insight["plain"])
        runner_token = m.group(1) if m else None
        if runner_token and runner_token in seen_names:
            continue
        picked.append(insight)
        if runner_token:
            seen_names.add(runner_token)
        if len(picked) >= 5:
            break

    # Missing-runners callout.
    not_connected = [r for r in runners if not r.connected]
    missing_count = len(not_connected)
    missing_names = [r.cfg["name"] for r in not_connected]
    # Clean placeholder brackets for display: "[Brother 2]" -> "Brother 2"
    missing_clean = [n.replace("[", "").replace("]", "") for n in missing_names]

    # Week label.
    date_range = f"{week_start.day} {week_start.strftime('%b')} – {week_end.day} {week_end.strftime('%b')}"
    week_num = max(1, ((week_end - TRAINING_START).days // 7) + 1)
    headline = f"Médoc Week {week_num} · {date_range}"

    # Build the WhatsApp share text (plain, with *asterisk* bold).
    share_lines: list[str] = [f"🍷 {headline}"]
    if squad_wow:
        share_lines += ["", squad_wow]
    if picked:
        share_lines.append("")
        for insight in picked:
            share_lines.append(f"{insight['tag']} {insight['plain']}")
    if missing_count > 0:
        share_lines += ["", f"🚧 Still {missing_count} of 13 not connected — {', '.join(missing_clean[:6])}{', +more' if missing_count > 6 else ''}. Pass the connect link on — every extra runner is more rivalry and less wooden-spoon risk."]
    share_lines += [
        "",
        "Desktop: https://fbmedoc.github.io/Marathon-Du-Medoc-Dashboard/",
        "Mobile:  https://fbmedoc.github.io/Marathon-Du-Medoc-Dashboard/mobile.html",
        "Connect: https://fbmedoc.github.io/Marathon-Du-Medoc-Dashboard/connect.html  (code: medoc26)",
        "",
        "On y va, doucement.",
    ]
    share_text = "\n".join(share_lines)
    share_url  = "https://wa.me/?text=" + url_quote(share_text)

    return {
        "headline":      headline,
        "date_range":    date_range,
        "week_label":    f"Week {week_num}",
        "is_historical": is_historical,
        "target_offset": target_offset,
        "squad_wow":     squad_wow,
        "insights":      picked,
        "missing_count": missing_count,
        "missing_names": missing_clean,
        "share_text":    share_text,
        "share_url":     share_url,
    }


def build_recap_history(runners: list[RunnerStats], week_history: list[dict], today_uk: date, display_names: dict, max_recaps: int = 4) -> list[dict]:
    """
    Generate a list of weekly recaps, newest first, so the dashboard can
    let the user scroll back through previous weeks. List[0] is the default
    "this past week" recap; List[1] is the week before that, etc.

    Empty recaps (e.g. a week before training started where there's no
    sensible content) are filtered out.
    """
    monday_uk = today_uk - timedelta(days=today_uk.weekday())
    is_sunday = today_uk.weekday() == 6
    default_offset = 0 if is_sunday else 1

    recaps: list[dict] = []
    for i in range(max_recaps):
        offset = default_offset + i
        # Stop if this week starts before training began (no meaningful recap)
        week_start = monday_uk - timedelta(days=offset * 7)
        if week_start < TRAINING_START - timedelta(days=6):
            break
        r = build_weekly_recap(runners, week_history, today_uk, display_names, target_offset=offset)
        if not r:
            continue
        # Skip "empty" recaps with no insights and no meaningful squad WoW.
        if not r["insights"] and not r["squad_wow"]:
            continue
        recaps.append(r)
    return recaps


# ─── Training plan: pick the current week & generate workouts ──────────
def weeks_to_race(today_uk: date) -> int:
    """Whole weeks from today to race day. 0 = race week. Clamped to [0, 18]."""
    days = (RACE_DATE - today_uk).days
    return max(0, min(18, days // 7))


def current_week_plan(today_uk: date) -> dict:
    """The plan entry for the current week, with derived day-by-day workouts."""
    w = weeks_to_race(today_uk)
    plan = dict(MARATHON_PLAN[w])   # copy
    plan["weeks_out"] = w
    plan["week_num"]  = 18 - w + 1   # week 1..19 (race week = 19)
    plan["workouts"]  = generate_workouts(plan)
    return plan


def generate_workouts(week_plan: dict) -> list[dict]:
    """Derive a 7-day workout list from a week's target volume + key session."""
    long_km     = week_plan["long_km"]
    key_session = week_plan["key_session"]
    target      = week_plan["target_km"]

    # Race week gets a bespoke template
    if isinstance(long_km, str):  # "RACE"
        return [
            {"day": "Mon", "type": "Rest",     "value": "or travel",     "kind": "rest"},
            {"day": "Tue", "type": "Easy",     "value": "3km Z2",        "kind": "normal"},
            {"day": "Wed", "type": "Strides",  "value": "3km + 4×100m",  "kind": "normal"},
            {"day": "Thu", "type": "Shakeout", "value": "2–3km easy",    "kind": "normal"},
            {"day": "Fri", "type": "Rest",     "value": "carb up",       "kind": "rest"},
            {"day": "Sat", "type": "Shakeout", "value": "2km easy",      "kind": "normal"},
            {"day": "Sun", "type": "RACE DAY", "value": "42.2km",        "kind": "long"},
        ]

    # Roughly: 5 running days (Mon recovery, Tue quality, Thu easy, Fri easy, Sun long).
    # Quality day counted as ~8km of the weekly total; the rest spread across easy days.
    easy_budget = max(0, target - long_km - 8)
    mon_km = max(3, round(easy_budget * 0.25))
    thu_km = max(4, round(easy_budget * 0.40))
    fri_km = max(3, easy_budget - mon_km - thu_km)
    if fri_km < 3:
        fri_km = 3

    return [
        {"day": "Mon", "type": "Recovery", "value": f"{mon_km}km easy",        "kind": "normal"},
        {"day": "Tue", "type": "Quality",  "value": key_session,               "kind": "normal"},
        {"day": "Wed", "type": "Rest",     "value": "or yoga",                 "kind": "rest"},
        {"day": "Thu", "type": "Easy",     "value": f"{thu_km}km Z2",          "kind": "normal"},
        {"day": "Fri", "type": "Easy",     "value": f"{fri_km}km Z2",          "kind": "normal"},
        {"day": "Sat", "type": "Rest",     "value": "or 30' walk",             "kind": "rest"},
        {"day": "Sun", "type": "Long Run", "value": f"{long_km}km @ easy effort","kind": "long"},
    ]


def phases_with_current(today_uk: date) -> list[dict]:
    """The four high-level phases with the current one flagged."""
    cur_num = current_week_plan(today_uk)["phase_num"]
    out = []
    for num in ["I", "II", "III", "IV"]:
        meta = PHASE_META[num]
        out.append({
            "num":         num,
            "name":        meta["name"],
            "when":        meta["when"],
            "desc_long":   meta["desc_long"],
            "desc_short":  meta["desc_short"],
            "is_current":  num == cur_num,
        })
    return out


def group_plan_status(runners: list[RunnerStats], week_plan: dict) -> dict:
    """
    How the squad is tracking against this week's *realistic* group target.
    The realistic target is GROUP_TARGET_RATIO (75%) of the full plan total
    — assumes not every runner hits 100% every week.
    """
    connected = [r for r in runners if r.connected]
    connected_n = len(connected)
    target_per = week_plan["target_km"]
    full_target      = target_per * connected_n
    realistic_target = full_target * GROUP_TARGET_RATIO
    actual = sum(r.week_km for r in connected)

    if realistic_target > 0:
        pct = (actual / realistic_target) * 100
    else:
        pct = 0

    if pct >= 100:
        status, tone = "smashing the target", "up"
    elif pct >= 80:
        status, tone = "on target", "up"
    elif pct >= 50:
        status, tone = "behind target", "flat"
    elif pct > 0:
        status, tone = f"well behind ({int(round(pct))}%)", "down"
    else:
        status, tone = "no runs yet this week", "flat"

    return {
        "target_per_runner": target_per,
        "long_km":           week_plan["long_km"],
        "full_target":       round(full_target),
        "total_target":      round(realistic_target),   # the realistic 75% one
        "actual":            round(actual),
        "pct":               int(round(pct)),
        "status":            status,
        "tone":              tone,
        "connected_n":       connected_n,
        "target_ratio_pct":  int(GROUP_TARGET_RATIO * 100),
    }


# ─── Build the leaderboard rows that the templates iterate over ────────
def make_runner_rows(runners: list[RunnerStats]) -> list[dict]:
    """
    Everyone — including the groom — is ranked on the same scale: connected
    runners first (sorted by rolling 7-day km descending — rewards current
    activity, not a one-off massive week back in May), disconnected runners
    last. Total km is still shown in each runner's meta line so the
    cumulative number isn't lost. Tiebreaker on total km, then name.
    The groom keeps his ★ tag in the UI but earns his position like anyone else.
    """
    ordered = sorted(
        runners,
        key=lambda r: (not r.connected, -r.trailing_7d_km, -r.total_km, r.cfg["name"]),
    )

    # Progress-bar fill: relative to top runner's predicted_medoc_s.
    # We want the *fastest* (lowest seconds) to be 100%.
    valid_preds = [r.predicted_medoc_s for r in ordered if r.predicted_medoc_s]
    fastest = min(valid_preds) if valid_preds else None

    rows = []
    for i, r in enumerate(ordered, start=1):
        if r.connected and r.total_km > 0:
            meta_bits = [f"{int(round(r.total_km))}km total"]
            if r.streak > 0:
                meta_bits.append(f"{r.streak}-day streak")
            meta = " · ".join(meta_bits)
            predicted = fmt_hms(r.predicted_medoc_s)
            sub4 = sub4_delta_label(r.predicted_medoc_s)
            if fastest and r.predicted_medoc_s:
                pct = max(20, int(round((fastest / r.predicted_medoc_s) * 100)))
            else:
                pct = 0
        elif r.connected:
            meta = "no runs since 1 May"
            predicted = "—"
            sub4 = "—"
            pct = 0
        else:
            meta = "not connected — visit /connect.html"
            predicted = "—"
            sub4 = "—"
            pct = 0

        # Week-over-week momentum (trailing 7d vs the 7 before): glanceable
        # arrow under the Last-7d figure. Shifts under 1 km are noise — show
        # nothing rather than a fluttering arrow.
        shift = r.wow_shift_km if r.connected else None
        if shift is None or abs(shift) < 1.0:
            trend_dir, trend_label = "", ""
        elif shift > 0:
            trend_dir, trend_label = "up", f"▲ +{shift:.0f} km"
        else:
            trend_dir, trend_label = "down", f"▼ −{abs(shift):.0f} km"

        rows.append({
            "rank":             i,
            "rank_roman":       to_roman(i),
            "name":             r.cfg["name"],
            "avatar":           r.cfg.get("avatar"),   # optional path to runner photo
            "tag":              r.cfg.get("tag"),
            "is_groom":         bool(r.cfg.get("is_groom")),
            "is_brother":       bool(r.cfg.get("is_brother")),
            "connected":        r.connected,
            "meta":             meta,
            "week_km":          f"{r.week_km:.0f}" if r.connected and r.week_km > 0 else "—",
            "trailing_7d_km":   f"{r.trailing_7d_km:.0f}" if r.connected and r.trailing_7d_km > 0 else "—",
            "trend_dir":        trend_dir,
            "trend_label":      trend_label,
            "total_km":         f"{int(round(r.total_km))}" if r.connected and r.total_km > 0 else "—",
            "longest_km":       f"{r.longest_km:.1f}" if r.connected and r.longest_km > 0 else "—",
            "predicted":        predicted,
            "sub4":             sub4,
            "progress_pct":     pct,
            "streak":           r.streak,
            "avg_pace":         fmt_pace(r.avg_pace_s),
            "avg_hr":           f"{int(round(r.avg_hr))}" if r.avg_hr else "—",
            "elevation_m":      int(round(r.elevation_m)),
            # Drinks (trailing 7d). "—" when the runner has never logged.
            "drinks_7d":        str(r.drinks_7d) if r.has_drinks_log else "—",
            "drinks_dot":       drinks_week_dot(r.drinks_7d) if r.has_drinks_log else "b0",
            "has_drinks_log":   r.has_drinks_log,
            "today_band":       r.today_band,
            "drink_dots":       r.drink_dots if r.drink_dots else [""] * 14,
        })
    return rows


# ─── WhatsApp share text ───────────────────────────────────────────────
def build_share_text(
    runners: list[RunnerStats],
    rows: list[dict],
    trailing_7d: dict,
    days_until: int,
    today_uk: date,
) -> str:
    """Plain-text squad update for the dashboard's copy-to-clipboard button.
    Built server-side so the page JS just copies a ready-made string."""
    lines = [
        f"🍷 MÉDOC 26 · SQUAD UPDATE · {today_uk.strftime('%a %d %b')}",
        f"🏁 {days_until} days to race day",
        "",
        f"Last 7 days: {trailing_7d['total_km']} km · {trailing_7d['sessions']} sessions (squad)",
    ]
    podium = ["🥇", "🥈", "🥉"]
    leaders = [r for r in rows if r["connected"] and r["trailing_7d_km"] != "—"]
    for medal, r in zip(podium, leaders):
        lines.append(f"{medal} {r['name']} — {r['trailing_7d_km']} km")
    soif_pool = [r for r in runners if r.has_drinks_log and r.drinks_7d > 0]
    if soif_pool:
        thirsty = max(soif_pool, key=lambda r: (r.drinks_7d, r.drink_days_7d))
        tot = fmt_drink_total(thirsty.drinks_7d, thirsty.drinks_7d_heavy)
        lines += ["", f"🍷 La Soif: {thirsty.cfg['name']} — {tot} drinks (7d)"]
    lines += ["", "Full board → https://fbmedoc.github.io/Marathon-Du-Medoc-Dashboard/"]
    return "\n".join(lines)


# ─── Main render ───────────────────────────────────────────────────────
def main() -> None:
    print(f"Le Marathon Du Médoc 26 dashboard build · {datetime.utcnow().isoformat()} UTC")

    runners_cfg = json.loads(RUNNERS_FILE.read_text(encoding="utf-8"))
    today_uk = datetime.now(UK_TZ).date()
    days_until = max(0, (RACE_DATE - today_uk).days)
    token_cache = load_token_cache()
    cache_hits_before = sum(1 for v in token_cache.values() if v.get("access_token"))

    # Self-selected drinks log (graceful {} if the Worker/KV isn't reachable).
    drinks_map = fetch_drinks()
    if drinks_map:
        print(f"Drinks: loaded logs for {len(drinks_map)} runner(s).")

    # Crunch per-runner stats
    shared_tokens = fetch_shared_tokens()

    runners: list[RunnerStats] = []
    for cfg in runners_cfg:
        try:
            rs = compute_runner(cfg, today_uk, token_cache, shared_tokens)
        except Exception as e:
            print(f"[{cfg['id']}] unexpected error: {e}", file=sys.stderr)
            rs = RunnerStats(cfg=cfg)
        # Attach drinks to every runner (logged independently of Strava).
        try:
            attach_drinks(rs, drinks_map, today_uk)
        except Exception as e:
            print(f"[{cfg['id']}] drinks attach failed: {e}", file=sys.stderr)
        runners.append(rs)

    save_token_cache(token_cache)
    print(f"Token cache: {cache_hits_before} entries restored, {len(token_cache)} entries saved.")

    # Order by total_km for award lookups (groom-first ordering happens in make_runner_rows)
    by_total = sorted(runners, key=lambda r: r.total_km, reverse=True)

    # Disambiguation map: "Dan C." / "Danny A." when first names clash.
    # Reused across ticker, newsflash, and weekly recap.
    display_names = build_display_names(runners)

    rows         = make_runner_rows(runners)
    groom_row    = next((r for r in rows if r["is_groom"]), None)
    group        = build_group_stats(runners, today_uk)
    trailing_7d  = build_trailing_7d(runners, today_uk)
    week_grid, week_summary = build_week_grid(runners, today_uk)
    # Build enough week history to power the recap navigation (4 weeks back),
    # plus an extra trailing week so each historical recap has a "prior week"
    # for its WoW comparison.
    week_history = build_week_history(runners, today_uk, num_weeks=6)
    awards       = build_awards(runners, by_total)
    mini_boards  = build_mini_boards(runners, today_uk)
    activity_ticker = build_activity_ticker(runners, display_names, limit=8)
    phases       = phases_with_current(today_uk)
    this_week    = current_week_plan(today_uk)
    plan_status  = group_plan_status(runners, this_week)
    news_flash   = build_newsflash(runners, group, days_until, this_week, plan_status, today_uk, week_history, display_names)
    weekly_recap = build_weekly_recap(runners, week_history, today_uk, display_names)
    recap_history = build_recap_history(runners, week_history, today_uk, display_names, max_recaps=4)
    share_text   = build_share_text(runners, rows, trailing_7d, days_until, today_uk)

    synced_uk = datetime.now(UK_TZ).strftime("%H:%M %Z")

    ctx: dict[str, Any] = {
        "race_date":      RACE_DATE.isoformat(),
        "race_date_label":RACE_DATE_LABEL,
        "days_until":     days_until,
        "prologue":       PROLOGUE_TEXT,
        "footer_quote":   FOOTER_QUOTE,
        "synced_at":      synced_uk,
        "current_phase":  next((p for p in phases if p["is_current"]), phases[1]),
        "phases":         phases,
        "this_week":      this_week,
        "plan_status":    plan_status,
        "news_flash":      news_flash,
        "activity_ticker": activity_ticker,
        "weekly_recap":    weekly_recap,
        "recap_history":   recap_history,
        "runners":         rows,
        "groom_row":       groom_row,
        "group":           group,
        "trailing_7d":     trailing_7d,
        "week_grid":       week_grid,
        "week_summary":    week_summary,
        "week_history":    week_history,
        "awards":          awards,
        "mini_boards":     mini_boards,
        "medoc_facts":     MEDOC_FACTS,
        "plan_targets":    PLAN_TARGETS,
        "drinks_window_label": DRINK_WINDOW_LBL,
        "drinks_any":      any(r.has_drinks_log for r in runners),
        "share_text":      share_text,
    }

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["comma"] = lambda v: f"{int(round(float(v))):,}" if v not in (None, "—") else "—"
    env.filters["signed_pct"] = lambda v: (f"+{v}" if v is not None and v >= 0 else (str(v) if v is not None else "—"))

    INDEX_OUT.write_text(env.get_template("desktop.html.j2").render(**ctx), encoding="utf-8")
    MOBILE_OUT.write_text(env.get_template("mobile.html.j2").render(**ctx),  encoding="utf-8")
    print(f"Wrote {INDEX_OUT.name} and {MOBILE_OUT.name}.")


if __name__ == "__main__":
    main()
