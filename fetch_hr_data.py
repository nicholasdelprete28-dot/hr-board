"""
fetch_hr_data.py  (v2 - full automation)

Builds today's HR favorability board with NO manual screenshots, using only
free, public data sources:

  - MLB Stats API (statsapi.mlb.com)  -> schedule, probable pitchers,
    confirmed lineups, season batting/pitching stats, platoon splits
  - Baseball Savant CSV export        -> barrel%, exit velocity, hard-hit%
  - Open-Meteo weather API            -> live wind speed/direction per park

WHAT THIS REPLACES FROM SWIFTPROPS, AND HOW:
  - "AVG vs Pitch Mix"  -> approximated with the batter's real platoon split
                           (AVG vs LHP or vs RHP, whichever matches today's
                           starter) - not identical to swiftprops' proprietary
                           calculation, but a real, defensible stat that
                           measures the same underlying idea.
  - Crusher/Split tags   -> derived directly from that same platoon split
                           data instead of a black-box tag.
  - Wind, Park           -> live weather API + a fixed park-factor table.
  - Lineup Bonus         -> batting order position from confirmed lineups
                           (falls back to blank if lineups aren't posted yet
                           - usually 1-3 hours before first pitch).

HONESTY NOTE: this script has not been run against live data (the environment
that wrote it has no network access). Field names in MLB's API responses are
correct as documented, but if MLB tweaks a field name, you may need to adjust
a line or two. Test it locally first with `python fetch_hr_data.py` before
trusting the automated schedule.
"""

import csv
import io
import json
import time
import datetime
from zoneinfo import ZoneInfo
import requests

YEAR = 2026
# GitHub Actions runners use UTC system time. Using that directly would mean
# every run after ~7-8pm Eastern starts asking about TOMORROW's schedule
# (barely populated - no pitchers or lineups posted yet) instead of finishing
# out today's real slate. MLB's own scheduling is anchored to US Eastern
# time, so that's what "today" should mean here too.
TODAY = datetime.datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")

# ---------------------------------------------------------------------------
# Fixed park factors (rough HR-friendliness, -2 pitcher-friendly to +2 hitter-
# friendly) and each park's lat/lon for live wind lookup. Extend this table
# with any team not yet listed.
# ---------------------------------------------------------------------------
PARKS = {
    "COL": {"factor": 2, "lat": 39.7559, "lon": -104.9942},   # Coors Field
    "NYY": {"factor": 2, "lat": 40.8296, "lon": -73.9262},    # Yankee Stadium
    "CIN": {"factor": 1, "lat": 39.0975, "lon": -84.5068},
    "MIL": {"factor": 1, "lat": 43.0280, "lon": -87.9712},
    "HOU": {"factor": 0, "lat": 29.7573, "lon": -95.3555},
    "CHC": {"factor": 0, "lat": 41.9484, "lon": -87.6553},
    "BOS": {"factor": 0, "lat": 42.3467, "lon": -71.0972},
    "ATL": {"factor": 0, "lat": 33.8908, "lon": -84.4678},
    "PHI": {"factor": 1, "lat": 39.9061, "lon": -75.1665},
    "BAL": {"factor": -1, "lat": 39.2839, "lon": -76.6218},
    "SF":  {"factor": -1, "lat": 37.7786, "lon": -122.3893},
    "TEX": {"factor": -1, "lat": 32.7473, "lon": -97.0842},
    "CLE": {"factor": -1, "lat": 41.4962, "lon": -81.6852},
    "PIT": {"factor": -1, "lat": 40.4469, "lon": -80.0057},
    "SD":  {"factor": -1, "lat": 32.7073, "lon": -117.1566},
    # Add remaining parks as needed - default factor 0 is used if a team is missing.
}


def statsapi_get(path, params=None):
    url = f"https://statsapi.mlb.com/api/v1/{path}"
    resp = requests.get(url, params=params or {}, timeout=30)
    resp.raise_for_status()
    return resp.json()


# MLB team IDs are stable and well documented - hardcoding this avoids relying on
# the schedule endpoint returning an "abbreviation" field, which it does not
# include by default (that was the KeyError bug).
TEAM_ABBR = {
    108: "LAA", 109: "AZ", 110: "BAL", 111: "BOS", 112: "CHC", 113: "CIN",
    114: "CLE", 115: "COL", 116: "DET", 117: "HOU", 118: "KC", 119: "LAD",
    120: "WSH", 121: "NYM", 133: "ATH", 134: "PIT", 135: "SD", 136: "SEA",
    137: "SF", 138: "STL", 139: "TB", 140: "TEX", 141: "TOR", 142: "MIN",
    143: "PHI", 144: "ATL", 145: "CWS", 146: "MIA", 147: "NYY", 158: "MIL",
}


def team_abbr(team_obj):
    """Look up a team's abbreviation by ID, falling back to its full name if
    somehow not in the table (e.g. a new expansion team)."""
    return TEAM_ABBR.get(team_obj.get("id"), team_obj.get("name", "UNK"))


def get_todays_games():
    """Today's schedule with probable starting pitchers and live game status."""
    data = statsapi_get("schedule", {
        "sportId": 1, "date": TODAY, "hydrate": "probablePitcher,linescore"
    })
    games = []
    for date_block in data.get("dates", []):
        for g in date_block.get("games", []):
            games.append({
                "game_pk": g["gamePk"],
                "home_team": team_abbr(g["teams"]["home"]["team"]),
                "away_team": team_abbr(g["teams"]["away"]["team"]),
                "home_team_id": g["teams"]["home"]["team"].get("id"),
                "away_team_id": g["teams"]["away"]["team"].get("id"),
                "home_pitcher": g["teams"]["home"].get("probablePitcher", {}),
                "away_pitcher": g["teams"]["away"].get("probablePitcher", {}),
                # "Preview" (not started), "Live" (in progress), or "Final"
                # (completed) - used so the webpage can hide finished games
                # by default while still letting you tap into them manually.
                "status": g.get("status", {}).get("abstractGameState", "Preview"),
            })
    return games


def get_lineup(game_pk, side):
    """
    Confirmed STARTING batting order for 'home' or 'away' side, if posted yet.
    Uses each player's individual battingOrder code (e.g. '100' = leadoff starter,
    '801' = a substitute who entered later in the 8-hole) rather than the team-level
    list, which can include the full active roster instead of just the 9 starters.
    Only codes ending in '00' are true starters.
    """
    try:
        box = statsapi_get(f"game/{game_pk}/boxscore")
        team_box = box["teams"][side]
        players = team_box.get("players", {})
        lineup = {}
        for pid_key, pdata in players.items():
            order_code = pdata.get("battingOrder")
            if order_code and order_code.endswith("00"):
                pid = int(pid_key.replace("ID", ""))
                slot = int(order_code) // 100  # '300' -> 3rd in the order
                lineup[pid] = slot
        return lineup
    except Exception:
        return {}


def get_recent_lineup(team_id):
    """
    FALLBACK for when today's official lineup isn't posted yet (usually not
    available until 1-3 hours before first pitch). Pulls the starting lineup
    from this team's most recently completed game instead - regulars are
    usually stable day to day, so this is a reasonable 'probable starters'
    estimate, NOT a confirmed lineup. Rows built this way are tagged
    lineup_confirmed=False so you always know which you're looking at.
    """
    try:
        end = datetime.date.today() - datetime.timedelta(days=1)
        start = end - datetime.timedelta(days=10)
        data = statsapi_get("schedule", {
            "sportId": 1, "teamId": team_id,
            "startDate": start.isoformat(), "endDate": end.isoformat(),
        })
        games = []
        for d in data.get("dates", []):
            games.extend(d.get("games", []))
        finished = [g for g in games
                    if g.get("status", {}).get("abstractGameState") == "Final"]
        if not finished:
            return {}
        finished.sort(key=lambda g: g["gameDate"], reverse=True)
        most_recent = finished[0]
        side = "home" if most_recent["teams"]["home"]["team"]["id"] == team_id else "away"
        return get_lineup(most_recent["gamePk"], side)
    except Exception:
        return {}


def get_pitcher_hand_and_id(probable_pitcher):
    return probable_pitcher.get("id"), probable_pitcher.get("pitchHand", {}).get("code", "R")


def get_season_pitching_stats():
    """WHIP and HR/9 for every pitcher with a decision this season."""
    data = statsapi_get("stats", {
        "stats": "season", "group": "pitching", "season": YEAR, "sportId": 1, "limit": 500
    })
    out = {}
    for split in data.get("stats", [{}])[0].get("splits", []):
        pid = split.get("player", {}).get("id")
        stat = split.get("stat", {})
        if pid and stat.get("whip") is not None:
            out[pid] = {
                "whip": float(stat["whip"]),
                "hr9": float(stat.get("homeRunsPer9", 1.2) or 1.2),
            }
    return out


def get_season_batting_stats():
    """AVG, OBP, ISO (computed from SLG-AVG), and plate appearances for every
    batter this season. AVG/OBP feed the HRR (Hits+Runs+RBI) board's
    "on-base" component. PA feeds compute_score()'s sample-size shrink on
    the power inputs (ISO/barrel%/EV/hard-hit%) - see power_sample_weight()
    below."""
    data = statsapi_get("stats", {
        "stats": "season", "group": "hitting", "season": YEAR, "sportId": 1, "limit": 1500
    })
    out = {}
    for split in data.get("stats", [{}])[0].get("splits", []):
        pid = split.get("player", {}).get("id")
        name = split.get("player", {}).get("fullName")
        stat = split.get("stat", {})
        avg = stat.get("avg")
        obp = stat.get("obp")
        slg = stat.get("slg")
        pa = stat.get("plateAppearances")
        if pid and avg is not None and slg is not None:
            try:
                iso = float(slg) - float(avg)
            except ValueError:
                iso = None
            try:
                avg_f = float(avg)
            except (TypeError, ValueError):
                avg_f = None
            try:
                obp_f = float(obp) if obp not in (None, "") else None
            except (TypeError, ValueError):
                obp_f = None
            try:
                pa_i = int(pa) if pa not in (None, "") else None
            except (TypeError, ValueError):
                pa_i = None
            try:
                slg_f = float(slg) if slg not in (None, "") else None
            except (TypeError, ValueError):
                slg_f = None
            out[pid] = {"name": name, "iso": iso, "avg": avg_f, "obp": obp_f, "pa": pa_i, "slg": slg_f}
    return out


_name_cache = {}


def get_player_name(player_id):
    """
    Fallback for players missing from get_season_batting_stats() - usually
    rookies or recent call-ups with too few plate appearances to appear in
    season aggregate stats yet. Without this, those players showed up with a
    blank name in players.json even though their other stats were fine.
    """
    if player_id in _name_cache:
        return _name_cache[player_id]
    try:
        data = statsapi_get(f"people/{player_id}")
        name = data.get("people", [{}])[0].get("fullName", "")
    except Exception:
        name = ""
    _name_cache[player_id] = name
    return name


def get_platoon_split(batter_id, vs_hand):
    """Batter's AVG facing LHP or RHP this season. vs_hand is 'L' or 'R'."""
    sit_code = "vl" if vs_hand == "L" else "vr"
    try:
        data = statsapi_get(f"people/{batter_id}/stats", {
            "stats": "statSplits", "sitCodes": sit_code,
            "group": "hitting", "season": YEAR
        })
        splits = data.get("stats", [{}])[0].get("splits", [])
        if splits:
            return float(splits[0]["stat"].get("avg", 0) or 0)
    except Exception:
        pass
    return None


def get_risp_avg(batter_id):
    """
    Batter's AVG with Runners In Scoring Position this season - feeds the HRR
    (Hits+Runs+RBI) board's RBI-opportunity signal, the same role
    get_platoon_split() plays for the HR board's matchup signal.

    HONESTY NOTE: "risp" is the commonly-documented MLB Stats API sitCode for
    this split, but - like every other untested endpoint in this file (see
    the module docstring) - it hasn't been confirmed against a live response.
    If this silently returns None for everyone, check the run log (add a
    print of the raw response here temporarily) and fix the sitCode string.
    """
    try:
        data = statsapi_get(f"people/{batter_id}/stats", {
            "stats": "statSplits", "sitCodes": "risp",
            "group": "hitting", "season": YEAR
        })
        splits = data.get("stats", [{}])[0].get("splits", [])
        if splits:
            return float(splits[0]["stat"].get("avg", 0) or 0)
    except Exception:
        pass
    return None


def get_gamelog(batter_id, season):
    """
    Every game this batter has played in `season`, oldest first, with the
    per-game counting stats the player detail view needs (HR, hits, XBH,
    runs, RBI, plate appearances) plus the opponent and home/away flag so the
    frontend can label each bar ("vs CLE" / "@ PIT").

    Feeds BOTH the existing L15-HR "recent form" number and the player detail
    graphs (HR board), plus the new "hrr" field (Hits + Runs + RBI combined,
    the standard prop-bet stat line) that powers the HRR board. Note "hrr"
    intentionally double-counts a home run (it's already 1 hit, plus at least
    1 run and 1 RBI) - that's how the real H+R+RBI prop line works, not a bug.
    """
    try:
        data = statsapi_get(f"people/{batter_id}/stats", {
            "stats": "gameLog", "group": "hitting", "season": season
        })
        splits = data.get("stats", [{}])[0].get("splits", [])
        games = []
        for s in splits:
            stat = s.get("stat", {})
            opp = s.get("opponent", {}) or {}
            doubles = int(stat.get("doubles", 0) or 0)
            triples = int(stat.get("triples", 0) or 0)
            hr = int(stat.get("homeRuns", 0) or 0)
            hits = int(stat.get("hits", 0) or 0)
            runs = int(stat.get("runs", 0) or 0)
            rbi = int(stat.get("rbi", 0) or 0)
            games.append({
                "date": s.get("date"),
                "opp": team_abbr(opp) if opp else "",
                "home": bool(s.get("isHome")),
                "hr": hr,
                "hits": hits,
                "xbh": doubles + triples + hr,
                "pa": int(stat.get("plateAppearances", 0) or 0),
                "runs": runs,
                "rbi": rbi,
                "hrr": hits + runs + rbi,
                # Total bases: 1*singles + 2*2B + 3*3B + 4*HR, expanded so we
                # don't need singles stored separately - feeds the TB board.
                "tb": hits + doubles + 2 * triples + 3 * hr,
            })
        # The API normally returns these oldest-first already; sort defensively
        # so a change on MLB's end can't silently flip chart order left-to-right.
        games.sort(key=lambda g: g["date"] or "")
        return games
    except Exception:
        return []


def get_season_totals_hitting(batter_id, season):
    """Aggregate hitting totals for a prior season (used for the '2025' row
    in the player detail split box, where a full game log isn't needed -
    just games played and counting stats)."""
    try:
        data = statsapi_get(f"people/{batter_id}/stats", {
            "stats": "season", "group": "hitting", "season": season
        })
        splits = data.get("stats", [{}])[0].get("splits", [])
        if not splits:
            return None
        stat = splits[0].get("stat", {})
        gp = int(stat.get("gamesPlayed", 0) or 0)
        if gp == 0:
            return None
        return {
            "gamesPlayed": gp,
            "hits": int(stat.get("hits", 0) or 0),
            "doubles": int(stat.get("doubles", 0) or 0),
            "triples": int(stat.get("triples", 0) or 0),
            "homeRuns": int(stat.get("homeRuns", 0) or 0),
            "plateAppearances": int(stat.get("plateAppearances", 0) or 0),
            "runs": int(stat.get("runs", 0) or 0),
            "rbi": int(stat.get("rbi", 0) or 0),
        }
    except Exception:
        return None


def window_stats(games):
    """HR%/hits%/XBH%/HRR% (share of games with at least one, or - for HRR -
    at least 2, matching the standard 1.5 Hits+Runs+RBI prop line), plus
    average hits/XBH/HRR/PA, across a list of games (a window like
    L5/L10/L20, or a full season's real game log) - these percentages are
    exact since they're built from real per-game data, not an aggregate
    approximation."""
    n = len(games)
    if n == 0:
        return None
    hr_games = sum(1 for g in games if g["hr"] >= 1)
    hits_games = sum(1 for g in games if g["hits"] >= 1)
    xbh_games = sum(1 for g in games if g["xbh"] >= 1)
    hrr_games = sum(1 for g in games if g.get("hrr", 0) >= 2)
    tb_games = sum(1 for g in games if g.get("tb", 0) >= 2)
    return {
        "n": n,
        "hrPct": round(100 * hr_games / n, 1),
        "hitsPct": round(100 * hits_games / n, 1),
        "xbhPct": round(100 * xbh_games / n, 1),
        "hrrPct": round(100 * hrr_games / n, 1),
        "tbPct": round(100 * tb_games / n, 1),
        "hitsAvg": round(sum(g["hits"] for g in games) / n, 2),
        "xbhAvg": round(sum(g["xbh"] for g in games) / n, 2),
        "hrrAvg": round(sum(g.get("hrr", 0) for g in games) / n, 2),
        "tbAvg": round(sum(g.get("tb", 0) for g in games) / n, 2),
        "paAvg": round(sum(g["pa"] for g in games) / n, 2),
    }


def season_totals_to_window(totals):
    """Same shape as window_stats(), but built from season-AGGREGATE totals
    (used for a prior season, where we don't pull the full game log). There's
    no way to recover '% of games with >=1 hit/HR/XBH/HRR-line' from
    aggregate totals alone (a multi-hit game looks the same as two
    single-hit games), so only hrPct is included, as a per-game-played
    approximation - hitsPct/xbhPct/hrrPct are left out entirely rather than
    shown as something they're not; the frontend falls back to the
    average-per-game figures for those instead."""
    if not totals or not totals.get("gamesPlayed"):
        return None
    gp = totals["gamesPlayed"]
    return {
        "n": gp,
        "hrPct": round(100 * totals["homeRuns"] / gp, 1),
        "hitsAvg": round(totals["hits"] / gp, 2),
        "xbhAvg": round((totals["doubles"] + totals["triples"] + totals["homeRuns"]) / gp, 2),
        "hrrAvg": round((totals["hits"] + totals.get("runs", 0) + totals.get("rbi", 0)) / gp, 2),
        "tbAvg": round((totals["hits"] + totals["doubles"] + 2 * totals["triples"]
                        + 3 * totals["homeRuns"]) / gp, 2),
        "paAvg": round(totals["plateAppearances"] / gp, 2),
    }


def get_wind(lat, lon):
    """Current wind speed (mph) and direction (degrees) at a park."""
    try:
        resp = requests.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": lat, "longitude": lon,
            "current": "wind_speed_10m,wind_direction_10m",
            "wind_speed_unit": "mph",
        }, timeout=15)
        resp.raise_for_status()
        cur = resp.json().get("current", {})
        return cur.get("wind_speed_10m"), cur.get("wind_direction_10m")
    except Exception:
        return None, None


def fetch_batter_statcast():
    """
    Returns a dict keyed by MLBAM player ID (an integer both Baseball Savant
    and MLB's Stats API use for the same players), NOT by name. Matching by
    name string was unreliable - accented letters, "Jr." formatting, and
    suffix punctuation don't line up character-for-character between the two
    sources. Player ID is the correct join key.

    Tries a few possible column names for the ID field since we can't verify
    Savant's exact current CSV schema without a live test run - and prints
    the real header row either way, so if matching still fails we can see
    exactly what columns actually came back instead of guessing again.
    """
    url = (f"https://baseballsavant.mlb.com/leaderboard/statcast"
           # min=1 instead of min=q: "q" (qualified) only includes the ~150 top
           # batters by plate-appearance volume league-wide, which excludes
           # plenty of real starters (platoon players, part-timers). min=1
           # includes anyone with at least 1 batted ball event this season.
           f"?type=batter&year={YEAR}&position=&team=&min=1&csv=true")
    resp = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    # decode with utf-8-sig, NOT resp.text: Savant's CSV starts with a BOM
    # (byte-order-mark) character that sits directly before the quoted
    # "last_name, first_name" header. That BOM breaks Python's csv parser's
    # ability to recognize the opening quote for that one field, which
    # splits it into two garbage columns and shifts every column after it
    # by one position - silently misaligning player_id with the wrong data.
    # utf-8-sig strips the BOM before parsing, fixing this at the root.
    text = resp.content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    print(f"  Savant CSV columns: {reader.fieldnames}")
    print(f"  Savant CSV row count: {len(rows)}")
    if rows:
        print(f"  sample row: {rows[0]}")

    id_columns = ["player_id", "batter", "xba_id", "id", "mlb_id", "mlbam_id"]
    id_col_used = next((c for c in id_columns if rows and c in rows[0]), None)
    print(f"  using ID column: {id_col_used}")

    # Same fallback approach as the ID column above: Savant has renamed CSV
    # columns before, and a silently-wrong name here doesn't error - it just
    # makes row.get() return None, "or 0" swallows that, and every player's
    # stat quietly comes out as 0.0% with no error anywhere. Trying several
    # known names and LOGGING which one matched (or that none did) turns
    # that into something visible in the Action's run log instead.
    ev_columns = ["avg_hit_speed", "exit_velocity_avg", "launch_speed_avg"]
    barrel_columns = ["brl_percent", "barrel_percent", "barrel_batted_rate"]
    hardhit_columns = ["ev95percent", "hard_hit_percent", "hardhit_percent", "z_hard_hit_percent"]
    ev_col = next((c for c in ev_columns if rows and c in rows[0]), None)
    barrel_col = next((c for c in barrel_columns if rows and c in rows[0]), None)
    hardhit_col = next((c for c in hardhit_columns if rows and c in rows[0]), None)
    print(f"  using EV column: {ev_col} | barrel column: {barrel_col} | hard-hit column: {hardhit_col}")
    if rows and (ev_col is None or barrel_col is None or hardhit_col is None):
        print(f"  WARNING: at least one stat column not found - check the "
              f"printed CSV columns above and add the real name to the "
              f"matching *_columns list in fetch_batter_statcast().")

    out = {}
    for row in rows:
        pid_raw = row.get(id_col_used) if id_col_used else None
        if not pid_raw:
            continue
        try:
            pid = int(pid_raw)
            out[pid] = {
                "ev": float((row.get(ev_col) if ev_col else None) or 0),
                "barrel": float((row.get(barrel_col) if barrel_col else None) or 0) / 100,
                "hardhit": float((row.get(hardhit_col) if hardhit_col else None) or 0) / 100,
            }
        except (TypeError, ValueError):
            continue
    return out


def wind_park_factor(speed, direction):
    """Rough wind-effect scoring: strong wind matters more than light wind."""
    if speed is None or speed < 5:
        return 0
    if speed >= 15:
        return 2
    if speed >= 8:
        return 1
    return 0


# ---------------------------------------------------------------------------
# Same scoring formula as the HR Board webpage (v10): Power 35%, Matchup 30%,
# Recent 15%, Platoon 10%, Opportunity 10%. Kept in sync by hand - if the
# formula changes on the webpage, update it here too.
# ---------------------------------------------------------------------------
def clamp01(x):
    return max(0.0, min(1.0, x))


def barrel_confidence(barrel, ev):
    """How much to trust a batter's barrel% as real power signal, rather
    than taking it at face value. The idea: a high barrel rate unsupported
    by real exit velo is a little suspect, so it gets discounted - but this
    now ramps in smoothly instead of snapping at hard cutoffs (barrel==0.12,
    ev==90/92), which used to create a cliff where a guy at 11.9% barrel
    scored BETTER than a guy at 12.0% with identical EV, since crossing the
    line used to roughly halve the effective value overnight. Same
    endpoints and same 0.5 max-discount ceiling as before, just continuous
    in between: no discount below ~8% barrel or at elite EV (95+), full
    0.5 discount only at both high barrel AND weak EV (85 or below).
    """
    if barrel is None or ev is None:
        return 1.0
    barrel_intensity = clamp01((barrel - 0.08) / 0.08)   # ramps in 8% -> 16%
    ev_support = clamp01((ev - 85) / 10)                  # ramps in 85 -> 95 mph
    discount = barrel_intensity * (1 - ev_support) * 0.5  # 0.5 = same ceiling as before
    return 1 - discount


def avgmix_confidence_blend(avgmix):
    if avgmix is None:
        return 0.24
    if avgmix <= 0.10 or avgmix >= 0.40:
        return avgmix * 0.5 + 0.24 * 0.5
    return avgmix


def risp_confidence_blend(risp):
    """Same shrink-toward-league-average treatment as avgmix_confidence_blend()
    above, applied to RISP AVG. RISP splits are usually an even smaller
    sample than the vs-hand platoon split - early in the season a batter
    can go 3-for-6 with runners in scoring position and look like an RBI
    lock off a tiny sample. Anchored to ~.255 (RISP AVG's typical league
    mean) rather than .24 since RISP average runs a little higher than
    the platoon-average anchor."""
    if risp is None:
        return 0.255
    if risp <= 0.15 or risp >= 0.35:
        return risp * 0.5 + 0.255 * 0.5
    return risp


# Lineup-slot bonus tuned for HRR (Hits+Runs+RBI) rather than the HR board's
# straight top-of-order-favoring formula (max(1, 9 - order_pos)). Runs favor
# the top of the order (most plate appearances, most times up with the
# bottom of the order on base ahead of them); RBI favors the heart of the
# order (3-6, batting with the most traffic already on base). A straight
# line down from leadoff shortchanges the 3-6 hole hitters who drive in the
# most runs, so this plateaus across slots 1-6 instead and only drops off
# for the bottom of the order - this is the fix the compute_hrr_score()
# docstring below used to flag as a known gap.
HRR_LINEUP_BONUS = {1: 7, 2: 8, 3: 8, 4: 8, 5: 7, 6: 5, 7: 3, 8: 2, 9: 1}


def hrr_lineup_bonus(order_pos):
    if order_pos is None:
        return 4
    return HRR_LINEUP_BONUS.get(order_pos, 1)


# Gentle sample-size confidence shrink for the power inputs (ISO/barrel%/
# EV/hard-hit%), all of which come straight from this season's aggregate
# stats with zero protection against small samples. A part-time player who
# gets hot over a short stretch can otherwise look like a proven elite
# power bat off a handful of at-bats. This is deliberately soft, not a
# hard cutoff - POWER_SHRINK_K is "how many PA of league-average we mix in
# as a prior," so even a thin-but-real sample keeps most of its own signal
# instead of getting flattened toward average. Tuned gentle on purpose: the
# goal is to take the edge off a lucky 20-PA flash, not punish a guy who's
# genuinely 60-80 PA into a real hot stretch.
POWER_SHRINK_K = 40
LEAGUE_AVG_ISO = 0.150
LEAGUE_AVG_BARREL = 0.075
LEAGUE_AVG_EV = 88.5
LEAGUE_AVG_HARDHIT = 0.36


def power_sample_weight(pa):
    if pa is None or pa <= 0:
        return 0.3  # unknown PA - treat like a modest partial sample, not zero trust
    return pa / (pa + POWER_SHRINK_K)


def compute_score(p):
    barrel = p["barrel"] or 0
    ev = p["ev"] or 85
    iso = p["iso"] or 0
    hardhit = p["hardhit"] or 0.30
    # Gentle PA-weighted shrink toward league average before these feed the
    # power score - see power_sample_weight() above for the reasoning.
    pw = power_sample_weight(p.get("pa"))
    barrel = barrel * pw + LEAGUE_AVG_BARREL * (1 - pw)
    ev = ev * pw + LEAGUE_AVG_EV * (1 - pw)
    iso = iso * pw + LEAGUE_AVG_ISO * (1 - pw)
    hardhit = hardhit * pw + LEAGUE_AVG_HARDHIT * (1 - pw)
    phr9 = p["phr9"] if p["phr9"] is not None else 1.2
    whip = p["whip"] if p["whip"] is not None else 1.30
    avgmix = avgmix_confidence_blend(p["avgmix"])
    wind = p["wind"] or 0
    park = p["park"] or 0
    l15hr = p["l15hr"] if p["l15hr"] is not None else 0
    l5hr = p["l5hr"] if p.get("l5hr") is not None else 0
    lbonus = p["lbonus"] if p["lbonus"] is not None else 3
    crush = p["crush"] or 0
    split = p["split"] or 0

    conf = barrel_confidence(barrel, ev)
    barrel_adj = barrel * conf

    power = (clamp01(barrel_adj / 0.25) + clamp01((ev - 85) / 15)
             + clamp01(iso / 0.4) + clamp01((hardhit - 0.3) / 0.4)) / 4

    phr9_s = clamp01((phr9 - 0.3) / 1.7)
    whip_s = clamp01((whip - 0.9) / 0.9)
    pitcher_quality = (phr9_s + whip_s) / 2
    avgmix_s = clamp01(avgmix / 0.5)
    wind_s = clamp01((wind + 2) / 4)
    park_s = clamp01((park + 2) / 4)
    # wind_s carries a bit more weight than before (0.5 -> 0.7) per request -
    # still clearly the smallest of the four matchup inputs (~19% of the
    # bucket vs ~27% each for pitcher quality/avgmix/park), just no longer
    # nearly invisible on days with real wind.
    matchup = (pitcher_quality + avgmix_s + park_s + wind_s * 0.7) / 3.7

    # Recent form: blends the steadier 15-game base rate with a
    # fast-reacting 5-game streak read, so a player who's gone cold (or
    # caught fire) this week actually moves instead of being masked by a
    # slow-draining 15-game window. L15 anchors it (60%) so one huge game
    # doesn't spike the score; L5 (40%) is what makes "on a heater right
    # now" show up day to day instead of taking two weeks to register.
    # 2+ HR in the last 5 games is rare enough to max out that half.
    recent = clamp01(l15hr / 6) * 0.6 + clamp01(l5hr / 2) * 0.4
    platoon = (crush + split) / 2
    opportunity = clamp01((lbonus - 1) / 5)

    score = power * 35 + matchup * 30 + recent * 15 + platoon * 10 + opportunity * 10

    return {
        "score": round(score, 1),
        "conf": conf,
        "powerPct": round(power * 100, 1),
        "matchupPct": round(matchup * 100, 1),
        "recentPct": round(recent * 100, 1),
        "platoonPct": round(platoon * 100, 1),
        "opportunityPct": round(opportunity * 100, 1),
    }


# ---------------------------------------------------------------------------
# HRR (Hits+Runs+RBI, over 1.5 line - i.e. needs 2+ combined) board scoring.
# Same 5-factor shape and 0-100 scale as compute_score() above so both boards
# read consistently, but built from different underlying stats since HRR
# rewards getting on base and driving in runs, not raw power:
#   OnBase 35%     - season AVG/OBP/ISO (times on base + extra-base ability
#                    drive both hits AND runs)
#   Matchup 30%    - opposing pitcher WHIP (a leaky pitcher means more
#                    baserunners AND more RBI chances) blended with the same
#                    platoon-AVG signal the HR board uses
#   Recent 15%     - share of the last 15 games clearing the 1.5 HRR line
#   RISP 10%       - confidence-blended AVG with runners in scoring position
#                    (RBI conversion) - see risp_confidence_blend() above
#   Opportunity 10%- lineup slot, via hrr_lineup_bonus() above rather than
#                    the HR board's lbonus - tuned to plateau across the
#                    1-6 holes instead of favoring leadoff hitters only.
# ---------------------------------------------------------------------------
def compute_hrr_score(p):
    avg = p.get("avg") if p.get("avg") is not None else 0.240
    obp = p.get("obp") if p.get("obp") is not None else 0.310
    iso = p["iso"] or 0
    whip = p["whip"] if p["whip"] is not None else 1.30
    avgmix = avgmix_confidence_blend(p["avgmix"])
    risp = risp_confidence_blend(p.get("risp"))
    l15hrr = p.get("l15hrr") if p.get("l15hrr") is not None else 0
    hrr_lbonus = p["hrrLbonus"] if p.get("hrrLbonus") is not None else 4

    onbase = (clamp01((avg - 0.200) / 0.150) + clamp01((obp - 0.280) / 0.170)
              + clamp01(iso / 0.35)) / 3

    whip_s = clamp01((whip - 0.9) / 0.9)
    avgmix_s = clamp01(avgmix / 0.5)
    matchup = (whip_s + avgmix_s) / 2

    recent = clamp01(l15hrr / 10)  # clearing the line ~10/15 games is elite
    risp_s = clamp01((risp - 0.150) / 0.250)
    opportunity = clamp01((hrr_lbonus - 1) / 5)

    score = onbase * 35 + matchup * 30 + recent * 15 + risp_s * 10 + opportunity * 10

    return {
        "hrrScore": round(score, 1),
        "hrrOnbasePct": round(onbase * 100, 1),
        "hrrMatchupPct": round(matchup * 100, 1),
        "hrrRecentPct": round(recent * 100, 1),
        "hrrRispPct": round(risp_s * 100, 1),
        "hrrOpportunityPct": round(opportunity * 100, 1),
    }


# ---------------------------------------------------------------------------
# TB (Total Bases, line 1.5 - needs 2+) board scoring. Sits between HR and
# HRR in spirit: TB rewards BOTH contact (any hit counts for at least 1
# base) and power (extra-base hits count for more), so it gets a heavier
# power weight than HRR but still credits pure contact, unlike the HR
# board which only cares about the ball leaving the park.
#   Contact 25%    - AVG/OBP, same on-base signal as HRR but lower weight
#   Power 30%      - barrel/EV/ISO/hard-hit% (PA-shrunk, same as the HR
#                    board) PLUS season SLG, which is literally TB/AB -
#                    the most direct rate-stat proxy for this exact prop
#   Matchup 25%    - pitcher WHIP + platoon AVG, same pattern as the other
#                    two boards
#   Recent 10%     - share of the last 15 games clearing the 1.5 TB line
#   Opportunity 10%- lineup slot bonus (same field as the HR board)
# ---------------------------------------------------------------------------
def compute_tb_score(p):
    avg = p.get("avg") if p.get("avg") is not None else 0.240
    obp = p.get("obp") if p.get("obp") is not None else 0.310
    slg = p.get("slg") if p.get("slg") is not None else 0.390
    barrel = p["barrel"] or 0
    ev = p["ev"] or 85
    iso = p["iso"] or 0
    hardhit = p["hardhit"] or 0.30
    whip = p["whip"] if p["whip"] is not None else 1.30
    avgmix = avgmix_confidence_blend(p["avgmix"])
    l15tb = p.get("l15tb") if p.get("l15tb") is not None else 0
    lbonus = p["lbonus"] if p["lbonus"] is not None else 3

    # Same gentle PA-weighted shrink as the HR board - these are the same
    # small-sample-prone inputs, so they get the same protection here.
    pw = power_sample_weight(p.get("pa"))
    barrel_s = barrel * pw + LEAGUE_AVG_BARREL * (1 - pw)
    ev_s = ev * pw + LEAGUE_AVG_EV * (1 - pw)
    iso_s = iso * pw + LEAGUE_AVG_ISO * (1 - pw)
    hardhit_s = hardhit * pw + LEAGUE_AVG_HARDHIT * (1 - pw)

    conf = barrel_confidence(barrel_s, ev_s)
    barrel_adj = barrel_s * conf

    contact = (clamp01((avg - 0.200) / 0.150) + clamp01((obp - 0.280) / 0.170)) / 2

    power = (clamp01(barrel_adj / 0.25) + clamp01((ev_s - 85) / 15)
             + clamp01(iso_s / 0.4) + clamp01((hardhit_s - 0.3) / 0.4)
             + clamp01((slg - 0.320) / 0.280)) / 5

    whip_s = clamp01((whip - 0.9) / 0.9)
    avgmix_s = clamp01(avgmix / 0.5)
    matchup = (whip_s + avgmix_s) / 2

    recent = clamp01(l15tb / 10)  # clearing the line ~10/15 games is elite

    opportunity = clamp01((lbonus - 1) / 5)

    score = contact * 25 + power * 30 + matchup * 25 + recent * 10 + opportunity * 10

    return {
        "tbScore": round(score, 1),
        "tbContactPct": round(contact * 100, 1),
        "tbPowerPct": round(power * 100, 1),
        "tbMatchupPct": round(matchup * 100, 1),
        "tbRecentPct": round(recent * 100, 1),
        "tbOpportunityPct": round(opportunity * 100, 1),
    }


def main():
    print("Fetching today's schedule and probable pitchers...")
    games = get_todays_games()
    print(f"  {len(games)} games today")

    print("Fetching season pitching stats (WHIP, HR/9)...")
    pitching_stats = get_season_pitching_stats()

    print("Fetching season batting stats (ISO)...")
    batting_stats = get_season_batting_stats()

    print("Fetching Statcast batter data (barrel%, EV, hard-hit%)...")
    statcast = fetch_batter_statcast()
    print(f"  parsed {len(statcast)} batters with Statcast data")

    # ---- PASS 1: build every player row using only data we already have in
    # memory (no network calls in this loop) - fast, a few seconds at most.
    rows = []  # each entry: (player_row_dict, batter_id, pitcher_hand)
    sides_with_pitcher = 0
    sides_confirmed_lineup = 0
    sides_projected_lineup = 0
    sides_missing_pitcher = 0
    sides_no_lineup_at_all = 0

    for g in games:
        for side, opp_side in [("home", "away"), ("away", "home")]:
            team = g[f"{side}_team"]
            team_id = g[f"{side}_team_id"]
            opp_pitcher = g[f"{opp_side}_pitcher"]
            if not opp_pitcher:
                sides_missing_pitcher += 1
                continue
            sides_with_pitcher += 1
            pitcher_id, pitcher_hand = get_pitcher_hand_and_id(opp_pitcher)
            pitcher_stat = pitching_stats.get(pitcher_id, {"whip": 1.30, "hr9": 1.20})

            park = PARKS.get(g["home_team"], {"factor": 0, "lat": None, "lon": None})
            wind_speed, wind_dir = (None, None)
            if park["lat"] is not None:
                wind_speed, wind_dir = get_wind(park["lat"], park["lon"])
            wind_score = wind_park_factor(wind_speed, wind_dir)

            lineup = get_lineup(g["game_pk"], side)
            lineup_confirmed = True
            if lineup:
                sides_confirmed_lineup += 1
            else:
                lineup = get_recent_lineup(team_id)
                lineup_confirmed = False
                if lineup:
                    sides_projected_lineup += 1
                    print(f"  using PROJECTED lineup for {team} (from last game) "
                          f"- {g['away_team']} @ {g['home_team']}")
                else:
                    sides_no_lineup_at_all += 1
                    print(f"  no lineup available (today OR recent) for {team} "
                          f"- {g['away_team']} @ {g['home_team']}")

            for batter_id, order_pos in lineup.items():
                bstats = batting_stats.get(batter_id, {})
                name = bstats.get("name") or ""  # filled in pass 2 if still blank
                sc = statcast.get(batter_id, {})

                player_row = {
                    "player": name,
                    "team": team,
                    "pitcher": opp_pitcher.get("fullName", ""),
                    "hand": pitcher_hand,
                    "game": f"{g['away_team']} @ {g['home_team']}",
                    "lineupConfirmed": lineup_confirmed,
                    "gameStatus": g["status"],
                    "playerId": batter_id,
                    "barrel": sc.get("barrel"),
                    "ev": sc.get("ev"),
                    "hardhit": sc.get("hardhit"),
                    "iso": bstats.get("iso"),
                    "pa": bstats.get("pa"),
                    "slg": bstats.get("slg"),
                    "avg": bstats.get("avg"),
                    "obp": bstats.get("obp"),
                    "phr9": pitcher_stat["hr9"],
                    "whip": pitcher_stat["whip"],
                    "avgmix": None,   # filled in pass 2
                    "wind": wind_score,
                    "park": park["factor"],
                    "l15hr": None,    # filled in pass 2
                    "l5hr": None,     # filled in pass 2
                    "l15hrr": None,   # filled in pass 2
                    "l15tb": None,    # filled in pass 2
                    "risp": None,     # filled in pass 2
                    "lbonus": max(1, 9 - order_pos),
                    "hrrLbonus": hrr_lineup_bonus(order_pos),
                    "crush": 0,       # finalized after pass 2
                    "split": 0,
                    "hrprob": None,
                }
                rows.append((player_row, batter_id, pitcher_hand))

    print(f"  sides with a probable pitcher: {sides_with_pitcher} "
          f"(missing: {sides_missing_pitcher})")
    print(f"  sides with a CONFIRMED lineup: {sides_confirmed_lineup}")
    print(f"  sides using a PROJECTED lineup (from last game): {sides_projected_lineup}")
    print(f"  sides with no lineup available at all: {sides_no_lineup_at_all}")

    # ---- PASS 2: the slow part - platoon split, full game log (for L15 HR
    # and the player detail view), prior-season totals, and any missing name
    # lookups - run CONCURRENTLY across many threads instead of one at a time.
    # This is what previously made the whole run take 4-10+ minutes; with
    # ~20 requests in flight at once instead of 1, it should stay well under
    # a couple minutes for a typical ~270-player slate even with the extra
    # per-player calls the detail view now needs.
    print(f"Fetching per-player matchup data ({len(rows)} players, concurrently)...")

    def fetch_one(item):
        player_row, batter_id, pitcher_hand = item
        if not player_row["player"]:
            player_row["player"] = get_player_name(batter_id)
        platoon_avg = get_platoon_split(batter_id, pitcher_hand)

        games_this_year = get_gamelog(batter_id, YEAR)
        totals_prev_year = get_season_totals_hitting(batter_id, YEAR - 1)
        last20 = games_this_year[-20:]
        l15hr = sum(g["hr"] for g in games_this_year[-15:]) if games_this_year else None
        # Last-5-game HR count - a fast-reacting "is this guy hot right now"
        # signal to sit alongside l15hr's steadier 15-game base rate. See
        # compute_score()'s "recent" factor below for how the two blend.
        l5hr = sum(g["hr"] for g in games_this_year[-5:]) if games_this_year else None
        # Games clearing the 1.5 HRR line (H+R+RBI >= 2) - feeds the HRR
        # board's "recent form" the same way l15hr feeds the HR board.
        l15hrr = (sum(1 for g in games_this_year[-15:] if g["hrr"] >= 2)
                  if games_this_year else None)
        # Games clearing the 1.5 TB line (2+ total bases) - same pattern,
        # feeds the new TB board's recent-form factor.
        l15tb = (sum(1 for g in games_this_year[-15:] if g["tb"] >= 2)
                 if games_this_year else None)
        # Raw last-15 hits count (not a "clear the line" count like the two
        # above) - just the actual number of hits, for the HRR board's
        # dedicated Hits display so it's not only implied inside the
        # combined HRR line-clearing count.
        l15hits = (sum(g["hits"] for g in games_this_year[-15:])
                   if games_this_year else None)
        risp = get_risp_avg(batter_id)

        player_row["avgmix"] = platoon_avg
        player_row["l15hr"] = l15hr
        player_row["l5hr"] = l5hr
        player_row["l15hrr"] = l15hrr
        player_row["l15tb"] = l15tb
        player_row["l15hits"] = l15hits
        player_row["risp"] = risp
        player_row["crush"] = 1 if (platoon_avg or 0) >= 0.280 else 0
        player_row["split"] = 1 if (platoon_avg or 0) >= 0.260 else 0
        # Powers the player detail view (Graph + Stats tabs): last 20 games
        # plus L5/L10/L20/season splits, precomputed here so tapping a card
        # on the site is instant - no extra API calls from the browser.
        player_row["gamelog"] = {
            "games": last20,
            "l5": window_stats(last20[-5:]),
            "l10": window_stats(last20[-10:]),
            "l20": window_stats(last20[-20:]),
            "seasonCur": window_stats(games_this_year),
            "seasonPrev": season_totals_to_window(totals_prev_year),
            "yearCur": YEAR,
            "yearPrev": YEAR - 1,
        }
        return player_row

    import concurrent.futures
    players = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        for i, result in enumerate(executor.map(fetch_one, rows)):
            players.append(result)
            if (i + 1) % 50 == 0:
                print(f"  ...{i + 1}/{len(rows)} done")

    for player_row in players:
        player_row.update(compute_score(player_row))
        player_row.update(compute_hrr_score(player_row))
        player_row.update(compute_tb_score(player_row))

    # Written sorted by the HR score for backward compatibility - the
    # frontend re-sorts client-side by hrrScore when the HRR tab is active.
    players.sort(key=lambda p: -p["score"])

    # allow_nan=False makes Python raise a clear error HERE if any stat somehow
    # came out as NaN/Infinity, instead of silently writing invalid JSON that
    # would then fail to parse in the browser with a cryptic "syntax error".
    with open("players.json", "w") as f:
        json.dump(players, f, indent=2, allow_nan=False)

    print(f"Wrote players.json with {len(players)} players.")


if __name__ == "__main__":
    main()
