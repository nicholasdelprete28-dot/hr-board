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
import math
import os
import time
import datetime
import concurrent.futures
from zoneinfo import ZoneInfo
import requests
from fetch_odds import normalize_name

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
                # Raw UTC ISO8601 timestamp - same field already trusted
                # elsewhere in this file for date sorting (get_recent_lineup,
                # get_recent_starter). The frontend converts this to the
                # viewer's own local time for display.
                "game_time": g.get("gameDate"),
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


def get_recent_starter(team_id):
    """FALLBACK for when a team hasn't officially announced today's probable
    pitcher yet (this can lag behind lineup confirmation, and some teams
    announce later than others). Without this, a batter facing that team
    gets skipped ENTIRELY, not just marked unconfirmed - the whole
    opposing lineup silently disappears from the board rather than
    showing a projected read. Falls back to whoever started this team's
    most recently completed game, same "recent form as a stand-in for
    today" philosophy as get_recent_lineup() above. Returns a probable-
    pitcher-shaped dict ({"id", "fullName", "pitchHand"}) or None."""
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
        finished.sort(key=lambda g: g["gameDate"], reverse=True)
        for g in finished:
            side = "home" if g["teams"]["home"]["team"]["id"] == team_id else "away"
            box = statsapi_get(f"game/{g['gamePk']}/boxscore")
            team_box = box.get("teams", {}).get(side, {})
            pitcher_ids = team_box.get("pitchers", [])
            if not pitcher_ids:
                continue
            starter_id = pitcher_ids[0]  # first pitcher listed for the game = the starter
            person = team_box.get("players", {}).get(f"ID{starter_id}", {}).get("person", {})
            if not person:
                continue
            hand = person.get("pitchHand", {}).get("code", "R")
            return {"id": starter_id, "fullName": person.get("fullName", ""),
                    "pitchHand": {"code": hand}}
        return None
    except Exception:
        return None


def get_pitcher_hand_and_id(probable_pitcher):
    return probable_pitcher.get("id"), probable_pitcher.get("pitchHand", {}).get("code", "R")


def _parse_innings(ip_str):
    """MLB reports innings pitched as a string like '142.1' where the part
    after the decimal is OUTS recorded (0, 1, or 2) in that partial inning,
    NOT tenths of an inning - '142.1' means 142 and 1/3 innings, not
    142.1 innings. This converts it to a true decimal value."""
    if ip_str in (None, ""):
        return None
    whole_str, _, frac_str = str(ip_str).partition(".")
    try:
        whole = int(whole_str)
        thirds = int(frac_str) if frac_str else 0
    except ValueError:
        return None
    return whole + thirds / 3


def get_season_pitching_stats():
    """WHIP, HR/9, K/9, BB/9, ERA, and season K/IP/starts totals for every
    pitcher with a decision this season. WHIP/HR9 feed the HR/HRR/TB
    boards' pitcher-matchup factor (unchanged from before); K9/ERA/season
    totals feed the new K (strikeouts) board."""
    data = statsapi_get("stats", {
        "stats": "season", "group": "pitching", "season": YEAR, "sportId": 1, "limit": 1500
    })
    out = {}
    for split in data.get("stats", [{}])[0].get("splits", []):
        pid = split.get("player", {}).get("id")
        stat = split.get("stat", {})
        if pid and stat.get("whip") is not None:
            ip = _parse_innings(stat.get("inningsPitched"))
            gs = int(stat.get("gamesStarted", 0) or 0)
            era_raw = stat.get("era")
            try:
                era = float(era_raw) if era_raw not in (None, "", "-", "inf") else 4.20
            except (TypeError, ValueError):
                era = 4.20
            out[pid] = {
                "whip": float(stat["whip"]),
                "hr9": float(stat.get("homeRunsPer9", 1.2) or 1.2),
                "k9": float(stat.get("strikeoutsPer9Inn", 8.0) or 8.0),
                "bb9": float(stat.get("walksPer9Inn", 3.2) or 3.2),
                "era": era,
                "seasonK": int(stat.get("strikeOuts", 0) or 0),
                "ip": ip,
                "gamesStarted": gs,
                "ipPerStart": round(ip / gs, 1) if ip and gs else None,
            }
    return out


def get_team_k_rate():
    """Season strikeout rate (K% of plate appearances) for every team's
    LINEUP - not the team's own pitching staff. This is the K board's
    matchup signal: a pitcher facing a free-swinging, high-strikeout-rate
    lineup has a real edge, same idea as a batter facing a hittable
    pitcher on the other boards."""
    try:
        data = statsapi_get("teams/stats", {
            "stats": "season", "group": "hitting", "season": YEAR, "sportId": 1
        })
        out = {}
        for split in data.get("stats", [{}])[0].get("splits", []):
            team_id = split.get("team", {}).get("id")
            stat = split.get("stat", {})
            so = stat.get("strikeOuts")
            pa = stat.get("plateAppearances")
            if team_id and so is not None and pa:
                out[team_id] = round(int(so) / int(pa), 3)
        return out
    except Exception:
        return {}


def get_pitcher_gamelog(pitcher_id, season):
    """Every start this pitcher has made in `season`, oldest first, with
    strikeouts and innings pitched per start - feeds the K board's
    recent-form (last-3-starts) number. Relief appearances (if any) are
    skipped so a spot-start reliever's low-inning outing doesn't dilute
    the "as a starter" read."""
    try:
        data = statsapi_get(f"people/{pitcher_id}/stats", {
            "stats": "gameLog", "group": "pitching", "season": season, "sportId": 1
        })
        splits = data.get("stats", [{}])[0].get("splits", [])
        starts = []
        for s in splits:
            stat = s.get("stat", {})
            opp = s.get("opponent", {}) or {}
            if not stat.get("gamesStarted"):
                continue
            starts.append({
                "date": s.get("date"),
                "opp": team_abbr(opp) if opp else "",
                "k": int(stat.get("strikeOuts", 0) or 0),
                "ip": _parse_innings(stat.get("inningsPitched")),
            })
        starts.sort(key=lambda g: g["date"] or "")
        return starts
    except Exception:
        return []


def get_pitcher_season_stats(pitcher_id, season):
    """This one pitcher's own season pitching line, fetched directly
    rather than pulled from the bulk league-wide stats/season/pitching
    leaderboard. The K board only needs this for today's ~15-16 probable
    starters - a small enough set that a reliable per-pitcher call beats
    depending on whatever sort/pagination quirk was silently dropping
    real starters from the bulk list even at a generous limit (confirmed
    happening in practice - a real starter's season stats came back
    completely blank despite his per-start game log fetching fine, which
    only makes sense if he simply wasn't present in that bulk response).
    If a pitcher was traded mid-season, MLB's API can return one split per
    team - this combines them into one real season-total line instead of
    just taking whichever split happens to come first."""
    try:
        data = statsapi_get(f"people/{pitcher_id}/stats", {
            "stats": "season", "group": "pitching", "season": season, "sportId": 1
        })
        splits = data.get("stats", [{}])[0].get("splits", [])
        if not splits:
            return {}
        total_outs = 0
        total_k = total_bb = total_gs = total_hr = 0
        total_er = 0.0
        hits_plus_walks = 0.0
        for s in splits:
            stat = s.get("stat", {})
            ip = _parse_innings(stat.get("inningsPitched")) or 0
            total_outs += round(ip * 3)
            total_k += int(stat.get("strikeOuts", 0) or 0)
            total_bb += int(stat.get("baseOnBalls", 0) or 0)
            total_er += float(stat.get("earnedRuns", 0) or 0)
            total_gs += int(stat.get("gamesStarted", 0) or 0)
            total_hr += int(stat.get("homeRuns", 0) or 0)
            hits_plus_walks += float(stat.get("hits", 0) or 0) + float(stat.get("baseOnBalls", 0) or 0)
        ip_total = total_outs / 3
        if ip_total <= 0:
            return {}
        # Hard sanity clamp: innings-per-start can never realistically
        # exceed a complete game (9), and average IP/start above ~8 is
        # already essentially impossible for a modern starter. Without
        # this, a pitcher who had even one relief outing mixed into his
        # season (his innings from that outing count toward total_outs,
        # but gamesStarted doesn't increment) can produce a nonsensical
        # inflated ratio - this catches that regardless of the exact
        # cause rather than trusting the raw division blindly.
        ip_per_start = round(ip_total / total_gs, 1) if total_gs else None
        if ip_per_start is not None:
            ip_per_start = min(ip_per_start, 8.0)
        return {
            "whip": round(hits_plus_walks / ip_total, 2),
            "hr9": round(total_hr * 9 / ip_total, 2),
            "k9": round(total_k * 9 / ip_total, 2),
            "bb9": round(total_bb * 9 / ip_total, 2),
            "era": round(total_er * 9 / ip_total, 2),
            "seasonK": total_k,
            "ip": round(ip_total, 1),
            "gamesStarted": total_gs,
            "ipPerStart": ip_per_start,
        }
    except Exception:
        return {}


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
            "group": "hitting", "season": YEAR, "sportId": 1
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
            "group": "hitting", "season": YEAR, "sportId": 1
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
            "stats": "gameLog", "group": "hitting", "season": season, "sportId": 1
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
            "stats": "season", "group": "hitting", "season": season, "sportId": 1
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


def fetch_pitch_mix_data():
    """Pulls TWO things in one bulk call each from Baseball Savant's
    pitch-arsenal-stats leaderboard: (1) every BATTER's performance
    (hard-hit%) against each individual pitch type this season, and (2)
    every PITCHER's own pitch-type usage mix (what % of their pitches are
    fastballs vs sliders vs curves etc). Blended together in
    compute_pitch_mix_match() below, this answers a sharper question than
    the existing handedness-only platoon split: not just "does he hit
    lefties/righties well" but "does his swing profile match what THIS
    specific pitcher actually throws."

    Like fetch_batter_statcast() above, the exact column names AND
    whether this endpoint returns all pitch types unfiltered in one call
    are both best-effort assumptions - can't be verified without a live
    run. Prints the raw columns/row count/sample either way, same as
    that function, so a wrong assumption is immediately diagnosable from
    the Action log instead of silently producing nothing.

    Returns (batter_pitch_data, pitcher_pitch_mix):
      batter_pitch_data: {batter_id: {pitch_type: hard_hit_pct_0_to_1}}
      pitcher_pitch_mix: {pitcher_id: {pitch_type: usage_fraction_0_to_1}}
    """
    batter_pitch_data = {}
    pitcher_pitch_mix = {}

    for kind, out_dict in [("batter", batter_pitch_data), ("pitcher", pitcher_pitch_mix)]:
        url = (f"https://baseballsavant.mlb.com/leaderboard/pitch-arsenal-stats"
               f"?type={kind}&year={YEAR}&min=1&csv=true")
        try:
            resp = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            text = resp.content.decode("utf-8-sig")
            reader = csv.DictReader(io.StringIO(text))
            rows = list(reader)
            print(f"  Pitch-arsenal ({kind}) CSV columns: {reader.fieldnames}")
            print(f"  Pitch-arsenal ({kind}) CSV row count: {len(rows)}")
            if rows:
                print(f"  sample row: {rows[0]}")
        except Exception as e:
            print(f"  WARNING: pitch-arsenal ({kind}) fetch failed ({e}) - pitch-mix "
                  f"matching will fall back to handedness-only for everyone.")
            continue

        id_columns = ["player_id", "batter", "pitcher", "id", "mlb_id", "mlbam_id"]
        id_col = next((c for c in id_columns if rows and c in rows[0]), None)
        pitch_type_columns = ["pitch_type", "pitch_name"]
        pitch_col = next((c for c in pitch_type_columns if rows and c in rows[0]), None)
        usage_columns = ["pitch_usage", "usage", "pitch_percent"]
        usage_col = next((c for c in usage_columns if rows and c in rows[0]), None)
        metric_columns = ["hard_hit_percent", "ev95percent", "whiff_percent"]
        metric_col = next((c for c in metric_columns if rows and c in rows[0]), None)
        print(f"  ({kind}) using id={id_col} pitch_type={pitch_col} "
              f"usage={usage_col} metric={metric_col}")

        if not (id_col and pitch_col):
            print(f"  WARNING: couldn't identify required columns for {kind} "
                  f"pitch-arsenal data - skipping, will fall back to handedness-only.")
            continue

        for row in rows:
            try:
                pid = int(row[id_col])
                pitch_type = row[pitch_col]
            except (ValueError, KeyError, TypeError):
                continue
            out_dict.setdefault(pid, {})
            if kind == "pitcher" and usage_col:
                try:
                    usage_raw = float(row[usage_col])
                    # Usage might come back as a percent (45.2) or already
                    # a fraction (0.452) - normalize to a 0-1 fraction.
                    out_dict[pid][pitch_type] = usage_raw / 100 if usage_raw > 1 else usage_raw
                except (ValueError, TypeError):
                    pass
            elif kind == "batter" and metric_col:
                try:
                    metric_raw = float(row[metric_col])
                    out_dict[pid][pitch_type] = metric_raw / 100 if metric_raw > 1 else metric_raw
                except (ValueError, TypeError):
                    pass

    print(f"  parsed pitch-mix data for {len(batter_pitch_data)} batters, "
          f"{len(pitcher_pitch_mix)} pitchers")
    return batter_pitch_data, pitcher_pitch_mix


def compute_pitch_mix_match(batter_id, pitcher_id, batter_pitch_data, pitcher_pitch_mix):
    """0-1 score: this batter's hard-hit% weighted by how often the
    OPPOSING PITCHER actually throws each pitch type - a sharper question
    than plain handedness. Returns None (not 0) when there isn't enough
    real data to trust, so compute_score's blend can cleanly fall back to
    handedness-only instead of treating "no data" the same as "bad
    matchup" - a None here should never quietly drag a score down."""
    batter_data = batter_pitch_data.get(batter_id)
    mix = pitcher_pitch_mix.get(pitcher_id)
    if not batter_data or not mix:
        return None
    weighted_sum = 0.0
    weight_total = 0.0
    for pitch_type, usage in mix.items():
        if pitch_type in batter_data:
            weighted_sum += usage * batter_data[pitch_type]
            weight_total += usage
    if weight_total < 0.3:  # too little real overlap to trust the result
        return None
    return weighted_sum / weight_total


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


# Same shrink-toward-average philosophy as power_sample_weight() above,
# but for the OPPOSING PITCHER's own HR9/WHIP - a gap that let a pitcher
# with only a few innings this season (a real example: 0.00 HR/9, simply
# because he hasn't faced enough batters yet for a home run to show up)
# get trusted at FULL strength in the matchup multiplier below, cutting
# a batter's projected HR rate roughly in half off what's almost
# certainly small-sample noise, not a real HR-suppression skill. K=20
# innings is roughly where a pitcher's own rate stats start being a
# real signal rather than early-season noise.
PITCHER_SHRINK_K = 20


def pitcher_sample_weight(ip):
    if ip is None or ip <= 0:
        return 0.0  # no real innings on record yet - don't trust the rate at all
    return min(1.0, ip / (ip + PITCHER_SHRINK_K))


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
    # Platoon: blends the sharper pitch-mix match (how well this batter's
    # actual performance profile lines up against what THIS pitcher
    # specifically throws, weighted by his real usage rates) with the
    # original handedness-only read, rather than replacing it outright -
    # pitch-mix data isn't always available (thin sample, fetch failure),
    # and handedness alone is still a real, working signal on its own.
    # 70/30 favoring the sharper signal when it's there; falls back to
    # 100% handedness when it isn't, rather than treating missing data
    # as a bad matchup.
    handedness_platoon = (crush + split) / 2
    pitch_mix_raw = p.get("pitchMixMatch")  # this batter's weighted hard-hit% vs the arsenal, or None
    if pitch_mix_raw is not None:
        pitch_mix_norm = clamp01(pitch_mix_raw / 0.45)  # ~45% hard-hit vs arsenal = elite
        platoon = pitch_mix_norm * 0.7 + handedness_platoon * 0.3
    else:
        platoon = handedness_platoon
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


# ---------------------------------------------------------------------------
# "Value" probability model - a SECOND, independent probability estimate
# for HR/HRR/TB, separate from each board's composite favorability score
# above. The composite score is a weighted comparison scale (0-100),
# useful for ranking players against each other, but it was never
# calibrated to BE a real probability of clearing a specific line - it's
# a heuristic rescale for display (see DISPLAY_PCT_FLOOR/CEILING on the
# frontend), not something that should be directly compared to a real
# sportsbook's implied probability.
#
# This builds a genuinely calibrated probability instead, the same
# rigorous way the K board's Poisson model works: start from the
# player's own REAL recent per-game rate (not a composite score), adjust
# for today's specific matchup, and get an actual modeled probability -
# something that can be honestly compared against the market's price to
# find where the market and our model disagree (a real "value" signal),
# rather than comparing two different kinds of numbers that only look
# alike.
# ---------------------------------------------------------------------------
LEAGUE_AVG_PITCHER_HR9 = 1.20
LEAGUE_AVG_PITCHER_WHIP = 1.30
# Rough league-average anchors this shrink pulls toward for a thin sample -
# same role as LEAGUE_AVG_ISO/BARREL/etc above, just for these two new
# probability models specifically.
LEAGUE_AVG_HR_RATE = 0.12     # ~HRs per game across an average everyday MLB hitter
# NOTE: HRR and TB do NOT share one real-world clearing rate - a hitter's
# average combined H+R+RBI per game (~1.8) clears the 1.5 line noticeably
# more often than his average total-bases (~1.4) does, since a walk/other
# non-hit event can still add a run or RBI toward HRR but contributes
# nothing to TB. Using a single shared constant for both understated BOTH
# probabilities relative to their own real baseline (confirmed against
# actual sportsbook consensus: HRR's true clear rate lands close to ~52%,
# TB's closer to ~43% - see compute_hrr_probability's own docstring,
# which already documented the ~52% target this constant was silently
# failing to hit).
LEAGUE_AVG_HRR_CLEAR_RATE = 0.52  # ~rate of clearing the fixed 1.5 HRR line across all hitters
LEAGUE_AVG_TB_CLEAR_RATE = 0.43   # ~rate of clearing the fixed 1.5 TB line across all hitters


def compute_hr_probability(p):
    """HR is a raw COUNT stat (how many home runs, not "did he clear a
    rate"), so this uses the same Poisson approach as the K board: a
    real per-game mean adjusted by today's specific pitcher, then
    P(HR >= 1) via the Poisson model.

    Recent form (l15hr/l5hr) is blended against the player's OWN season
    power level (from ISO) rather than a flat league-average anchor -
    critical fix, since a flat anchor left the model structurally blind
    to whether a hot recent stretch was actually backed by real,
    established power. A player who's fundamentally a modest power bat
    but caught a fluky hot 15-game run would get modeled almost entirely
    off that stretch, which isn't a real signal - it's a hot-streak-with-
    long-odds illusion, not genuine value. Even for a player with a large
    season PA sample, a 15-20 game recent window is still small and
    noisy ON ITS OWN, so recent form is capped at 40% weight regardless
    of how established the player is - the rest anchors to his real
    season-long power level, with an ADDITIONAL pull toward that same
    anchor for players who don't have enough PA to trust at all (the
    original sample-size fix, now anchored to something more meaningful
    than a flat league constant)."""
    l15hr = p.get("l15hr")
    if l15hr is None:
        return None
    l5hr = p.get("l5hr")
    if l5hr is not None:
        recent_rate = (l15hr / 15) * 0.6 + (l5hr / 5) * 0.4
    else:
        recent_rate = l15hr / 15

    # This player's OWN season-implied HR rate, built from the SAME 4
    # power indicators the composite score's own "power" bucket uses
    # (barrel%, exit velo, ISO, hard-hit%) - not just ISO alone. Using
    # only ISO left this model blind to real signals the card itself
    # already shows and weighs (a guy could have modest ISO but elite
    # barrel%/EV, or vice versa) - this keeps the two models genuinely
    # comparable instead of one running on a narrower slice of the same
    # data. barrel_confidence() is the same smoothed EV-vs-barrel% trust
    # curve used in compute_score, so a barrel rate unsupported by real
    # exit velo gets the same discount here that it gets there.
    barrel = p.get("barrel") or 0
    ev = p.get("ev") or 85
    iso = p.get("iso") or 0
    hardhit = p.get("hardhit") or 0.30
    barrel_adj = barrel * barrel_confidence(barrel, ev)
    power_quality = (clamp01(barrel_adj / 0.25) + clamp01((ev - 85) / 15)
                      + clamp01(iso / 0.4) + clamp01((hardhit - 0.3) / 0.4)) / 4
    # power_quality is 0-1 (roughly 0.3-0.4 = a league-average power
    # profile, higher = real elite power across the board). Scaled onto
    # a realistic HR-rate range: a totally powerless profile floors at
    # 30% of league average, a maxed-out elite profile caps near 1.7x.
    # Ceiling raised from 1.4x to 2.0x - the old ceiling capped even a
    # maxed-out elite power profile around ~22-28% probability, which is
    # below what real sportsbook pricing implies for a genuinely great
    # HR matchup (typically +200 to +350, i.e. 22-33% implied). That gap
    # meant the model could almost never clear the market's price, so
    # the HR board's value tab was structurally starved of real edges
    # regardless of how good a matchup actually was. Verified against
    # real live players (Olson, Ohtani, Harper) before picking this
    # number, not guessed.
    season_implied_rate = LEAGUE_AVG_HR_RATE * (0.3 + power_quality * 2.0)

    RECENT_TRUST = 0.6  # raised from 0.4 - HR specifically needs recent hot streaks
    # to carry real weight, since that's the exact signal being hunted for (a
    # player heating up before the market/books catch up). HRR and TB keep the
    # original 0.4 - not broken, this change is scoped to HR only. Verified
    # this doesn't create false positives on cold streaks - a cold player's
    # hrProb actually drops FURTHER at 0.6 than at 0.4, since the same
    # weighting cuts both directions.
    blended_rate = recent_rate * RECENT_TRUST + season_implied_rate * (1 - RECENT_TRUST)

    # Additional shrink toward the season-implied anchor specifically for
    # players without a real established MLB track record (thin/no PA) -
    # same mechanism as before, just pulling toward a smarter anchor now.
    pw = power_sample_weight(p.get("pa"))
    base_rate = blended_rate * pw + season_implied_rate * (1 - pw)

    phr9 = p.get("phr9") if p.get("phr9") is not None else LEAGUE_AVG_PITCHER_HR9
    # Shrink the pitcher's own HR9 toward league average based on HIS OWN
    # innings-pitched sample - a pitcher with only a handful of innings
    # showing an eye-catching 0.00 HR9 hasn't demonstrated real HR-
    # suppression skill, he just hasn't faced enough batters yet. Without
    # this, that "0.00" got trusted at full strength and could cut a
    # batter's projection roughly in half off what's almost certainly
    # noise - see pitcher_sample_weight() above.
    pw_pitcher = pitcher_sample_weight(p.get("pip"))
    effective_phr9 = phr9 * pw_pitcher + LEAGUE_AVG_PITCHER_HR9 * (1 - pw_pitcher)
    # Dampened 50% so one very homer-prone or very stingy pitcher doesn't
    # swing the projection further than a real matchup edge should -
    # same philosophy as the K board's matchup multiplier.
    matchup_mult = 1 + ((effective_phr9 / LEAGUE_AVG_PITCHER_HR9) - 1) * 0.5
    mean = max(0.01, base_rate * matchup_mult)
    raw_prob = poisson_over_prob(mean, 0.5)  # P(1 or more)
    # Hard sanity ceiling, grounded in real sportsbook pricing rather than
    # a guess: even the shortest real "anytime HR" lines ever offered for
    # the best sluggers in the best matchups sit around +196 to +220
    # (31-34% implied) - the model stacking several favorable real
    # factors on one player (hot streak + strong power + a genuinely
    # homer-prone pitcher) can otherwise compound past what any real book
    # actually prices, even though each individual input is legitimate.
    # 30% keeps real exceptional cases meaningfully high without drifting
    # into territory no real market has ever actually offered.
    return min(raw_prob, 0.30) if raw_prob is not None else raw_prob


LEAGUE_AVG_AVG = 0.245
LEAGUE_AVG_OBP = 0.315


def compute_hrr_probability(p):
    """Same anchor-to-season-level fix as compute_hr_probability above,
    applied to HRR. Recent clearing rate (l15hrr) is capped at 40%
    weight and blended against a season-quality anchor built from real
    AVG/OBP - on-base ability drives Hits+Runs+RBI broadly, not raw
    power specifically, so this uses contact/on-base stats as the
    anchor rather than ISO. Without this, a hot 15-game stretch from a
    hitter with a genuinely below-average season line (like a part-time
    or currently-slumping regular catching one good week) gets modeled
    as if it were a fully proven, sustainable rate - which is exactly
    what produced a wildly inflated 73.6% for a real player whose season
    AVG/OBP were both below league average, when the market's own price
    (and this fix) both land close to a real ~52%.

    IMPORTANT: `quality` (the AVG/OBP ratio vs league average) is used
    as an ADDITIVE nudge off LEAGUE_AVG_HRR_CLEAR_RATE, not a raw
    multiplier on it. A straight multiplier (quality * anchor) compounds
    badly for elite-contact hitters: their quality ratio can run 1.2-1.3x
    league average, which - once also stacked with a hot recent stretch
    (up to 40% weight above) and a soft-matchup multiplier below - pushed
    a real elite contact hitter's number to 74.6% against a book price of
    58.3%, a swing way past anything a "value" signal should be flagging
    for an efficiently-priced star. QUALITY_SLOPE controls how many
    points of probability one full unit of quality above/below average is
    worth - keeping it well under 1.0 means the anchor itself (not the
    quality ratio) still does most of the work of hitting the real ~52%
    average, while a truly elite or truly weak hitter only shifts a
    bounded amount off that anchor instead of scaling it."""
    l15hrr = p.get("l15hrr")
    if l15hrr is None:
        return None
    recent_rate = clamp01(l15hrr / 15)

    avg = p.get("avg")
    obp = p.get("obp")
    QUALITY_SLOPE = 0.25
    if avg is not None and obp is not None:
        quality = ((avg / LEAGUE_AVG_AVG) + (obp / LEAGUE_AVG_OBP)) / 2
        season_implied_rate = clamp01(
            LEAGUE_AVG_HRR_CLEAR_RATE + QUALITY_SLOPE * (quality - 1)
        )
    else:
        season_implied_rate = LEAGUE_AVG_HRR_CLEAR_RATE

    RECENT_TRUST = 0.4  # even a hot, well-established stretch caps out at 40% weight
    blended_rate = recent_rate * RECENT_TRUST + season_implied_rate * (1 - RECENT_TRUST)

    pw = power_sample_weight(p.get("pa"))
    base_rate = blended_rate * pw + season_implied_rate * (1 - pw)

    whip = p.get("whip") if p.get("whip") is not None else LEAGUE_AVG_PITCHER_WHIP
    matchup_mult = 1 + ((whip / LEAGUE_AVG_PITCHER_WHIP) - 1) * 0.5
    return clamp01(base_rate * matchup_mult)


def compute_tb_probability(p):
    """Same fix, applied to Total Bases - anchored to a BLEND of contact
    (AVG/OBP) and power (barrel%/EV/ISO/hard-hit%), matching the same
    two-factor philosophy the composite TB score itself uses (any hit
    counts for at least 1 base, extra bases count for more - TB isn't a
    pure power stat the way HR is), rather than power alone."""
    l15tb = p.get("l15tb")
    if l15tb is None:
        return None
    recent_rate = clamp01(l15tb / 15)

    avg = p.get("avg")
    obp = p.get("obp")
    contact_quality = (((avg / LEAGUE_AVG_AVG) + (obp / LEAGUE_AVG_OBP)) / 2
                        if avg is not None and obp is not None else 1.0)

    barrel = p.get("barrel") or 0
    ev = p.get("ev") or 85
    iso = p.get("iso") or 0
    hardhit = p.get("hardhit") or 0.30
    barrel_adj = barrel * barrel_confidence(barrel, ev)
    power_quality = (clamp01(barrel_adj / 0.25) + clamp01((ev - 85) / 15)
                      + clamp01(iso / 0.4) + clamp01((hardhit - 0.3) / 0.4)) / 4
    power_multiplier = 0.3 + power_quality * 1.4

    # Blend contact and power (roughly matching the composite TB score's
    # own ~45/55 contact-to-power weighting), scaled onto a realistic
    # clearing-rate range.
    combined_multiplier = contact_quality * 0.45 + power_multiplier * 0.55
    season_implied_rate = clamp01(LEAGUE_AVG_TB_CLEAR_RATE * combined_multiplier)

    RECENT_TRUST = 0.4
    blended_rate = recent_rate * RECENT_TRUST + season_implied_rate * (1 - RECENT_TRUST)

    pw = power_sample_weight(p.get("pa"))
    base_rate = blended_rate * pw + season_implied_rate * (1 - pw)

    whip = p.get("whip") if p.get("whip") is not None else LEAGUE_AVG_PITCHER_WHIP
    matchup_mult = 1 + ((whip / LEAGUE_AVG_PITCHER_WHIP) - 1) * 0.5
    return clamp01(base_rate * matchup_mult)


# ---------------------------------------------------------------------------
# K (Strikeouts) board scoring, v2 - the first PITCHER-side board. Unlike
# the three batter boards, which all share one fixed universal line (1.5),
# a real strikeout prop line varies enormously by pitcher - an ace's line
# might be 7.5, a backend starter's might be 3.5. So instead of scoring
# against a fixed threshold, this computes an actual per-pitcher projection
# and derives THAT pitcher's own line from it, then uses a Poisson model
# (the standard statistical model for a bounded count stat like strikeouts
# in a start) to get a genuine probability of clearing it - that
# probability IS the headline "favorability" number, not an arbitrary
# weighted composite like the other boards use.
#
# Projection = today's expected innings x this pitcher's own K rate per
# inning x a matchup multiplier from the opposing lineup's strikeout rate
# (dampened 40% so one extreme opponent number doesn't swing it too hard),
# with a small, capped nudge from recent form.
# ---------------------------------------------------------------------------
LEAGUE_AVG_TEAM_K_RATE = 0.220


def poisson_over_prob(mean, line):
    """P(X > line) for X ~ Poisson(mean). `line` is a half-integer (e.g.
    4.5) so "over" is unambiguous: it means X >= the next whole number up
    (5, 6, 7...), with no possibility of a push. Built as an iterative
    running product rather than computing factorials directly, since a
    season's worth of strikeouts can make factorial(n) astronomically
    large - this way is numerically stable for any realistic mean."""
    if mean is None or mean <= 0:
        return None
    threshold = math.floor(line) + 1  # smallest whole count that clears the line
    cdf = math.exp(-mean)  # P(X=0)
    term = cdf
    for k in range(1, threshold):
        term *= mean / k
        cdf += term
    return max(0.0, min(1.0, 1 - cdf))


def compute_k_score(p):
    k9 = p.get("k9") if p.get("k9") is not None else 8.0
    era = p.get("era") if p.get("era") is not None else 4.20
    opp_k_rate = p.get("oppKRate") if p.get("oppKRate") is not None else LEAGUE_AVG_TEAM_K_RATE
    l3k = p.get("l3k") if p.get("l3k") is not None else 0
    ip_per_start = p.get("ipPerStart") if p.get("ipPerStart") is not None else 5.0

    # Matchup multiplier, dampened to 40% of the raw ratio so a lineup
    # that's wildly above/below league-average K rate doesn't swing the
    # projection further than a real matchup edge should.
    matchup_mult = 1 + ((opp_k_rate / LEAGUE_AVG_TEAM_K_RATE) - 1) * 0.4
    proj_k = ip_per_start * (k9 / 9) * matchup_mult

    # Small, capped recent-form nudge: if the last 3 starts ran meaningfully
    # hotter or colder than what the season rate alone would predict for 3
    # starts, shade the projection toward that - capped at +/-15% so a
    # single monster or disaster start can't dominate the projection.
    expected_l3 = proj_k * 3
    if expected_l3 > 0:
        nudge = clamp01((l3k - expected_l3) / (expected_l3 * 1.5) + 0.5) - 0.5
        proj_k *= (1 + nudge * 0.3)  # nudge in [-0.5,0.5] -> swing in [-15%,+15%]

    proj_k = max(0.5, round(proj_k, 2))

    # Model line: nearest half-integer AT OR BELOW the projection - the
    # standard prop-betting convention (half-lines can't push), and it
    # deliberately sits at/under the projected mean so the "over"
    # probability comes out meaningfully above a flat coinflip rather than
    # exactly 50/50 for every single pitcher, same way a real sportsbook's
    # opening number tends to sit slightly favorable-to-the-over on a
    # model projection.
    model_line = math.floor(proj_k * 2) / 2
    if model_line < 0.5:
        model_line = 0.5

    over_prob = poisson_over_prob(proj_k, model_line)
    score = round((over_prob if over_prob is not None else 0.5) * 100, 1)

    # Sub-factor percentages, kept as supplementary "why" context shown on
    # the card - they no longer drive the headline score directly, the
    # Poisson probability above does.
    whiff = (clamp01((k9 - 6.5) / 5.0) + clamp01((5.20 - era) / 2.50)) / 2
    matchup = clamp01((opp_k_rate - 0.170) / 0.110)
    recent = clamp01(l3k / 24)
    workload = clamp01((ip_per_start - 4.0) / 3.0)

    return {
        "kScore": score,
        "projK": proj_k,
        "kLine": model_line,
        "kWhiffPct": round(whiff * 100, 1),
        "kMatchupPct": round(matchup * 100, 1),
        "kRecentPct": round(recent * 100, 1),
        "kWorkloadPct": round(workload * 100, 1),
    }


def main():
    print("Fetching today's schedule and probable pitchers...")
    games = get_todays_games()
    print(f"  {len(games)} games today")

    print("Fetching season pitching stats (WHIP, HR/9)...")
    pitching_stats = get_season_pitching_stats()

    # The bulk stats/season/pitching list above is the SAME unreliable
    # source that turned out to silently drop real starters for the K
    # board (confirmed live - a real pitcher's season line came back
    # completely blank despite him definitely having pitched this
    # season). That fix (get_pitcher_season_stats, a reliable per-pitcher
    # call) only ever got wired into the K board's own pitcher pass - the
    # HR/HRR/TB boards' "opposing pitcher" WHIP/HR9 was still trusting the
    # same flaky bulk list, silently falling back to a hardcoded league-
    # average-ish default ({"whip": 1.30, "hr9": 1.20}) for any pitcher it
    # dropped, with nothing on the card distinguishing a real 1.30/1.20
    # from a missing one. Since today's probable starters are a small,
    # known set (the same ~30 pitchers the K board already fetches
    # individually), just fetch all of them reliably up front here too,
    # rather than trusting the bulk list for anyone.
    print("Fetching reliable per-pitcher season stats for today's probable starters...")
    todays_pitcher_ids = set()
    for g in games:
        for key in ("home_pitcher", "away_pitcher"):
            pp = g.get(key)
            if pp and pp.get("id"):
                todays_pitcher_ids.add(pp["id"])

    def fetch_one_pitcher_stat(pid):
        return pid, get_pitcher_season_stats(pid, YEAR)

    reliable_pitcher_stats = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        for pid, stats in executor.map(fetch_one_pitcher_stat, todays_pitcher_ids):
            if stats:
                reliable_pitcher_stats[pid] = stats
    print(f"  got reliable season stats for {len(reliable_pitcher_stats)} of "
          f"{len(todays_pitcher_ids)} probable starters "
          f"({len(todays_pitcher_ids) - len(reliable_pitcher_stats)} still falling back to the bulk list/default)")

    print("Fetching season batting stats (ISO)...")
    batting_stats = get_season_batting_stats()

    print("Fetching Statcast batter data (barrel%, EV, hard-hit%)...")
    statcast = fetch_batter_statcast()
    print(f"  parsed {len(statcast)} batters with Statcast data")

    print("Fetching pitch-mix data (batter vs pitch type, pitcher usage)...")
    batter_pitch_data, pitcher_pitch_mix = fetch_pitch_mix_data()

    # ---- PASS 1: build every player row using only data we already have in
    # memory (no network calls in this loop) - fast, a few seconds at most.
    rows = []  # each entry: (player_row_dict, batter_id, pitcher_hand)
    sides_with_pitcher = 0
    sides_confirmed_lineup = 0
    sides_projected_lineup = 0
    sides_missing_pitcher = 0
    sides_no_lineup_at_all = 0
    sides_projected_pitcher = 0
    sides_no_pitcher_at_all = 0

    for g in games:
        for side, opp_side in [("home", "away"), ("away", "home")]:
            team = g[f"{side}_team"]
            team_id = g[f"{side}_team_id"]
            opp_pitcher = g[f"{opp_side}_pitcher"]
            pitcher_confirmed = True
            if not opp_pitcher:
                opp_team_id = g[f"{opp_side}_team_id"]
                opp_pitcher = get_recent_starter(opp_team_id)
                pitcher_confirmed = False
                if opp_pitcher:
                    sides_projected_pitcher += 1
                    print(f"  using PROJECTED opposing pitcher ({opp_pitcher.get('fullName')}) "
                          f"for {team}'s batters - {g['away_team']} @ {g['home_team']}")
                else:
                    sides_no_pitcher_at_all += 1
                    sides_missing_pitcher += 1
                    print(f"  no opposing pitcher available (confirmed OR recent) for {team}'s "
                          f"batters - {g['away_team']} @ {g['home_team']}")
                    continue
            sides_with_pitcher += 1
            pitcher_id, pitcher_hand = get_pitcher_hand_and_id(opp_pitcher)
            pitcher_stat = reliable_pitcher_stats.get(
                pitcher_id, pitching_stats.get(pitcher_id, {"whip": 1.30, "hr9": 1.20}))

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
                    "playerType": "batter",
                    "player": name,
                    "team": team,
                    "pitcher": opp_pitcher.get("fullName", ""),
                    "hand": pitcher_hand,
                    "game": f"{g['away_team']} @ {g['home_team']}",
                    "lineupConfirmed": lineup_confirmed,
                    "pitcherConfirmed": pitcher_confirmed,
                    "gameStatus": g["status"],
                    "gameTime": g.get("game_time"),
                    "playerId": batter_id,
                    "pitchMixMatch": compute_pitch_mix_match(
                        batter_id, pitcher_id, batter_pitch_data, pitcher_pitch_mix),
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
                    "pip": pitcher_stat.get("ip"),  # opposing pitcher's innings pitched -
                                                     # used to shrink an unreliable small-
                                                     # sample HR9/WHIP toward league average
                                                     # instead of trusting it at full strength
                    "pip": pitcher_stat.get("ip"),  # opposing pitcher's innings pitched -
                                                     # used to shrink an unreliable small-
                                                     # sample HR9/WHIP toward league average
                                                     # instead of trusting it at full strength
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
          f"(fully missing: {sides_no_pitcher_at_all})")
    print(f"  sides using a PROJECTED opposing pitcher (from last start): {sides_projected_pitcher}")
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
        # Real calibrated probabilities (see compute_hr_probability's
        # docstring above), independent of the composite favorability
        # scores just computed - lets the frontend compare our actual
        # model probability against the market's real implied
        # probability for a genuine "value" signal.
        hr_prob = compute_hr_probability(player_row)
        if hr_prob is not None:
            player_row["hrProb"] = round(hr_prob * 100, 1)
        hrr_prob = compute_hrr_probability(player_row)
        if hrr_prob is not None:
            player_row["hrrProb"] = round(hrr_prob * 100, 1)
        tb_prob = compute_tb_probability(player_row)
        if tb_prob is not None:
            player_row["tbProb"] = round(tb_prob * 100, 1)

    # ---- PITCHERS: the K (strikeouts) board. Built as its own pass since
    # pitchers aren't part of the batter/lineup pipeline above at all - one
    # row per team per game (today's probable starter), deduped by pitcher
    # ID in case of a doubleheader. Reuses the same `games` schedule and
    # `pitching_stats` already fetched above.
    print("Fetching team strikeout rates (opposing-lineup matchup signal)...")
    team_k_rate = get_team_k_rate()

    pitcher_rows = {}  # keyed by pitcher_id, dedupes doubleheader duplicates
    for g in games:
        for side, opp_side in [("home", "away"), ("away", "home")]:
            pitcher = g[f"{side}_pitcher"]
            if not pitcher or not pitcher.get("id"):
                continue
            pitcher_id = pitcher["id"]
            if pitcher_id in pitcher_rows:
                continue
            opp_team_id = g[f"{opp_side}_team_id"]
            pstat = pitching_stats.get(pitcher_id, {})
            pitcher_rows[pitcher_id] = {
                "playerType": "pitcher",
                "player": pitcher.get("fullName", ""),
                "playerId": pitcher_id,
                "team": g[f"{side}_team"],
                "opponent": g[f"{opp_side}_team"],
                "game": f"{g['away_team']} @ {g['home_team']}",
                "gameStatus": g["status"],
                "gameTime": g.get("game_time"),
                "hand": pitcher.get("pitchHand", {}).get("code", "R"),
                "k9": pstat.get("k9"),
                "bb9": pstat.get("bb9"),
                "era": pstat.get("era"),
                "whip": pstat.get("whip"),
                "seasonK": pstat.get("seasonK"),
                "ipPerStart": pstat.get("ipPerStart"),
                "oppKRate": team_k_rate.get(opp_team_id),
                "l3k": None,   # filled below
                "l5k": None,   # filled below
            }

    print(f"Fetching per-pitcher recent form ({len(pitcher_rows)} starters, concurrently)...")

    def fetch_pitcher(item):
        pitcher_id, row = item
        # Reuse the reliable per-pitcher season stats already fetched
        # earlier in main() for the batter boards' matchup data - same
        # pitchers (today's probable starters), no need to fetch twice.
        own_season = reliable_pitcher_stats.get(pitcher_id) or get_pitcher_season_stats(pitcher_id, YEAR)
        if own_season:
            row.update(own_season)
        starts = get_pitcher_gamelog(pitcher_id, YEAR)
        row["l3k"] = sum(g["k"] for g in starts[-3:]) if starts else None
        row["l5k"] = sum(g["k"] for g in starts[-5:]) if starts else None

        # Blend in RECENT form for the two projection inputs (ipPerStart,
        # k9), rather than trusting the season-long average alone. A
        # season average can be stale if workload or stuff has changed
        # recently - a pitcher on a short leash after a rough stretch,
        # or working back from an injury, throws fewer innings NOW than
        # his full-season average suggests, and a real sportsbook line
        # already prices in that kind of current-form context we
        # otherwise can't see. 60/40 blend toward recent (last 5 starts),
        # same recency-weighting philosophy as the batter boards' L15/L5
        # blend - enough to actually move the projection, not so much
        # that a single short outing whipsaws it.
        last5 = starts[-5:]
        if last5:
            recent_ip_total = sum(g["ip"] or 0 for g in last5)
            recent_ip_avg = recent_ip_total / len(last5)
            recent_k_total = sum(g["k"] for g in last5)
            recent_k9 = (recent_k_total * 9 / recent_ip_total) if recent_ip_total > 0 else None

            season_ip = row.get("ipPerStart")
            row["ipPerStart"] = (round(recent_ip_avg * 0.6 + season_ip * 0.4, 1)
                                  if season_ip is not None else round(recent_ip_avg, 1))

            season_k9 = row.get("k9")
            if recent_k9 is not None:
                row["k9"] = (round(recent_k9 * 0.6 + season_k9 * 0.4, 2)
                              if season_k9 is not None else round(recent_k9, 2))

        row.update(compute_k_score(row))
        # Last 10 starts, for the player-detail bar chart on the frontend -
        # same idea as a batter's gamelog, just K's-per-start instead of
        # HR/hits-per-game.
        row["starts"] = starts[-10:]
        return row

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        pitchers = list(executor.map(fetch_pitcher, pitcher_rows.items()))

    print(f"  {len(pitchers)} probable starters found")
    players.extend(pitchers)

    # Carry forward each player's existing real sportsbook odds (bookOdds)
    # from the PREVIOUS players.json, rather than losing them every time
    # this script runs. fetch_hr_data.py rebuilds the whole file from
    # scratch on every run and has no concept of odds at all - without
    # this, ANY run of this script (even just to catch a lineup update)
    # would silently wipe every player's odds until fetch_odds.py ran
    # again, forcing a real API-credit spend just to undo the wipe. Real
    # tradeoff worth knowing: carried-forward odds can go stale (a line
    # moves, a player gets scratched) until the next actual fetch_odds.py
    # run refreshes them - that's the accepted cost of not burning
    # credits on every single fetch_hr_data.py run.
    if os.path.exists("players.json"):
        try:
            with open("players.json") as f:
                old_players = json.load(f)
            old_odds_by_name = {
                normalize_name(p.get("player", "")): p["bookOdds"]
                for p in old_players if p.get("bookOdds")
            }
            carried = 0
            for player_row in players:
                key = normalize_name(player_row.get("player", ""))
                if key in old_odds_by_name:
                    player_row["bookOdds"] = old_odds_by_name[key]
                    carried += 1
            print(f"Carried forward existing sportsbook odds for {carried} players "
                  f"from the previous players.json.")
        except Exception as e:
            print(f"  WARNING: couldn't carry forward previous odds ({e}) - "
                  f"bookOdds will be empty until fetch_odds.py runs again.")

    # Written sorted by the HR score for backward compatibility - the
    # frontend re-sorts client-side by whichever score field the active
    # board uses. .get(..., 0) since pitcher rows have kScore, not score.
    players.sort(key=lambda p: -p.get("score", 0))

    # allow_nan=False makes Python raise a clear error HERE if any stat somehow
    # came out as NaN/Infinity, instead of silently writing invalid JSON that
    # would then fail to parse in the browser with a cryptic "syntax error".
    with open("players.json", "w") as f:
        json.dump(players, f, indent=2, allow_nan=False)

    print(f"Wrote players.json with {len(players)} players.")

    write_daily_snapshot(players)


def write_daily_snapshot(players):
    """A small daily snapshot for the historical accuracy tracker - just
    each player's projected score/line, not the full row (that would bloat
    the repo fast at 3 runs/day, every day, forever). Overwritten on every
    run of the day, so the snapshot that survives is from the LAST run
    before games start - the most-confirmed lineup/matchup data of the
    day, which is what we actually want to grade against final results.
    check_results.py reads this the next day and compares it against
    actual box scores to build a real track record instead of just
    trusting the model blindly."""
    os.makedirs("history", exist_ok=True)
    # Use the same Eastern-time TODAY as the rest of the script (see the
    # comment at its definition near the top of the file) - NOT raw
    # datetime.date.today(), which on GitHub Actions runs in UTC. A run
    # after ~7-8pm Eastern would otherwise save this snapshot under
    # TOMORROW's date, silently breaking check_results.py's ability to
    # ever find it under today's actual date.
    date_str = TODAY
    snapshot = []
    for p in players:
        if p.get("playerType") == "pitcher":
            snapshot.append({
                "playerId": p.get("playerId"), "player": p.get("player"),
                "playerType": "pitcher", "team": p.get("team"), "opponent": p.get("opponent"),
                "kScore": p.get("kScore"), "kLine": p.get("kLine"),
            })
        else:
            snapshot.append({
                "playerId": p.get("playerId"), "player": p.get("player"),
                "playerType": "batter", "team": p.get("team"),
                "score": p.get("score"), "hrrScore": p.get("hrrScore"), "tbScore": p.get("tbScore"),
            })
    path = f"history/{date_str}.json"
    with open(path, "w") as f:
        json.dump(snapshot, f)
    print(f"Wrote daily snapshot to {path} ({len(snapshot)} players)")


if __name__ == "__main__":
    main()
