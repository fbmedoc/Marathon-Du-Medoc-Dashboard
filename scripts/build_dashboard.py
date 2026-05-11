"""
Build the Le Louis 26 dashboard.

Reads runner config from runners.json, refreshes each runner's Strava access
token, pulls the last 60 days of runs, computes per-runner and group stats,
then renders index.html (desktop) and mobile.html from Jinja2 templates.

Designed to be tolerant: missing secrets, revoked tokens, runners with zero
activities, and Strava API hiccups all degrade gracefully to "—" rather
than crashing the build.
"""

from __future__ import annotations

import json
import math
import os
import sys
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

RACE_DATE        = date(2026, 9, 5)        # Marathon du Médoc 2026
RACE_DATE_LABEL  = "5 September 2026"
UK_TZ            = pytz.timezone("Europe/London")
SUB_4_SECONDS    = 4 * 3600                # reference time for "to sub-4" deltas
MEDOC_PENALTY    = 1.10                    # Médoc time = marathon × this (wine stops!)
DAYS_BACK        = 60
WEEK_DAYS        = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

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

COSTUME_POLL = {
    "title": "Group costume vote — closes 1 July",
    "theme": "The 80s",
    "options": [
        {"emoji": "🕺", "name": "Top Gun Pilots",  "votes": 3, "note": "leading",       "leading": True},
        {"emoji": "💪", "name": "Aerobics Class",  "votes": 1, "note": "neon leotards", "leading": False},
        {"emoji": "🎸", "name": "Hair Metal Band", "votes": 1, "note": "wigs essential","leading": False},
        {"emoji": "👻", "name": "Ghostbusters",    "votes": 0, "note": "proton packs?", "leading": False},
    ],
}

MEDOC_FACTS = [
    {"num": "23",        "lbl": "Wine Stops",  "desc": "Châteaux pouring along the route. Pace yourself."},
    {"num": "42.2",      "lbl": "The Distance","desc": "Through Pauillac vineyards. The most beautiful suffering.","unit_small": True, "unit": "km"},
    {"num": "6:30",      "lbl": "Time Limit",  "desc": "After that they pack up the oysters. Be earlier than that."},
]

# 18-week plan, anchored to race day. Each phase has its own template week.
TRAINING_PHASES = [
    {
        "num": "I", "name": "Foundation", "when": "Wks 1–4 · May",
        "desc_long":  "Easy aerobic base. Build to 40km/week. Just consistency.",
        "desc_short": "Aerobic base · 40km/wk",
        "weeks_before_race": (18, 14),
        "workouts": [
            {"day": "Mon", "type": "Easy",     "value": "5km Z2",        "kind": "normal"},
            {"day": "Tue", "type": "Easy",     "value": "6km Z2",        "kind": "normal"},
            {"day": "Wed", "type": "Rest",     "value": "or yoga",       "kind": "rest"},
            {"day": "Thu", "type": "Easy",     "value": "7km Z2",        "kind": "normal"},
            {"day": "Fri", "type": "Rest",     "value": "or walk",       "kind": "rest"},
            {"day": "Sat", "type": "Strides",  "value": "5km + 6×100m",  "kind": "normal"},
            {"day": "Sun", "type": "Long Run", "value": "14km @ 6:00",   "kind": "long"},
        ],
    },
    {
        "num": "II", "name": "Build", "when": "Wks 5–10 · Jun–Jul",
        "desc_long":  "Intervals enter the chat. Long runs to 24km. Tempo at threshold.",
        "desc_short": "Intervals · 24km longs",
        "weeks_before_race": (14, 8),
        "workouts": [
            {"day": "Mon", "type": "Recovery", "value": "5–6km easy",      "kind": "normal"},
            {"day": "Tue", "type": "Intervals","value": "6×800m @ 5:00",   "kind": "normal"},
            {"day": "Wed", "type": "Rest",    "value": "or yoga",          "kind": "rest"},
            {"day": "Thu", "type": "Tempo",   "value": "8km @ 5:30",       "kind": "normal"},
            {"day": "Fri", "type": "Easy",    "value": "5km Z2",           "kind": "normal"},
            {"day": "Sat", "type": "Rest",    "value": "or 30' walk",      "kind": "rest"},
            {"day": "Sun", "type": "Long Run","value": "22km @ 6:00",      "kind": "long"},
        ],
    },
    {
        "num": "III", "name": "Peak", "when": "Wks 11–16 · Jul–Aug",
        "desc_long":  "The hard yards. 32km long runs, marathon-pace efforts. Sleep more.",
        "desc_short": "32km longs · MP efforts",
        "weeks_before_race": (8, 2),
        "workouts": [
            {"day": "Mon", "type": "Recovery","value": "6km easy",         "kind": "normal"},
            {"day": "Tue", "type": "MP",     "value": "10km @ 5:40",       "kind": "normal"},
            {"day": "Wed", "type": "Rest",   "value": "or yoga",           "kind": "rest"},
            {"day": "Thu", "type": "Intervals","value": "5×1km @ 4:50",    "kind": "normal"},
            {"day": "Fri", "type": "Easy",   "value": "6km Z2",            "kind": "normal"},
            {"day": "Sat", "type": "Rest",   "value": "or 30' walk",       "kind": "rest"},
            {"day": "Sun", "type": "Long Run","value": "30km @ 5:55",      "kind": "long"},
        ],
    },
    {
        "num": "IV", "name": "Taper", "when": "Wks 17–18 · Aug–Sep",
        "desc_long":  "Volume drops by half. Carb load. Trust the work. Pack the costume.",
        "desc_short": "Halve volume · carb up",
        "weeks_before_race": (2, 0),
        "workouts": [
            {"day": "Mon", "type": "Easy",     "value": "5km Z2",        "kind": "normal"},
            {"day": "Tue", "type": "Strides",  "value": "4km + 4×100m",  "kind": "normal"},
            {"day": "Wed", "type": "Rest",     "value": "or yoga",       "kind": "rest"},
            {"day": "Thu", "type": "MP",       "value": "5km @ 5:40",    "kind": "normal"},
            {"day": "Fri", "type": "Rest",     "value": "carb up",       "kind": "rest"},
            {"day": "Sat", "type": "Shakeout", "value": "3km easy",      "kind": "normal"},
            {"day": "Sun", "type": "RACE DAY", "value": "42.2km",        "kind": "long"},
        ],
    },
]

PLAN_TARGETS = {
    "headline": "An honest path to 03:59:59",
    "blurb": (
        "Sub-four needs 5:40/km on race day. Médoc adds time at every "
        "château — treat it as a stretch goal. The realistic win: everyone "
        "finishes upright, in costume, with photos."
    ),
    "blurb_short": (
        "Sub-four needs 5:40/km on race day. Médoc adds time at every "
        "château — treat it as the stretch goal. Realistic win: everyone "
        "finishes in costume."
    ),
    "sub4_pace": "5:40",
    "realistic":  "4:30",
    "cutoff":     "6:30",
}


# ─── Strava API ────────────────────────────────────────────────────────
def refresh_token(refresh: str) -> str | None:
    """Exchange a refresh token for a fresh access token. None on failure."""
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
        return r.json().get("access_token")
    except Exception as e:
        print(f"  token refresh failed: {e}", file=sys.stderr)
        return None


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


def compute_runner(cfg: dict, today_uk: date) -> RunnerStats:
    """Compute every stat the dashboard needs for one runner."""
    rs = RunnerStats(cfg=cfg)

    secret_name = cfg["secret"]
    refresh = os.environ.get(secret_name, "").strip()
    if not refresh:
        print(f"[{cfg['id']}] no secret — marking disconnected")
        return rs

    access = refresh_token(refresh)
    if not access:
        print(f"[{cfg['id']}] token refresh failed — marking disconnected")
        return rs

    # Strava expects unix seconds. Use 60 days ago at start of UK day.
    cutoff_uk = UK_TZ.localize(datetime.combine(today_uk - timedelta(days=DAYS_BACK), time.min))
    activities = fetch_activities(access, int(cutoff_uk.timestamp()))
    print(f"[{cfg['id']}] {len(activities)} runs in last {DAYS_BACK}d")

    rs.connected = True
    rs.activities = activities

    if not activities:
        rs.form_dots = ["skip"] * 5
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

    # ─── Form: last 5 calendar days ────────────────────────────────
    # "on" = total day km >= 5; "partial" = ran but <5; "skip" = none.
    by_day = defaultdict(float)
    for a in activities:
        d = utc_to_uk(a["start_date"]).date()
        by_day[d] += a.get("distance", 0) / 1000.0
    dots = []
    for i in range(5, 0, -1):    # 5 days ago → 1 day ago, left-to-right
        d = today_uk - timedelta(days=i)
        km = by_day.get(d, 0.0)
        if km >= 5:
            dots.append("on")
        elif km > 0:
            dots.append("partial")
        else:
            dots.append("skip")
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

    # Le Groom — always Louis (the rank-1 logic enforced elsewhere too)
    groom = next((r for r in runners if r.cfg.get("is_groom")), None)
    if groom:
        first_name = groom.cfg["name"].split()[0]
        awards["le_groom"] = {
            "title":   "Le Groom",
            "icon":    "🤵",
            "detail":  winner_html("Top of the table — ", first_name, ", leading by example. As he should."),
            "featured": True,
        }
    else:
        awards["le_groom"] = {"title": "Le Groom", "icon": "🤵", "detail": "—", "featured": True}

    # Sibling Rivalry — best non-Louis Illig brother by total km
    sib, sib_km = first(
        lambda r: r.cfg.get("is_brother") and not r.cfg.get("is_groom") and r.connected,
        lambda r: r.total_km,
    )
    if sib:
        all_illigs = [r for r in runners if (r.cfg.get("is_brother") or r.cfg.get("is_groom"))]
        all_illigs.sort(key=lambda r: r.total_km, reverse=True)
        above = next((r for r in all_illigs if r.total_km > sib_km), None)
        gap = f"By {int(round(above.total_km - sib_km))}km" if above else "by default"
        awards["sibling_rivalry"] = {
            "title":  "The Sibling Rivalry",
            "icon":   "👯",
            "detail": winner_html("Best brother (non-groom) — ", sib.cfg["name"], f". {gap}. For now."),
        }
    else:
        awards["sibling_rivalry"] = {"title": "The Sibling Rivalry", "icon": "👯", "detail": "Waiting on a brother to log a run."}

    # Top Dog (non-Illig) — most km this week
    top, top_km = first(
        lambda r: not r.cfg.get("is_brother") and not r.cfg.get("is_groom") and r.connected,
        lambda r: r.week_km,
    )
    if top and top_km and top_km > 0:
        awards["top_dog"] = {
            "title":  "Top Dog (non-Illig)",
            "icon":   "👑",
            "detail": winner_html("Most km this week — ", top.cfg["name"], f" at {top_km:.0f}km"),
        }
    else:
        awards["top_dog"] = {"title": "Top Dog (non-Illig)", "icon": "👑", "detail": "Up for grabs."}

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
        awards["la_flamme"] = {
            "title":  "La Flamme",
            "icon":   "🔥",
            "detail": winner_html("Longest streak — ", first_name, f", {flame_d} days. Locked in."),
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

    # The Ghost — most days since last run (only counts connected runners)
    ghost, ghost_d = first(lambda r: r.connected, lambda r: r.days_since_last_run)
    if ghost and ghost_d is not None and ghost_d > 0:
        awards["ghost"] = {
            "title":  "The Ghost",
            "icon":   "👻",
            "detail": winner_html("Most days since last run — ", ghost.cfg["name"], f", {ghost_d} days. 'Starts Monday.'"),
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

    # Form: take connected runners sorted by total_km (rough proxy for relevance)
    form_runners = sorted(connected, key=lambda r: r.total_km, reverse=True)[:5]
    form = [{"name": r.cfg["name"], "dots": r.form_dots or ["skip"]*5} for r in form_runners]

    return {"longest": longest, "pace_imp": pace_imp, "elevation": elevation, "hr": hr, "form": form}


# ─── News flash auto-generator ─────────────────────────────────────────
def build_newsflash(runners: list[RunnerStats], group: dict) -> list[dict]:
    """A handful of one-liners derived from current data. Falls back to evergreens."""
    items: list[dict] = []
    connected = [r for r in runners if r.connected]

    # Louis long run highlight
    louis = next((r for r in runners if r.cfg.get("is_groom")), None)
    if louis and louis.longest_km > 0:
        items.append({"label": "BREAKING", "text": f"Louis logs {louis.longest_km:.1f}km long run — confidence at all-time high"})

    # Best brother
    bros = [r for r in connected if r.cfg.get("is_brother")]
    if bros:
        best_bro = max(bros, key=lambda r: r.total_km)
        gap = (louis.total_km - best_bro.total_km) if louis else 0
        if gap > 0:
            items.append({"label": "RIVALRY", "text": f"{best_bro.cfg['name']} closes the gap to {int(round(gap))}km — sibling tension rising"})

    # Group km milestone
    if group["total_km"] >= 100:
        items.append({"label": "MILESTONE", "text": f"Group passes {group['total_km']}km combined — Médoc bound"})

    # Biggest glow-up
    glow_cand = [r for r in connected if r.pace_improvement_s is not None and r.pace_improvement_s < -10]
    if glow_cand:
        g = min(glow_cand, key=lambda r: r.pace_improvement_s)
        items.append({"label": "PB", "text": f"{g.cfg['name']} drops {fmt_pace_delta(g.pace_improvement_s)}/km this month — the dark horse is galloping"})

    # Group prediction
    if group["predicted"] != "—":
        items.append({"label": "ALERT", "text": f"Sub-4 prediction sitting at {group['predicted']} — the dream is alive"})

    # Costume poll
    leader = next((o for o in COSTUME_POLL["options"] if o.get("leading")), None)
    if leader:
        items.append({"label": "POLL", "text": f"{leader['name']} leading the costume vote — {leader['note']}"})

    # The Ghost
    ghosts = [r for r in connected if r.days_since_last_run >= 7]
    if ghosts:
        g = max(ghosts, key=lambda r: r.days_since_last_run)
        items.append({"label": "UPDATE", "text": f"{g.cfg['name']} \"starts Monday\" — {g.days_since_last_run} days off the gas"})

    # Always-on fallbacks
    if not items:
        items = [
            {"label": "DOSSIER", "text": "The training files are being assembled. Stand by."},
            {"label": "MÉDOC",   "text": "23 châteaux. 42.2km. One stag-do. Pace yourselves."},
        ]
    return items


# ─── Training plan: pick the current phase ─────────────────────────────
def current_phase(today_uk: date) -> dict:
    days_to_race = max(0, (RACE_DATE - today_uk).days)
    weeks_to_race = days_to_race // 7
    for phase in TRAINING_PHASES:
        lo, hi = phase["weeks_before_race"]  # (max_weeks_out, min_weeks_out)
        if hi <= weeks_to_race <= lo:
            return phase
    # Past race day or before plan started — default to first/last as appropriate
    return TRAINING_PHASES[0] if weeks_to_race > 18 else TRAINING_PHASES[-1]


def phases_with_current(today_uk: date) -> list[dict]:
    """Return the four phases with `is_current` flag set."""
    cur = current_phase(today_uk)
    out = []
    for p in TRAINING_PHASES:
        out.append({**p, "is_current": (p["num"] == cur["num"])})
    return out


# ─── Build the leaderboard rows that the templates iterate over ────────
def make_runner_rows(runners: list[RunnerStats]) -> list[dict]:
    """
    Order: Louis first (always rank I), then the rest by total_km desc.
    Each dict matches what desktop.html.j2 and mobile.html.j2 iterate over.
    """
    groom = next((r for r in runners if r.cfg.get("is_groom")), None)
    others = sorted(
        [r for r in runners if not r.cfg.get("is_groom")],
        key=lambda r: (-r.connected * 1, -r.total_km, r.cfg["name"]),
    )
    ordered = ([groom] if groom else []) + others

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
            meta = "no runs in 60 days"
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
    print(f"Le Louis 26 dashboard build · {datetime.utcnow().isoformat()} UTC")

    runners_cfg = json.loads(RUNNERS_FILE.read_text(encoding="utf-8"))
    today_uk = datetime.now(UK_TZ).date()
    days_until = max(0, (RACE_DATE - today_uk).days)

    # Crunch per-runner stats
    runners: list[RunnerStats] = []
    for cfg in runners_cfg:
        try:
            rs = compute_runner(cfg, today_uk)
        except Exception as e:
            print(f"[{cfg['id']}] unexpected error: {e}", file=sys.stderr)
            rs = RunnerStats(cfg=cfg)
        runners.append(rs)

    # Order by total_km for award lookups (groom-first ordering happens in make_runner_rows)
    by_total = sorted(runners, key=lambda r: r.total_km, reverse=True)

    rows         = make_runner_rows(runners)
    group        = build_group_stats(runners, today_uk)
    week_grid, week_summary = build_week_grid(runners, today_uk)
    awards       = build_awards(runners, by_total)
    mini_boards  = build_mini_boards(runners)
    news_flash   = build_newsflash(runners, group)
    phases       = phases_with_current(today_uk)
    this_week    = current_phase(today_uk)

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
        "news_flash":     news_flash,
        "runners":        rows,
        "group":          group,
        "week_grid":      week_grid,
        "week_summary":   week_summary,
        "awards":         awards,
        "mini_boards":    mini_boards,
        "costume_poll":   COSTUME_POLL,
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
