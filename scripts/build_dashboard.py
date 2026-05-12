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
import sys
import time as time_module
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from html import escape as html_escape
from pathlib import Path
from typing import Any

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
UK_TZ              = pytz.timezone("Europe/London")
SUB_4_SECONDS      = 4 * 3600                # reference time for "to sub-4" deltas
MEDOC_PENALTY      = 1.10                    # Médoc time = marathon × this (wine stops!)
WEEK_DAYS          = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

STRAVA_CLIENT_ID     = os.environ.get("STRAVA_CLIENT_ID", "")
STRAVA_CLIENT_SECRET = os.environ.get("STRAVA_CLIENT_SECRET", "")

# Non-Strava bits the dashboard wants. Edit these to taste — they don't
# come from any API and the templates expect them in this shape.
PROLOGUE_TEXT = (
    "Thirteen souls. One groom. One marathon through the most beautifully "
    "ridiculous course in France. Below: the data, the rivalries, the "
    "sub-4 dream, the costume vote. On y va."
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


def _exchange_refresh_token(refresh: str) -> dict | None:
    """POST to Strava's /oauth/token. Returns the full JSON response or None."""
    if not (STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET and refresh):
        return None
    try:
        r = requests.post(
            "https://www.strava.com/oauth/token",
            data={
                "client_id": STRAVA_CLIENT_ID,
                "client_secret": STRAVA_CLIENT_SECRET,
                "grant_type": "refresh_token",
                "refresh_token": refresh,
            },
            timeout=15,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  token refresh failed: {e}", file=sys.stderr)
        return None


def get_access_token(runner_id: str, secret_refresh: str, cache: dict) -> str | None:
    """
    Get a working access token for the runner, using the cache to avoid
    unnecessary refreshes. Strava rotates refresh tokens, so we cache the
    latest one and prefer it; the GitHub-Secret refresh_token is a fallback
    for cold-cache cases.
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

    new_tokens = _exchange_refresh_token(refresh_to_use)

    # 3. Cached refresh_token failed (rotated/revoked)? Fall back to the secret.
    if not new_tokens and entry.get("refresh_token") and entry["refresh_token"] != secret_refresh:
        print(f"  cached refresh token invalid, falling back to secret", file=sys.stderr)
        new_tokens = _exchange_refresh_token(secret_refresh)

    if not new_tokens:
        return None

    cache[runner_id] = {
        "access_token":  new_tokens["access_token"],
        "refresh_token": new_tokens["refresh_token"],
        "expires_at":    new_tokens["expires_at"],
    }
    return new_tokens["access_token"]


def fetch_activities(access_token: str, after_ts: int) -> list[dict]:
    """Fetch activities since `after_ts` (unix). Paginates until exhausted."""
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
            print(f"  activity fetch failed (page {page}): {e}", file=sys.stderr)
            break
        if not batch:
            break
        activities.extend(batch)
        if len(batch) < 200:
            break
        page += 1
    # Runs only
    return [a for a in activities if a.get("type") == "Run"]


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
    pace_improvement_s: float | None = None   # negative = faster, vs 30 days prior
    pre7am_runs_week: int = 0
    days_since_last_run: int = 999
    hangover_score: float | None = None       # Sunday HR / speed; higher = more hungover
    wow_shift_km: float | None = None         # this week's km minus last week's km
    avg_hr_this_week: float | None = None     # duration-weighted, requires ≥3 HR runs
    hr_runs_this_week: int = 0


def compute_runner(cfg: dict, today_uk: date, token_cache: dict) -> RunnerStats:
    """Compute every stat the dashboard needs for one runner."""
    rs = RunnerStats(cfg=cfg)

    secret_name = cfg["secret"]
    refresh = os.environ.get(secret_name, "").strip()
    if not refresh:
        print(f"[{cfg['id']}] no secret — marking disconnected")
        return rs

    access = get_access_token(cfg["id"], refresh, token_cache)
    if not access:
        print(f"[{cfg['id']}] token refresh failed — marking disconnected")
        return rs

    # Pull every run since the training-cycle start date.
    cutoff_uk = UK_TZ.localize(datetime.combine(TRAINING_START, time.min))
    activities = fetch_activities(access, int(cutoff_uk.timestamp()))
    print(f"[{cfg['id']}] {len(activities)} runs since {TRAINING_START.isoformat()}")

    rs.connected = True
    rs.activities = activities

    if not activities:
        rs.form_dots = [""] * 7   # neutral dots — no judgement on inactivity
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

    # ─── Form: rolling last 7 days ─────────────────────────────────
    # Honest activity dots — no judgement of rest days.
    #   "on"      = solid run, day total >= 5km   (dark green)
    #   "partial" = short / shake-out, ran > 0    (light green / gold)
    #   ""        = rest day or no log            (neutral grey, default css)
    # Oldest (6 days ago) on the left, today on the right.
    by_day = defaultdict(float)
    for a in activities:
        d = utc_to_uk(a["start_date"]).date()
        by_day[d] += a.get("distance", 0) / 1000.0
    dots = []
    for i in range(6, -1, -1):
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
    if rs.longest_km >= 5 and rs.longest_seconds > 0:
        rs.predicted_marathon_s = rs.longest_seconds * (42.2 / rs.longest_km) ** 1.06
        rs.predicted_medoc_s    = rs.predicted_marathon_s * MEDOC_PENALTY

    # ─── Pace improvement: last 30 days vs 30–60 days ago ─────────
    boundary = UK_TZ.localize(datetime.combine(today_uk - timedelta(days=30), time.min))
    recent_dist, recent_time = 0.0, 0.0
    older_dist,  older_time  = 0.0, 0.0
    for a in activities:
        when = utc_to_uk(a["start_date"])
        d  = a.get("distance", 0) / 1000.0
        t  = a.get("moving_time", 0) or 0
        if when >= boundary:
            recent_dist += d; recent_time += t
        else:
            older_dist  += d; older_time  += t
    if recent_dist > 0 and older_dist > 0:
        pace_recent = recent_time / recent_dist
        pace_older  = older_time  / older_dist
        rs.pace_improvement_s = pace_recent - pace_older  # negative = faster now

    # ─── Pre-7am runs this week ────────────────────────────────────
    rs.pre7am_runs_week = sum(
        1 for a in week_runs
        if utc_to_uk(a["start_date"]).hour < 7
    )

    # ─── Days since last run ───────────────────────────────────────
    last_day = max(by_day.keys())
    rs.days_since_last_run = (today_uk - last_day).days

    # ─── Week-on-week km shift (for Biggest Shift award) ──────────
    # Compare same period: this week so far (Mon → today) vs last week's
    # equivalent days (last Mon → same weekday last week). Stops the
    # comparison being unfair early in the week.
    last_monday = monday_uk - timedelta(days=7)
    last_week_cutoff = last_monday + (today_uk - monday_uk)   # same weekday, last week
    last_week_same_period_km = sum(
        (a.get("distance", 0) / 1000.0) for a in activities
        if last_monday <= utc_to_uk(a["start_date"]).date() <= last_week_cutoff
    )
    rs.wow_shift_km = rs.week_km - last_week_same_period_km

    # ─── Hangover Hero: highest avg HR per m/s on Sunday runs ──────
    sun_scores = []
    for a in activities:
        if utc_to_uk(a["start_date"]).weekday() != 6:  # 6 = Sunday
            continue
        hr = a.get("average_heartrate")
        spd = a.get("average_speed")
        if hr and spd and spd > 0:
            sun_scores.append(hr / spd)
    if sun_scores:
        rs.hangover_score = max(sun_scores)

    # ─── This-week avg HR (duration-weighted) for The Sufferer ─────
    hr_week_runs = [a for a in week_runs if a.get("average_heartrate")]
    rs.hr_runs_this_week = len(hr_week_runs)
    if len(hr_week_runs) >= 3:
        num = sum((a["average_heartrate"] * ((a.get("moving_time") or 0))) for a in hr_week_runs)
        den = sum(((a.get("moving_time") or 0)) for a in hr_week_runs)
        if den > 0:
            rs.avg_hr_this_week = num / den

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


def build_week_grid(runners: list[RunnerStats], today_uk: date) -> tuple[list[dict], dict]:
    """Group km per weekday Mon→Sun + week summary."""
    monday_uk = today_uk - timedelta(days=today_uk.weekday())
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

    week_total_km = sum(by_weekday.values())
    connected_n   = max(1, sum(1 for r in runners if r.connected))
    avg_per_runner = week_total_km / connected_n
    pace_s = (total_time / week_total_km) if week_total_km > 0 else None

    # WoW for the week strip
    last_week_start = monday_uk - timedelta(days=7)
    last_week_end   = monday_uk - timedelta(days=1)
    last_week_km = 0.0
    for r in runners:
        if not r.connected:
            continue
        for a in r.activities:
            d = utc_to_uk(a["start_date"]).date()
            if last_week_start <= d <= last_week_end:
                last_week_km += a.get("distance", 0) / 1000.0
    if last_week_km > 0:
        wow_pct = ((week_total_km - last_week_km) / last_week_km) * 100
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


def winner_html(before: str, name: str, after: str) -> str:
    """Build an award-detail string with the winner name wrapped in <b>."""
    return f"{html_escape(before)}<b>{html_escape(name)}</b>{html_escape(after)}"


def build_awards(runners: list[RunnerStats], total_km_rank: list[RunnerStats]) -> dict:
    """Compute all eight award winners. Tolerant of empty fields."""

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

    # Top Dog — most km this week (Mon→today), anyone eligible.
    top, top_km = first(
        lambda r: r.connected,
        lambda r: r.week_km,
    )
    if top and top_km and top_km > 0:
        awards["top_dog"] = {
            "title":  "Top Dog",
            "icon":   "👑",
            "detail": winner_html("Most km this week — ", top.cfg["name"], f" at {top_km:.0f}km. King of the leaderboard."),
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
            "detail": winner_html("Up most vs same days last week — ", shift.cfg["name"], f", +{shift_d:.0f}km. Momentum is a hell of a drug."),
        }
    else:
        awards["biggest_shift"] = {"title": "Biggest Shift", "icon": "📊", "detail": "Squad holding steady — no week-on-week jumps yet."}

    # Biggest Glow-Up — most negative pace_improvement_s (faster)
    glow, glow_d = first(
        lambda r: r.connected,
        lambda r: r.pace_improvement_s,
        reverse=False,  # most negative wins
    )
    if glow and glow_d is not None and glow_d < 0:
        awards["glow_up"] = {
            "title":  "Biggest Glow-Up",
            "icon":   "📈",
            "detail": winner_html(f"Pace dropped {fmt_pace_delta(glow_d)}/km — ", glow.cfg["name"], " is cooking"),
        }
    else:
        awards["glow_up"] = {"title": "Biggest Glow-Up", "icon": "📈", "detail": "Nobody's improving yet — keep showing up."}

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

    # L'Aurore — most pre-7am runs this week
    aurore, aurore_d = first(lambda r: r.connected, lambda r: r.pre7am_runs_week)
    if aurore and aurore_d and aurore_d > 0:
        awards["laurore"] = {
            "title":  "L'Aurore",
            "icon":   "🌅",
            "detail": winner_html("Most pre-7am runs — ", aurore.cfg["name"], f", {aurore_d} this week"),
        }
    else:
        awards["laurore"] = {"title": "L'Aurore", "icon": "🌅", "detail": "Everyone's allergic to mornings this week."}

    # The Sufferer — highest weekly avg HR (≥3 HR runs required per runner)
    sufferer, hr_val = first(
        lambda r: r.connected and r.avg_hr_this_week is not None,
        lambda r: r.avg_hr_this_week,
    )
    if sufferer and hr_val:
        awards["the_sufferer"] = {
            "title":  "The Sufferer",
            "icon":   "🌡️",
            "detail": winner_html("Highest avg HR this week — ", sufferer.cfg["name"], f", {int(round(hr_val))}bpm. Either training hard or chasing buses."),
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

    # Hangover Hero — highest Sunday HR / pace ratio
    hh, hh_d = first(lambda r: r.connected, lambda r: r.hangover_score)
    if hh and hh_d is not None:
        best_hr = 0
        for a in hh.activities:
            if utc_to_uk(a["start_date"]).weekday() == 6 and a.get("average_heartrate"):
                if a["average_heartrate"] > best_hr:
                    best_hr = a["average_heartrate"]
        awards["hangover_hero"] = {
            "title":  "The Hangover Hero",
            "icon":   "🍷",
            "detail": winner_html(f"Highest Sunday HR — ", hh.cfg["name"], f", {int(best_hr)}bpm. Suspicious."),
            "shame":  True,
        }
    else:
        awards["hangover_hero"] = {"title": "The Hangover Hero", "icon": "🍷", "detail": "No suspicious Sundays. Yet.", "shame": True}

    return awards


def build_mini_boards(runners: list[RunnerStats]) -> dict:
    """Top-5 lists for the 'detail' section."""
    connected = [r for r in runners if r.connected]

    def top5(predicate, key, fmt, reverse=True, value_class=None):
        items = [(r, key(r)) for r in connected if predicate(r)]
        items = [(r, v) for r, v in items if v is not None]
        items.sort(key=lambda x: x[1], reverse=reverse)
        out = []
        for r, v in items[:5]:
            row = {"name": r.cfg["name"], "value": fmt(v)}
            if value_class:
                row["class"] = value_class(v) if callable(value_class) else value_class
            out.append(row)
        return out

    longest = top5(
        lambda r: r.longest_km > 0,
        lambda r: r.longest_km,
        lambda v: f"{v:.1f} km",
    )
    pace_imp = top5(
        lambda r: r.pace_improvement_s is not None,
        lambda r: r.pace_improvement_s,
        lambda v: fmt_pace_delta(v),
        reverse=False,
        value_class=lambda v: "good" if v < 0 else ("bad" if v > 0 else ""),
    )
    elevation = top5(
        lambda r: r.elevation_m > 0,
        lambda r: r.elevation_m,
        lambda v: f"{int(round(v)):,} m",
    )
    hr = top5(
        lambda r: r.avg_hr is not None and r.avg_hr > 0,
        lambda r: r.avg_hr,
        lambda v: f"{int(round(v))} bpm",
        reverse=False,           # lower HR = more aerobic = top
        value_class=lambda v: "good" if v < 155 else "",
    )

    # Form: rank by count of solid-run days, ties broken by short-run days.
    # Rewards consistency over the last 7 days, no penalty for rest.
    def form_score(r):
        on_count      = sum(1 for d in (r.form_dots or []) if d == "on")
        partial_count = sum(1 for d in (r.form_dots or []) if d == "partial")
        return (on_count, partial_count)

    form_runners = sorted(connected, key=form_score, reverse=True)[:5]
    form = [{"name": r.cfg["name"], "dots": r.form_dots or [""]*7} for r in form_runners]

    return {"longest": longest, "pace_imp": pace_imp, "elevation": elevation, "hr": hr, "form": form}


# ─── News flash auto-generator ─────────────────────────────────────────
def build_newsflash(
    runners: list[RunnerStats],
    group: dict,
    days_until: int,
    this_week: dict,
    plan_status: dict,
) -> list[dict]:
    """
    A mix of data-driven highlights and evergreen Médoc lore. Designed to feel
    alive even with sparse data — the marquee always has something to say.
    """
    items: list[dict] = []
    connected = [r for r in runners if r.connected]
    not_connected_n = sum(1 for r in runners if not r.connected)

    # ── COUNTDOWN ─────────────────────────────────────────────────
    if days_until > 84:
        items.append({"label": "COUNTDOWN", "text": f"{days_until} days till the start gun · {days_until // 7} long Sundays to fill"})
    elif days_until > 28:
        items.append({"label": "COUNTDOWN", "text": f"{days_until} days till Pauillac · the months are getting smaller"})
    elif days_until > 14:
        items.append({"label": "COUNTDOWN", "text": f"{days_until} days · taper season approaches, try not to peak too early"})
    elif days_until > 0:
        items.append({"label": "COUNTDOWN", "text": f"{days_until} days · we are now officially in the panic zone"})
    else:
        items.append({"label": "RACE DAY",  "text": "Today is the day · on y va, doucement"})

    # ── PHASE ───────────────────────────────────────────────────
    items.append({"label": "PHASE", "text": f"Week {this_week['week_num']} of 18 — {this_week['phase']} · {this_week['focus']}"})

    # ── SQUAD TARGET PROGRESS ───────────────────────────────────
    if plan_status["connected_n"] > 0 and plan_status["total_target"] > 0:
        if plan_status["pct"] >= 100:
            items.append({"label": "TARGET", "text": f"Squad smashed this week's {plan_status['total_target']}km target — {plan_status['actual']}km logged. Order more electrolytes."})
        elif plan_status["pct"] >= 70:
            items.append({"label": "TARGET", "text": f"Squad at {plan_status['actual']}/{plan_status['total_target']}km · close, but the lads on Strava silent mode need a nudge"})
        elif plan_status["pct"] >= 30:
            items.append({"label": "TARGET", "text": f"Squad at {plan_status['pct']}% of weekly target · recover Sunday, ramp Monday"})

    # ── ROLL CALL ───────────────────────────────────────────────
    if not_connected_n > 0:
        items.append({"label": "ROLL CALL", "text": f"{not_connected_n} of 13 still off the grid · they know who they are"})

    # ── LOUIS LONG RUN ──────────────────────────────────────────
    louis = next((r for r in runners if r.cfg.get("is_groom")), None)
    if louis and louis.longest_km > 0:
        items.append({"label": "BREAKING", "text": f"Louis logs {louis.longest_km:.1f}km long run — the groom is cooking"})

    # ── SIBLING RIVALRY ─────────────────────────────────────────
    bros = [r for r in connected if r.cfg.get("is_brother")]
    if bros and louis and louis.total_km > 0:
        best_bro = max(bros, key=lambda r: r.total_km)
        gap = louis.total_km - best_bro.total_km
        if gap > 0.5:
            items.append({"label": "RIVALRY", "text": f"{best_bro.cfg['name']} sitting {int(round(gap))}km behind the groom · the family Christmas is going to be awkward"})
        elif gap < -0.5:
            items.append({"label": "RIVALRY", "text": f"{best_bro.cfg['name']} {int(round(-gap))}km AHEAD of Louis · the groom may need a quiet word"})
        else:
            items.append({"label": "RIVALRY", "text": f"Illig brothers within a km of each other · this is anyone's race"})

    # ── DARK HORSE: who's quietly putting in the work ──────────
    non_illig = [r for r in connected if not r.cfg.get("is_brother") and not r.cfg.get("is_groom")]
    if non_illig:
        dh = max(non_illig, key=lambda r: r.total_km)
        if dh.total_km >= 30:
            items.append({"label": "DARK HORSE", "text": f"{dh.cfg['name']} quietly stacking {int(round(dh.total_km))}km · the non-Illigs are coming"})

    # ── PACE GLOW-UP ────────────────────────────────────────────
    glow_cand = [r for r in connected if r.pace_improvement_s is not None and r.pace_improvement_s < -10]
    if glow_cand:
        g = min(glow_cand, key=lambda r: r.pace_improvement_s)
        items.append({"label": "PB", "text": f"{g.cfg['name']} drops {fmt_pace_delta(g.pace_improvement_s)}/km in 30 days · the engine is warming up"})

    # ── GROUP PROJECTION ────────────────────────────────────────
    if group["predicted"] != "—":
        if "on for sub-4" in group["predicted_delta"]:
            items.append({"label": "PROJECTION", "text": f"Group prediction {group['predicted']} · sub-4 is on the table, lads"})
        else:
            items.append({"label": "PROJECTION", "text": f"Group prediction {group['predicted']} · {group['predicted_delta']}, but we have months"})

    # ── KM MILESTONES ───────────────────────────────────────────
    if group["total_km"] >= 1000:
        items.append({"label": "MILESTONE", "text": f"Squad crosses {group['total_km']}km combined · that's London to Bordeaux on foot, ironically"})
    elif group["total_km"] >= 500:
        items.append({"label": "MILESTONE", "text": f"{group['total_km']}km logged · over halfway to Paris if anyone fancied a detour"})
    elif group["total_km"] >= 200:
        items.append({"label": "MILESTONE", "text": f"{group['total_km']}km combined · {int(group['total_km'] / 42.2)} marathons' worth between us"})
    elif group["total_km"] >= 50:
        items.append({"label": "MILESTONE", "text": f"{group['total_km']}km in the bank · keep stacking"})

    # ── EARLY BIRDS ─────────────────────────────────────────────
    early = sorted([r for r in connected if r.pre7am_runs_week > 0], key=lambda r: r.pre7am_runs_week, reverse=True)
    if early:
        e = early[0]
        run_s = "run" if e.pre7am_runs_week == 1 else "runs"
        items.append({"label": "DAWN PATROL", "text": f"{e.cfg['name']} clocked {e.pre7am_runs_week} pre-7am {run_s} this week · respect, fear, slight concern"})

    # ── GHOST WATCH ─────────────────────────────────────────────
    ghosts = [r for r in connected if r.days_since_last_run >= 5]
    if ghosts:
        g = max(ghosts, key=lambda r: r.days_since_last_run)
        items.append({"label": "GHOST WATCH", "text": f"{g.cfg['name']} hasn't logged a run in {g.days_since_last_run} days · 'starts Monday' (week 4 of starting Monday)"})

    # ── STREAK ──────────────────────────────────────────────────
    streakers = sorted([r for r in connected if r.streak >= 3], key=lambda r: r.streak, reverse=True)
    if streakers:
        s = streakers[0]
        items.append({"label": "ON FIRE", "text": f"{s.cfg['name']} on a {s.streak}-day streak · the gas is on"})

    # ── ALWAYS-ON EVERGREEN BITS ────────────────────────────────
    items.extend([
        {"label": "FACT",       "text": "Médoc has 23 wine stops along the course · 1.77 châteaux per runner if shared evenly"},
        {"label": "REMINDER",   "text": "Oysters are served at km 38 · plan your stomach accordingly"},
        {"label": "DISCLAIMER", "text": "Predictions use Riegel's formula, not your hangover history"},
        {"label": "TERROIR",    "text": "Pauillac is mostly gravel · wear shoes that won't murder your feet"},
        {"label": "CUTOFF",     "text": "6h30m race cutoff · after that they pack up the oysters and you walk home"},
        {"label": "HOT TAKE",   "text": "The Médoc isn't a wine tour, it's a marathon with optional refreshments"},
        {"label": "RUMOUR",     "text": "One château allegedly pours grand cru · look for the queue, join it, lose 12 minutes"},
        {"label": "DRESS CODE", "text": "Costumes are mandatory · this is non-negotiable and yes, it'll chafe"},
        {"label": "PSA",        "text": "If you can hold a conversation on your long run, the pace is right · if you're explaining politics, you're going too easy"},
    ])

    # Defensive fallback (shouldn't fire — we always have countdown + phase)
    if not items:
        items = [{"label": "STATUS", "text": "The training files are being assembled. Stand by."}]

    return items


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
    """How the squad is tracking against this week's per-runner target."""
    connected = [r for r in runners if r.connected]
    connected_n = len(connected)
    target_per = week_plan["target_km"]
    total_target = target_per * connected_n
    actual = sum(r.week_km for r in connected)

    if total_target > 0:
        pct = (actual / total_target) * 100
    else:
        pct = 0

    if pct >= 95:
        status, tone = "on target", "up"
    elif pct >= 70:
        status, tone = "close to target", "flat"
    elif pct > 0:
        status, tone = f"under target ({int(round(pct))}%)", "down"
    else:
        status, tone = "no runs yet this week", "flat"

    return {
        "target_per_runner": target_per,
        "long_km":           week_plan["long_km"],
        "total_target":      round(total_target),
        "actual":            round(actual),
        "pct":               int(round(pct)),
        "status":            status,
        "tone":              tone,
        "connected_n":       connected_n,
    }


# ─── Build the leaderboard rows that the templates iterate over ────────
def make_runner_rows(runners: list[RunnerStats]) -> list[dict]:
    """
    Everyone — including the groom — is ranked on the same scale: connected
    runners first (sorted by total km descending), disconnected runners last.
    The groom keeps his ★ tag in the UI but earns his position like anyone else.
    """
    ordered = sorted(
        runners,
        key=lambda r: (not r.connected, -r.total_km, r.cfg["name"]),
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

        rows.append({
            "rank":             i,
            "rank_roman":       to_roman(i),
            "name":             r.cfg["name"],
            "tag":              r.cfg.get("tag"),
            "is_groom":         bool(r.cfg.get("is_groom")),
            "is_brother":       bool(r.cfg.get("is_brother")),
            "connected":        r.connected,
            "meta":             meta,
            "week_km":          f"{r.week_km:.0f}" if r.connected and r.week_km > 0 else "—",
            "total_km":         f"{int(round(r.total_km))}" if r.connected and r.total_km > 0 else "—",
            "longest_km":       f"{r.longest_km:.1f}" if r.connected and r.longest_km > 0 else "—",
            "predicted":        predicted,
            "sub4":             sub4,
            "progress_pct":     pct,
            "streak":           r.streak,
            "avg_pace":         fmt_pace(r.avg_pace_s),
            "avg_hr":           f"{int(round(r.avg_hr))}" if r.avg_hr else "—",
            "elevation_m":      int(round(r.elevation_m)),
        })
    return rows


# ─── Main render ───────────────────────────────────────────────────────
def main() -> None:
    print(f"Le Marathon Du Médoc 26 dashboard build · {datetime.utcnow().isoformat()} UTC")

    runners_cfg = json.loads(RUNNERS_FILE.read_text(encoding="utf-8"))
    today_uk = datetime.now(UK_TZ).date()
    days_until = max(0, (RACE_DATE - today_uk).days)
    token_cache = load_token_cache()
    cache_hits_before = sum(1 for v in token_cache.values() if v.get("access_token"))

    # Crunch per-runner stats
    runners: list[RunnerStats] = []
    for cfg in runners_cfg:
        try:
            rs = compute_runner(cfg, today_uk, token_cache)
        except Exception as e:
            print(f"[{cfg['id']}] unexpected error: {e}", file=sys.stderr)
            rs = RunnerStats(cfg=cfg)
        runners.append(rs)

    save_token_cache(token_cache)
    print(f"Token cache: {cache_hits_before} entries restored, {len(token_cache)} entries saved.")

    # Order by total_km for award lookups (groom-first ordering happens in make_runner_rows)
    by_total = sorted(runners, key=lambda r: r.total_km, reverse=True)

    rows         = make_runner_rows(runners)
    groom_row    = next((r for r in rows if r["is_groom"]), None)
    group        = build_group_stats(runners, today_uk)
    trailing_7d  = build_trailing_7d(runners, today_uk)
    week_grid, week_summary = build_week_grid(runners, today_uk)
    awards       = build_awards(runners, by_total)
    mini_boards  = build_mini_boards(runners)
    phases       = phases_with_current(today_uk)
    this_week    = current_week_plan(today_uk)
    plan_status  = group_plan_status(runners, this_week)
    news_flash   = build_newsflash(runners, group, days_until, this_week, plan_status)

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
        "news_flash":     news_flash,
        "runners":        rows,
        "groom_row":      groom_row,
        "group":          group,
        "trailing_7d":    trailing_7d,
        "week_grid":      week_grid,
        "week_summary":   week_summary,
        "awards":         awards,
        "mini_boards":    mini_boards,
        "medoc_facts":    MEDOC_FACTS,
        "plan_targets":   PLAN_TARGETS,
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
