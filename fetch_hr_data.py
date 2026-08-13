"""
fetch_hr_data.py  (v3.13 - power subfactor recalibration + probability formula fix)

Builds today's HR favorability board with NO manual screenshots, using only
free, public data sources:

  - MLB Stats API (statsapi.mlb.com)  -> schedule, probable pitchers,
    confirmed lineups, season batting/pitching stats, platoon splits,
    day/night splits, per-start pitcher game logs
  - Baseball Savant CSV export        -> season AND last-15-day barrel%,
    exit velocity, hard-hit%
  - Open-Meteo weather API            -> live wind speed/direction per park

WHAT CHANGED IN v3.13 (per formula review - PRIME/STRONG discrimination):

  Both compute_hr_subfactors() and compute_hr_probability() were rewritten.
  compute_score() (the 0-100 composite) and compute_hr_probability()
  (hrProb, what actually drives the live PRIME/STRONG tiers) both build
  from compute_hr_subfactors() - fixing it once here fixes both, same as
  the file's existing design intent.

  1. POWER DENOMINATORS WERE CALIBRATED AGAINST RANGES THAT DON'T EXIST
     IN REAL BASEBALL.
     - Exit velocity: old formula was (ev-85)/15, meaning a hitter needed
       a 100mph AVERAGE exit velocity to max this component out. No
       hitter has ever done that - the most extreme peak seasons on
       record average around 93-95mph. So even a historically dominant
       EV season scored ~70-80%, never near the top of its own scale.
     - Hard-hit%: old formula was (hardhit-0.3)/0.4, requiring a 70%
       hard-hit rate to max out. The all-time record is around 55-58%.
       Same problem, worse - real elite seasons capped near 50-65% of
       this component's scale.
     Net effect: "power" (35% of compute_score, and the main driver of
     hrProb's season_implied_rate) could never fully credit a
     legitimately dominant hitter. That compresses everything built on
     top of it - which is exactly why PRIME/STRONG couldn't tell a
     merely-good spot from a truly elite one.

  2. FIVE-WAY FLAT AVERAGE OF REDUNDANT MEASURES.
     Barrel%, EV, and hard-hit% are all quality-of-contact PROCESS
     stats measuring roughly the same underlying thing. ISO and season
     HR rate are both power OUTCOME stats, also measuring roughly the
     same thing as each other. Flat-averaging all 5 together doesn't
     give 5 independent opinions - it means any single one having a
     lagging day (e.g. a guy in a short HR drought despite still
     squaring the ball up exactly as hard as ever) silently drags the
     average down even though the other 4 measures disagree.
     FIX: group into `quality_of_contact` (barrel/EV/hard-hit - what
     SHOULD be happening) and `converted_power` (ISO/season HR rate -
     what IS actually happening), average within each group, then blend
     the two groups 55/45. Same inputs, but redundancy inside a group no
     longer swamps the signal across groups.

  3. SEASON_IMPLIED_RATE'S OWN CEILING WAS TOO LOW.
     Even with a maxed-out power_quality (1.0), the old
     POWER_QUALITY_MULTIPLIER=1.3 only pushed season_implied_rate to
     1.6x league average. Widened to 1.7x now that power_quality can
     legitimately approach 1.0 for real elite profiles (previously it
     almost never could, per #1/#2 above).

  4. RECENT PRODUCTION WAS UNDERWEIGHTED.
     RECENT_TRUST=0.4 meant actual demonstrated recent homers only got
     40% weight vs 60% for the power-quality-implied rate. Moved to an
     even 50/50 - a guy legitimately hot right now should move the
     needle as much as his underlying Statcast profile does.

  5. REDUNDANT TAIL COMPRESSION.
     The old exponential soft-cap (0.22 -> 0.30) flattened every player
     above a 22% raw estimate toward roughly the same number, on top of
     the stacking dampener a few lines below it that already exists to
     keep unrealistic multi-factor pileups in check. Replaced with one
     hard sanity ceiling (0.45) that only clips truly extreme outputs
     instead of squeezing the entire top half of the range.

  DOWNSTREAM IMPACT - read before relying on tier cutoffs: real elite
  power profiles will now score meaningfully higher on both `score`
  (0-100) and `hrProb` than before - that's the point. It also means:
    - The stacking dampener (STACK_THRESHOLD=0.75 on power/pitcher_s/
      recent) will now actually engage on real elite hitters more often,
      since power can legitimately reach that threshold now. That's
      working as designed, not a bug.
    - The live PRIME/STRONG cutoffs in index.html's favTierClass()/
      favTierName() (currently >=20 / >=14 on hrProb) were tuned against
      the OLD compressed scale and will need to move up once a few days
      of real post-fix output are visible - don't guess the new numbers
      blind, let the distribution settle first.
    - check_results.py's BATTER_TIERS (55/40/35) grades the OLD `score`
      field with OLD thresholds and was already out of sync with the
      live site regardless of this change (separate bug, flagged
      earlier) - this rewrite doesn't fix that on its own.

WHAT CHANGED IN v3.12 (per formula review):
  - FIX #1: PARKS was only covering 15 of 30 teams. Any game hosted by the
    other 15 (LAA, AZ, DET, KC, LAD, WSH, NYM, ATH, SEA, STL, TB, TOR, MIN,
    CWS, MIA) silently fell back to {"factor": 0, "lat": None, "lon": None},
    which meant wind was never even fetched for those games (lat is None)
    and park_s/wind_s collapsed to neutral (0.5) regardless of the real
    park - including real, well-known extreme parks like Seattle
    (pitcher-friendly) and Coors-adjacent hitter parks like Dodger
    Stadium. All 30 teams are now present. HONESTY NOTE: the `factor`
    values for the 15 newly-added parks (and the `orient` values for ALL
    30 parks, see FIX #2) are reasonable placeholders based on each
    park's general reputation, NOT derived from real Statcast park-factor
    data. They should be replaced with real published park factors
    (Fangraphs/ESPN) and verified stadium orientations before being fully
    trusted - this fix closes the "half the league silently neutral" gap,
    it does not certify the specific numbers.
  - FIX #2: wind_park_factor() took a `direction` parameter and never used
    it - wind could only ever help a batter (0/1/2), never hurt, no matter
    which way it was blowing. Each park now carries an `orient` field
    (approximate compass bearing, home plate -> center field, degrees from
    north - same "needs real verification" caveat as the factor values
    above) and wind_park_factor() now computes the in/out component via
    the angle between actual wind direction and that bearing, so
    wind_score (and therefore wind_s) can now go negative for in-blowing
    wind, not just 0 or positive.
  - FIX #3: the stacking dampener in both compute_score() and
    compute_hr_probability() only ever looked at [power, pitcher_s,
    recent] - every other multiplier (home/road, day/night, bullpen,
    day-of-week, and the platoon/opportunity/park/wind nudges in
    compute_hr_probability) could still all stack favorably on the same
    player completely unchecked. Both functions now also fold in how much
    the situational multipliers alone are boosting a player, and count
    that toward the same "too many favorable factors piling up" trigger.

WHAT CHANGED IN v3 (see the project's weighting-reform discussion):
  - compute_score() reweighted from 5 buckets to 7: Power 35%, Pitcher 15%,
    Platoon 10%, Recent 15%, Opportunity 10%, Park 8%, Wind 7% (UPDATED
    v3.11: Pitcher moved 20%->15%, Power 30%->35% - pitcher_s is a
    per-game factor identical for every batter facing that pitcher, and
    at 20% it was a real contributor to same-team players stacking
    together in board order; see the v3.11 notes on compute_score() and
    compute_hr_probability()'s PITCHER_MATCHUP_SENSITIVITY for the full
    reasoning). Pitcher used
    to be diluted inside a blended "matchup" bucket (worth ~19% of that
    bucket's 30%, i.e. ~6% of the whole score) - it's now a standalone,
    genuinely meaningful lever, without being allowed to outweigh Power.
  - Power now blends SEASON Statcast power (barrel%/EV/hard-hit%, still the
    anchor) with a LAST-15-DAYS version of the same three stats, so a real
    recent power surge (or decline) that hasn't fully shown up in the
    season aggregate yet - or has started fading from it - actually moves
    the score. ISO stays season-only; TB/HRR are unaffected.
  - Pitcher's own hr9/whip now blend in his last-3-starts form (reusing
    get_pitcher_gamelog(), already built and verified working for the K
    board), the same way the K board already blends recent ipPerStart/k9.
  - Two new small, capped adjustments: a home/road HR-rate split (built
    entirely from the season gamelog already being fetched - no new API
    call) and a day/night HR-rate split (one new per-player stat-splits
    call). Both are bounded multipliers on the final score, not new
    percentage-of-100 buckets - see HOME_ROAD_MAX_ADJ / DAY_NIGHT_MAX_ADJ
    below for exactly how much either can move a score.

HONESTY NOTE ON NEW PIECES (same standard the rest of this file already
holds itself to): fetch_batter_statcast_l15() uses Baseball Savant's
"custom leaderboard" endpoint with a date range - this specific endpoint,
its param names, and its column names have NOT been confirmed against a
live response. get_day_night_split()'s sitCode ('day'/'night') is a
best-guess, same unverified status as get_risp_avg()'s 'risp' sitCode
already was in earlier versions of this file. Both print their raw
response shape either way, same defensive pattern as
fetch_batter_statcast() and fetch_pitch_mix_data() use, so a wrong
assumption is immediately visible in the Action's run log instead of
silently producing nothing. Test locally before trusting the automated
schedule, same advice the v2 docstring already gave.

KNOWN LIMITATION - SEASON TOTAL/RATE, NOT WITHIN-SEASON TREND: every
"recent" signal this file computes (the L15 power blend in compute_score(),
the L15 HR-rate opportunity bonus, the pitcher last-3-starts blend) is a
fixed recent window measured against a season baseline. None of it tracks
WHERE within the season a player's production happened. A batter who hit
most of his homers in April and has gone cold since will score the same,
on season total/rate, as a batter with the identical season total who is
heating up right now - the L15 window catches "hot at this moment" to some
degree, but not the shape of the trend leading up to it (accelerating vs.
decelerating within the season). A real trend/trajectory feature - e.g. a
rolling weekly HR rate with a slope or acceleration term - is a separate,
buildable addition and is NOT implemented anywhere in this file.
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
TODAY = datetime.datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
TODAY_WEEKDAY = datetime.datetime.now(ZoneInfo("America/New_York")).weekday()  # 0=Mon..6=Sun, for day_of_week_split()

# FIX #1 (v3.12): all 30 teams now present - previously only 15 were listed
# and any game at the other 15 parks silently fell back to a neutral
# factor with lat/lon = None, which also meant wind was never fetched for
# those games at all. `orient` (FIX #2) is the approximate compass bearing
# from home plate toward center field, degrees from true north, used to
# resolve whether a given wind direction is blowing "out" or "in".
#
# HONESTY NOTE: both `factor` (for the 15 newly-added parks) and `orient`
# (for all 30 parks) are reasonable placeholders based on each park's
# general reputation and rough stadium geometry - NOT pulled from a
# verified data source. Replace `factor` with real published park factors
# (Fangraphs/ESPN) and verify `orient` against real stadium diagrams
# before fully trusting either. The goal of this fix is that every game
# now gets a real, non-neutral, non-skipped park/wind read instead of a
# silent neutral default - not that these exact numbers are final.
PARKS = {
    "COL": {"factor": 2,  "lat": 39.7559, "lon": -104.9942, "orient": 30},
    "NYY": {"factor": 2,  "lat": 40.8296, "lon": -73.9262,  "orient": 75},
    "CIN": {"factor": 1,  "lat": 39.0975, "lon": -84.5068,  "orient": 100},
    "MIL": {"factor": 1,  "lat": 43.0280, "lon": -87.9712,  "orient": 130},
    "PHI": {"factor": 1,  "lat": 39.9061, "lon": -75.1665,  "orient": 5},
    "AZ":  {"factor": 1,  "lat": 33.4453, "lon": -112.0667, "orient": 55},
    "LAD": {"factor": 1,  "lat": 34.0739, "lon": -118.2400, "orient": 25},
    "HOU": {"factor": 0,  "lat": 29.7573, "lon": -95.3555,  "orient": 55},
    "CHC": {"factor": 0,  "lat": 41.9484, "lon": -87.6553,  "orient": 30},
    "BOS": {"factor": 0,  "lat": 42.3467, "lon": -71.0972,  "orient": 45},
    "ATL": {"factor": 0,  "lat": 33.8908, "lon": -84.4678,  "orient": 15},
    "KC":  {"factor": 0,  "lat": 39.0517, "lon": -94.4803,  "orient": 60},
    "TOR": {"factor": 0,  "lat": 43.6414, "lon": -79.3894,  "orient": 60},
    "MIN": {"factor": 0,  "lat": 44.9817, "lon": -93.2776,  "orient": 90},
    "LAA": {"factor": 0,  "lat": 33.8003, "lon": -117.8827, "orient": 20},
    "WSH": {"factor": 0,  "lat": 38.8730, "lon": -77.0074,  "orient": 55},
    "STL": {"factor": 0,  "lat": 38.6226, "lon": -90.1928,  "orient": 45},
    "CWS": {"factor": 0,  "lat": 41.8299, "lon": -87.6338,  "orient": 135},
    "BAL": {"factor": -1, "lat": 39.2839, "lon": -76.6218,  "orient": 30},
    "SF":  {"factor": -1, "lat": 37.7786, "lon": -122.3893, "orient": 95},
    "TEX": {"factor": -1, "lat": 32.7473, "lon": -97.0842,  "orient": 30},
    "CLE": {"factor": -1, "lat": 41.4962, "lon": -81.6852,  "orient": 5},
    "PIT": {"factor": -1, "lat": 40.4469, "lon": -80.0057,  "orient": 65},
    "SD":  {"factor": -1, "lat": 32.7073, "lon": -117.1566, "orient": 5},
    "NYM": {"factor": -1, "lat": 40.7571, "lon": -73.8458,  "orient": 30},
    "ATH": {"factor": -1, "lat": 37.7516, "lon": -122.2005, "orient": 45},
    "SEA": {"factor": -1, "lat": 47.5914, "lon": -122.3325, "orient": 45},
    "TB":  {"factor": -1, "lat": 27.7683, "lon": -82.6534,  "orient": 90},
    "DET": {"factor": -1, "lat": 42.3390, "lon": -83.0485,  "orient": 25},
    "MIA": {"factor": -1, "lat": 25.7781, "lon": -80.2196,  "orient": 30},
}


def statsapi_get(path, params=None):
    url = f"https://statsapi.mlb.com/api/v1/{path}"
    resp = requests.get(url, params=params or {}, timeout=30)
    resp.raise_for_status()
    return resp.json()


TEAM_ABBR = {
    108: "LAA", 109: "AZ", 110: "BAL", 111: "BOS", 112: "CHC", 113: "CIN",
    114: "CLE", 115: "COL", 116: "DET", 117: "HOU", 118: "KC", 119: "LAD",
    120: "WSH", 121: "NYM", 133: "ATH", 134: "PIT", 135: "SD", 136: "SEA",
    137: "SF", 138: "STL", 139: "TB", 140: "TEX", 141: "TOR", 142: "MIN",
    143: "PHI", 144: "ATL", 145: "CWS", 146: "MIA", 147: "NYY", 158: "MIL",
}


def team_abbr(team_obj):
    return TEAM_ABBR.get(team_obj.get("id"), team_obj.get("name", "UNK"))


def get_todays_games():
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
                "status": g.get("status", {}).get("abstractGameState", "Preview"),
                "game_time": g.get("gameDate"),
            })
    return games


def is_day_game(game_time_iso):
    """True if this game's local (US Eastern) start time is before 5:00 PM -
    the standard rough cutoff used across baseball analytics for "day game"
    vs "night game." Returns None if the game time is missing."""
    if not game_time_iso:
        return None
    try:
        dt_utc = datetime.datetime.fromisoformat(game_time_iso.replace("Z", "+00:00"))
        dt_et = dt_utc.astimezone(ZoneInfo("America/New_York"))
        return dt_et.hour < 17
    except Exception:
        return None


def get_lineup(game_pk, side):
    try:
        box = statsapi_get(f"game/{game_pk}/boxscore")
        team_box = box["teams"][side]
        players = team_box.get("players", {})
        lineup = {}
        for pid_key, pdata in players.items():
            order_code = pdata.get("battingOrder")
            if order_code and order_code.endswith("00"):
                pid = int(pid_key.replace("ID", ""))
                slot = int(order_code) // 100
                lineup[pid] = slot
        return lineup
    except Exception:
        return {}


def get_recent_lineup(team_id):
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
            starter_id = pitcher_ids[0]
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
    """Every start this pitcher has made in `season`, oldest first. Feeds
    BOTH the K board's recent-form (last-3-starts) number AND, new in v3,
    the HR/HRR/TB boards' recent hr9/whip blend via
    get_pitcher_recent_form() below - so this now also captures homeRuns,
    hits, and walks per start, not just strikeouts/innings."""
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
                "hr": int(stat.get("homeRuns", 0) or 0),
                "hits": int(stat.get("hits", 0) or 0),
                "bb": int(stat.get("baseOnBalls", 0) or 0),
            })
        starts.sort(key=lambda g: g["date"] or "")
        return starts
    except Exception:
        return []


def get_pitcher_recent_form(pitcher_id, season, season_hr9, season_whip):
    """This pitcher's last-3-starts hr9/whip, shrunk toward his OWN season
    rate based on how many real innings those 3 starts actually covered.
    Reuses get_pitcher_gamelog() - no new API call. NOTE: as of v3.3 this
    is NOT used in any scoring calculation (see the v3.3 note in main());
    left defined in case a clearly-labeled "recent trend" display stat is
    wanted later."""
    starts = get_pitcher_gamelog(pitcher_id, season)
    last3 = starts[-3:]
    ip_total = sum(g["ip"] or 0 for g in last3)
    if ip_total <= 0:
        return season_hr9, season_whip
    recent_hr9 = sum(g["hr"] for g in last3) * 9 / ip_total
    recent_whip = sum(g["hits"] + g["bb"] for g in last3) / ip_total
    pw = pitcher_sample_weight(ip_total)
    PITCHER_RECENT_DAMPEN = 0.6
    blended_hr9 = season_hr9 + (recent_hr9 - season_hr9) * pw * PITCHER_RECENT_DAMPEN
    blended_whip = season_whip + (recent_whip - season_whip) * pw * PITCHER_RECENT_DAMPEN
    return blended_hr9, blended_whip


def get_pitcher_season_stats(pitcher_id, season):
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


def get_day_night_split(batter_id):
    """This batter's HR rate (per-PA) in day games vs night games this
    season. HONESTY NOTE: 'd'/'n' are the best-guess sitCodes for this
    split - not confirmed against a live response. Returns Nones on
    failure so callers fall back cleanly."""
    try:
        day_data = statsapi_get(f"people/{batter_id}/stats", {
            "stats": "statSplits", "sitCodes": "d",
            "group": "hitting", "season": YEAR, "sportId": 1
        })
        night_data = statsapi_get(f"people/{batter_id}/stats", {
            "stats": "statSplits", "sitCodes": "n",
            "group": "hitting", "season": YEAR, "sportId": 1
        })
        day_splits = day_data.get("stats", [{}])[0].get("splits", [])
        night_splits = night_data.get("stats", [{}])[0].get("splits", [])
        day_stat = day_splits[0]["stat"] if day_splits else {}
        night_stat = night_splits[0]["stat"] if night_splits else {}
        day_pa = int(day_stat.get("plateAppearances", 0) or 0)
        night_pa = int(night_stat.get("plateAppearances", 0) or 0)
        day_hr = int(day_stat.get("homeRuns", 0) or 0)
        night_hr = int(night_stat.get("homeRuns", 0) or 0)
        day_rate = day_hr / day_pa if day_pa > 0 else None
        night_rate = night_hr / night_pa if night_pa > 0 else None
        return {"dayHrRate": day_rate, "dayPa": day_pa,
                "nightHrRate": night_rate, "nightPa": night_pa}
    except Exception:
        return {"dayHrRate": None, "dayPa": 0, "nightHrRate": None, "nightPa": 0}


def get_gamelog(batter_id, season):
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
                "tb": hits + doubles + 2 * triples + 3 * hr,
            })
        games.sort(key=lambda g: g["date"] or "")
        return games
    except Exception:
        return []


def day_of_week_split(games, target_weekday):
    """This batter's HR rate on the SAME day of the week as today, built
    entirely from the season gamelog already fetched - zero new API
    calls. target_weekday is 0=Monday..6=Sunday.

    HONEST CAVEAT: MLB's API has no real "day of week" stat split -
    unlike home/road, which is a genuine, commonly-tracked split,
    day-of-week isn't. A single weekday only comes up ~15-25 times across
    a whole season for any player, an unavoidably thin sample no matter
    how it's built. This is why day_of_week_adjustment() below uses the
    smallest bound and highest minimum-sample gate of any situational
    adjustment in this file - treat it as the least-trusted signal here,
    by design."""
    matching_games = [g for g in games
                       if g.get("date") and datetime.date.fromisoformat(g["date"]).weekday() == target_weekday]
    pa = sum(g["pa"] for g in matching_games)
    hr = sum(g["hr"] for g in matching_games)
    return (hr / pa if pa > 0 else None), pa


def home_road_split(games):
    """This batter's HR rate at home vs on the road, built entirely from
    the season gamelog already fetched - zero new API calls. Returns
    per-PA rates with the underlying PA counts, so compute_score can
    shrink small samples the same cautious way every other rate stat in
    this file already does."""
    home_games = [g for g in games if g.get("home")]
    road_games = [g for g in games if not g.get("home")]

    def pa_rate(gs):
        pa = sum(g["pa"] for g in gs)
        hr = sum(g["hr"] for g in gs)
        return (hr / pa if pa > 0 else None), pa

    home_rate, home_pa = pa_rate(home_games)
    road_rate, road_pa = pa_rate(road_games)
    return {"homeHrRate": home_rate, "homePa": home_pa,
            "roadHrRate": road_rate, "roadPa": road_pa}


def get_season_totals_hitting(batter_id, season):
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
    url = (f"https://baseballsavant.mlb.com/leaderboard/statcast"
           f"?type=batter&year={YEAR}&position=&team=&min=1&csv=true")
    resp = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
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


def fetch_batter_statcast_l15():
    """Last-15-day barrel%, EV, and hard-hit%, built by aggregating
    Baseball Savant's real event-level search export ('statcast_search').
    Returns one row per pitch league-wide for the last 15 days, filtered
    down to real batted-ball events (type == 'X'), aggregated per batter
    client-side:
      - EV: mean of launch_speed across that batter's batted balls
      - Barrel%: share of batted balls where launch_speed_angle == '6'
      - Hard-hit%: share of batted balls with launch_speed >= 95
    """
    end_date = datetime.date.today()
    start_date = end_date - datetime.timedelta(days=15)
    url = (
        "https://baseballsavant.mlb.com/statcast_search/csv"
        "?all=true&hfPT=&hfAB=&hfBBT=&hfPR=&hfZ=&hfStadium=&hfBBL=&hfNewZones="
        "&hfGT=R%7C&hfC=&hfSea=" + str(YEAR) + "%7C&hfSit="
        "&player_type=batter&hfOuts=&opponent=&pitcher_throws=&batter_stands="
        "&hfSA=&game_date_gt=" + start_date.isoformat()
        + "&game_date_lt=" + end_date.isoformat()
        + "&hfInfield=&team=&position=&hfOutfield=&hfRO=&home_road=&hfFlag="
        "&hfPull=&metric_1=&hfInn=&min_pitches=0&min_results=0"
        "&group_by=name&sort_col=pitches&player_event_sort=api_p_release_speed"
        "&sort_order=desc&min_pas=0&type=details"
    )
    try:
        resp = requests.get(url, timeout=90, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        text = resp.content.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        rows = list(reader)
        print(f"  Savant L15 event-level CSV columns: {reader.fieldnames}")
        print(f"  Savant L15 event-level CSV row count (all pitches): {len(rows)}")
        if rows:
            print(f"  sample row keys with values: "
                  f"{ {k: rows[0].get(k) for k in ['batter','type','launch_speed','launch_angle','launch_speed_angle','game_date']} }")
    except Exception as e:
        print(f"  WARNING: L15 Statcast event-level fetch failed ({e}) - power "
              f"score will fall back to season-only for everyone.")
        return {}

    id_columns = ["batter", "player_id", "batter_id"]
    id_col = next((c for c in id_columns if rows and c in rows[0]), None)
    type_columns = ["type"]
    type_col = next((c for c in type_columns if rows and c in rows[0]), None)
    ls_columns = ["launch_speed"]
    ls_col = next((c for c in ls_columns if rows and c in rows[0]), None)
    lsa_columns = ["launch_speed_angle"]
    lsa_col = next((c for c in lsa_columns if rows and c in rows[0]), None)
    print(f"  L15 using batter_id={id_col} type={type_col} "
          f"launch_speed={ls_col} launch_speed_angle={lsa_col}")

    if not (id_col and ls_col):
        print(f"  WARNING: required L15 columns not found - check the printed "
              f"CSV columns above. Falling back to season-only power for everyone.")
        return {}

    per_batter = {}
    for row in rows:
        if type_col and row.get(type_col) != "X":
            continue
        ls_raw = row.get(ls_col)
        if not ls_raw:
            continue
        pid_raw = row.get(id_col)
        if not pid_raw:
            continue
        try:
            pid = int(float(pid_raw))
            ls = float(ls_raw)
        except (TypeError, ValueError):
            continue
        lsa_raw = row.get(lsa_col) if lsa_col else None
        is_barrel = (lsa_raw == "6")
        d = per_batter.setdefault(pid, {"ev_sum": 0.0, "n": 0, "barrels": 0, "hardhit": 0})
        d["ev_sum"] += ls
        d["n"] += 1
        if is_barrel:
            d["barrels"] += 1
        if ls >= 95:
            d["hardhit"] += 1

    out = {}
    for pid, d in per_batter.items():
        if d["n"] <= 0:
            continue
        out[pid] = {
            "ev": round(d["ev_sum"] / d["n"], 1),
            "barrel": round(d["barrels"] / d["n"], 3),
            "hardhit": round(d["hardhit"] / d["n"], 3),
            "pa": d["n"],
        }
    print(f"  parsed L15 power data for {len(out)} batters from "
          f"{sum(d['n'] for d in per_batter.values())} batted-ball events")
    return out


def fetch_pitch_mix_data():
    """The batter side captures real batting AVG ('ba') and plate-appearance
    count ('pa') per pitch type, not just hard-hit% - this is what makes
    compute_avg_vs_mix() below possible: a real "AVG vs this pitcher's
    actual arsenal" stat, built WITH proper sample-size shrinkage."""
    batter_pitch_data = {}
    batter_pitch_avg = {}
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
        ba_columns = ["ba"]
        ba_col = next((c for c in ba_columns if rows and c in rows[0]), None)
        pa_columns = ["pa"]
        pa_col = next((c for c in pa_columns if rows and c in rows[0]), None)
        print(f"  ({kind}) using id={id_col} pitch_type={pitch_col} "
              f"usage={usage_col} metric={metric_col} ba={ba_col} pa={pa_col}")

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
                    out_dict[pid][pitch_type] = usage_raw / 100 if usage_raw > 1 else usage_raw
                except (ValueError, TypeError):
                    pass
            elif kind == "batter" and metric_col:
                try:
                    metric_raw = float(row[metric_col])
                    out_dict[pid][pitch_type] = metric_raw / 100 if metric_raw > 1 else metric_raw
                except (ValueError, TypeError):
                    pass
            if kind == "batter" and ba_col and pa_col:
                try:
                    ba_raw = float(row[ba_col])
                    pa_raw = int(float(row[pa_col]))
                    batter_pitch_avg.setdefault(pid, {})[pitch_type] = {"ba": ba_raw, "pa": pa_raw}
                except (ValueError, TypeError, KeyError):
                    pass

    print(f"  parsed pitch-mix data for {len(batter_pitch_data)} batters, "
          f"{len(pitcher_pitch_mix)} pitchers, {len(batter_pitch_avg)} with AVG-vs-pitch data")
    return batter_pitch_data, pitcher_pitch_mix, batter_pitch_avg


def compute_pitch_mix_match(batter_id, pitcher_id, batter_pitch_data, pitcher_pitch_mix):
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
    if weight_total < 0.3:
        return None
    return weighted_sum / weight_total


AVG_VS_MIX_SHRINK_K = 30


def compute_avg_vs_mix(batter_id, pitcher_id, batter_pitch_avg, pitcher_pitch_mix, season_avg):
    """A real "AVG vs this pitcher's actual arsenal" number - this
    batter's real batting average against each pitch type, weighted by
    how often THIS SPECIFIC pitcher actually throws each one, then shrunk
    toward the batter's own season AVG based on how much real matched-PA
    sample backs it up."""
    batter_data = batter_pitch_avg.get(batter_id)
    mix = pitcher_pitch_mix.get(pitcher_id)
    if not batter_data or not mix:
        return None, 0
    weighted_ba_sum = 0.0
    weighted_pa_sum = 0.0
    matched_pa_total = 0
    for pitch_type, usage in mix.items():
        entry = batter_data.get(pitch_type)
        if not entry:
            continue
        weighted_ba_sum += usage * entry["ba"]
        weighted_pa_sum += usage
        matched_pa_total += entry["pa"]
    if weighted_pa_sum < 0.3:
        return None, 0
    raw_avg_vs_mix = weighted_ba_sum / weighted_pa_sum

    anchor_avg = season_avg if season_avg is not None else LEAGUE_AVG_AVG
    sw = matched_pa_total / (matched_pa_total + AVG_VS_MIX_SHRINK_K)
    shrunk = raw_avg_vs_mix * sw + anchor_avg * (1 - sw)
    return round(shrunk, 3), matched_pa_total


def get_team_roster(team_id):
    """Active roster for a team - standard MLB Stats API endpoint, used
    only to find which pitchers are actually relievers for
    get_team_bullpen_stats() below."""
    try:
        data = statsapi_get(f"teams/{team_id}/roster", {"rosterType": "active"})
        return data.get("roster", [])
    except Exception:
        return []


LEAGUE_AVG_BULLPEN_ERA = 4.20
LEAGUE_AVG_BULLPEN_WHIP = 1.32
BULLPEN_MIN_IP = 20


def get_team_bullpen_stats(team_id, season):
    """This team's RELIEF corps quality (ERA/WHIP), separate from the
    starting pitcher. A pitcher counts as part of the "bullpen" if he has
    zero games started this season. Aggregates ERA/WHIP weighted by real
    relief innings pitched across the whole roster."""
    roster = get_team_roster(team_id)
    pitcher_ids = [p["person"]["id"] for p in roster
                   if p.get("position", {}).get("abbreviation") == "P"]
    total_ip = 0.0
    total_er = 0.0
    total_hits_walks = 0.0
    for pid in pitcher_ids:
        stat = get_pitcher_season_stats(pid, season)
        if not stat or stat.get("gamesStarted", 0) > 0:
            continue
        ip = stat.get("ip") or 0
        if ip <= 0:
            continue
        total_ip += ip
        total_er += stat["era"] * ip / 9
        total_hits_walks += stat["whip"] * ip
    if total_ip < BULLPEN_MIN_IP:
        return {"bullpenEra": None, "bullpenWhip": None, "bullpenIp": round(total_ip, 1)}
    return {
        "bullpenEra": round(total_er * 9 / total_ip, 2),
        "bullpenWhip": round(total_hits_walks / total_ip, 2),
        "bullpenIp": round(total_ip, 1),
    }


BULLPEN_MAX_ADJ = 0.05


def bullpen_adjustment(p):
    """Bounded multiplier from the OPPOSING team's bullpen quality - a bad
    bullpen (high ERA/WHIP) is good for the batter, applied the same
    dampened, capped way every other situational adjustment in this file
    is. Only applies with a real relief-innings sample (BULLPEN_MIN_IP)."""
    era = p.get("oppBullpenEra")
    whip = p.get("oppBullpenWhip")
    ip = p.get("oppBullpenIp") or 0
    if era is None or whip is None or ip < BULLPEN_MIN_IP:
        return 1.0
    era_ratio = era / LEAGUE_AVG_BULLPEN_ERA
    whip_ratio = whip / LEAGUE_AVG_BULLPEN_WHIP
    combined_ratio = (era_ratio + whip_ratio) / 2
    adj = clamp01((combined_ratio - 1) * 0.3 + 0.5) - 0.5
    adj = max(-BULLPEN_MAX_ADJ, min(BULLPEN_MAX_ADJ, adj))
    return 1 + adj


def wind_park_factor(speed, direction, park_orientation=None):
    """FIX #2 (v3.12): previously took `direction` and never used it, so
    wind could only ever help (0/1/2), never hurt, regardless of which
    way it was actually blowing. Now resolves the angle between the real
    wind direction and this park's approximate home-plate-to-center-field
    bearing (`park_orientation`, degrees from north) to get an in/out
    component: +1 = blowing straight out, -1 = blowing straight in, 0 =
    blowing crosswise. That component scales the same speed-tiered base
    magnitude as before, so a strong wind blowing IN now correctly
    produces a negative wind_score instead of being indistinguishable
    from calm air or a wind blowing out.

    Falls back to the old speed-only, never-negative behavior if we don't
    have a real direction or a real park orientation to compare it to
    (e.g. wind fetch failed, or - before FIX #1 - the park wasn't in
    PARKS at all)."""
    if speed is None or speed < 5:
        return 0
    if direction is None or park_orientation is None:
        base = 2 if speed >= 15 else (1 if speed >= 8 else 0)
        return base
    diff = abs((direction - park_orientation + 180) % 360 - 180)
    out_component = math.cos(math.radians(diff))  # +1 straight out, -1 straight in
    if speed >= 15:
        return round(2 * out_component, 2)
    if speed >= 8:
        return round(1 * out_component, 2)
    return 0


def clamp01(x):
    return max(0.0, min(1.0, x))


def barrel_confidence(barrel, ev):
    if barrel is None or ev is None:
        return 1.0
    barrel_intensity = clamp01((barrel - 0.08) / 0.08)
    ev_support = clamp01((ev - 85) / 10)
    discount = barrel_intensity * (1 - ev_support) * 0.5
    return 1 - discount


def avgmix_confidence_blend(avgmix):
    if avgmix is None:
        return 0.24
    if avgmix <= 0.10 or avgmix >= 0.40:
        return avgmix * 0.5 + 0.24 * 0.5
    return avgmix


def risp_confidence_blend(risp):
    if risp is None:
        return 0.255
    if risp <= 0.15 or risp >= 0.35:
        return risp * 0.5 + 0.255 * 0.5
    return risp


HRR_LINEUP_BONUS = {1: 7, 2: 8, 3: 8, 4: 8, 5: 7, 6: 5, 7: 3, 8: 2, 9: 1}


def hrr_lineup_bonus(order_pos):
    if order_pos is None:
        return 4
    return HRR_LINEUP_BONUS.get(order_pos, 1)


POWER_SHRINK_K = 40
LEAGUE_AVG_ISO = 0.150
LEAGUE_AVG_BARREL = 0.075
LEAGUE_AVG_EV = 88.5
LEAGUE_AVG_HARDHIT = 0.36
LEAGUE_AVG_HR_RATE_PA = 0.033  # league-average HR rate per PA (~3.3%), for shrinking small samples

POWER_L15_WEIGHT = 0.35
POWER_L15_MIN_PA = 15
L15_ISO_MIN_PA = 30

HOME_ROAD_MAX_ADJ = 0.06
HOME_ROAD_MIN_PA = 60
DAY_NIGHT_MAX_ADJ = 0.04
DAY_NIGHT_MIN_PA = 60
DOW_MAX_ADJ = 0.03
DOW_MIN_PA = 25
TREND_MAX_ADJ = 0.06
TREND_MIN_PA = 40  # need a genuinely real L30 sample before trusting a trend read at all


def power_sample_weight(pa):
    if pa is None or pa <= 0:
        return 0.3
    return pa / (pa + POWER_SHRINK_K)


PITCHER_SHRINK_K = 20


def pitcher_sample_weight(ip):
    if ip is None or ip <= 0:
        return 0.0
    return min(1.0, ip / (ip + PITCHER_SHRINK_K))


def home_road_adjustment(p):
    """Bounded multiplier from the home/road HR-rate split, applied based
    on whether TODAY's game is home or away for this batter. Compares to
    the batter's OWN overall season rate (not league average), and only
    applies once there's a real sample in the relevant split."""
    is_home = p.get("isHomeGame")
    if is_home is None:
        return 1.0
    relevant_rate = p.get("homeHrRate") if is_home else p.get("roadHrRate")
    relevant_pa = p.get("homePa" if is_home else "roadPa") or 0
    overall_rate = p.get("seasonHrRate")
    if relevant_rate is None or overall_rate is None or overall_rate <= 0:
        return 1.0
    if relevant_pa < HOME_ROAD_MIN_PA:
        return 1.0
    raw_ratio = relevant_rate / overall_rate
    adj = clamp01((raw_ratio - 1) * 0.3 + 0.5) - 0.5
    adj = max(-HOME_ROAD_MAX_ADJ, min(HOME_ROAD_MAX_ADJ, adj))
    return 1 + adj


def day_of_week_adjustment(p):
    """Same bounded-multiplier pattern as every other situational
    adjustment, but with the smallest cap and highest minimum sample of
    any of them, reflecting how much thinner this specific signal
    genuinely is (see day_of_week_split()'s honesty note above)."""
    dow_rate = p.get("dowHrRate")
    dow_pa = p.get("dowPa") or 0
    overall_rate = p.get("seasonHrRate")
    if dow_rate is None or overall_rate is None or overall_rate <= 0:
        return 1.0
    if dow_pa < DOW_MIN_PA:
        return 1.0
    raw_ratio = dow_rate / overall_rate
    adj = clamp01((raw_ratio - 1) * 0.3 + 0.5) - 0.5
    adj = max(-DOW_MAX_ADJ, min(DOW_MAX_ADJ, adj))
    return 1 + adj


def day_night_adjustment(p):
    """Same pattern as home_road_adjustment(), for day/night games."""
    is_day = p.get("isDayGame")
    if is_day is None:
        return 1.0
    relevant_rate = p.get("dayHrRate") if is_day else p.get("nightHrRate")
    relevant_pa = p.get("dayPa" if is_day else "nightPa") or 0
    overall_rate = p.get("seasonHrRate")
    if relevant_rate is None or overall_rate is None or overall_rate <= 0:
        return 1.0
    if relevant_pa < DAY_NIGHT_MIN_PA:
        return 1.0
    raw_ratio = relevant_rate / overall_rate
    adj = clamp01((raw_ratio - 1) * 0.3 + 0.5) - 0.5
    adj = max(-DAY_NIGHT_MAX_ADJ, min(DAY_NIGHT_MAX_ADJ, adj))
    return 1 + adj


def trend_adjustment(p):
    """v3.17: bounded multiplier comparing this player's L15 HR rate
    against his OWN longer L30 window - a stable, PA-larger baseline
    rather than a flat season aggregate. This is real within-season trend
    detection (heating up / cooling down) that a season-long rate can't
    see, without the noise risk of splitting an already-thin L15 sample
    in half to fit a slope (see the KNOWN LIMITATION note at the top of
    this file - this directly addresses that gap, the cheap and low-
    noise way rather than the aggressive one).

    This is treated as genuine player-specific signal (like `recent`),
    NOT situational noise - it does not participate in the stacking
    dampener the way home/road, day/night, bullpen, and day-of-week do
    (see situational_multiplier_boost() and the v3.15 notes on why those
    four are kept separate from real performance signal)."""
    l15_rate = p.get("l15hrRate")
    l30_rate = p.get("l30hrRate")
    l30_pa = p.get("l30Pa") or 0
    if l15_rate is None or l30_rate is None or l30_rate <= 0:
        return 1.0
    if l30_pa < TREND_MIN_PA:
        return 1.0
    raw_ratio = l15_rate / l30_rate
    adj = clamp01((raw_ratio - 1) * 0.3 + 0.5) - 0.5
    adj = max(-TREND_MAX_ADJ, min(TREND_MAX_ADJ, adj))
    return 1 + adj


def situational_multiplier_boost(p):
    """FIX #3 (v3.12) helper, shared by compute_score() and
    compute_hr_probability(). The old stacking dampener in both functions
    only ever looked at [power, pitcher_s, recent] - every OTHER
    multiplier (home/road, day/night, bullpen, day-of-week) could still
    all line up favorably on the same player completely unchecked, since
    each one alone is capped small. This sums how much those situational
    multipliers, TOGETHER, are boosting a player above neutral (1.0), so
    a pileup of several small favorable factors can trigger the same
    dampening a single dominant factor would."""
    mults = [
        home_road_adjustment(p), day_night_adjustment(p),
        bullpen_adjustment(p), day_of_week_adjustment(p),
    ]
    return sum(m - 1 for m in mults if m > 1)


def compute_hr_subfactors(p):
    """Extracted from compute_score() so both the composite score AND
    hrProb build from the SAME underlying reads - power, pitcher,
    platoon, recent, opportunity, park, wind. Returns a dict of 0-1
    normalized sub-scores plus a couple of raw values (conf, avg_vs_mix)
    needed for display.

    v3.13 RECALIBRATION (see the module docstring for the full why):
    `power` is now built from two conceptually distinct groups instead of
    a flat 5-way average of highly-correlated measures - quality_of_contact
    (barrel%/EV/hard-hit%, the Statcast process stats) and converted_power
    (ISO/season HR rate, the actual outcome stats) - blended 55/45. Every
    denominator is now anchored to a realistic MLB floor->rare-peak-season
    range instead of a round number that (for EV and hard-hit% especially)
    no real hitter has ever actually reached."""
    barrel = p["barrel"] or 0
    ev = p["ev"] or 85
    iso = p["iso"] or 0
    hardhit = p["hardhit"] or 0.30
    pw = power_sample_weight(p.get("pa"))
    barrel_season = barrel * pw + LEAGUE_AVG_BARREL * (1 - pw)
    ev_season = ev * pw + LEAGUE_AVG_EV * (1 - pw)
    iso_season = iso * pw + LEAGUE_AVG_ISO * (1 - pw)
    hardhit_season = hardhit * pw + LEAGUE_AVG_HARDHIT * (1 - pw)

    # v3.14: L15 weight now EARNS ITSELF with real sample size instead of
    # snapping straight to full POWER_L15_WEIGHT the instant a hitter
    # clears the old flat POWER_L15_MIN_PA/L15_ISO_MIN_PA cutoffs. 15-30
    # PA is a tiny window - two or three well-struck balls can swing
    # barrel%/hard-hit% wildly and make a short hot stretch look like an
    # elite established profile when it's really just noise. This uses
    # the same pa/(pa+K) shrinkage pattern already used everywhere else
    # in this file (power_sample_weight, pitcher_sample_weight) so L15
    # only approaches its full weight with a genuinely substantial
    # recent sample, and stays heavily discounted right after clearing
    # the minimum-PA gate.
    L15_SHRINK_K = 35

    def l15_weight(sample_pa, min_pa):
        if not sample_pa or sample_pa < min_pa:
            return 0.0
        return POWER_L15_WEIGHT * (sample_pa / (sample_pa + L15_SHRINK_K))

    l15_barrel = p.get("l15Barrel")
    l15_ev = p.get("l15Ev")
    l15_hardhit = p.get("l15Hardhit")
    l15_pa = p.get("l15PowerPa") or 0
    lw = l15_weight(l15_pa, POWER_L15_MIN_PA)
    if l15_barrel is not None and lw > 0:
        barrel_final = l15_barrel * lw + barrel_season * (1 - lw)
        ev_final = l15_ev * lw + ev_season * (1 - lw)
        hardhit_final = l15_hardhit * lw + hardhit_season * (1 - lw)
    else:
        barrel_final, ev_final, hardhit_final = barrel_season, ev_season, hardhit_season

    l15_iso = p.get("l15Iso")
    l15_iso_pa = p.get("l15IsoPa") or 0
    iso_lw = l15_weight(l15_iso_pa, L15_ISO_MIN_PA)
    if l15_iso is not None and iso_lw > 0:
        iso_final = l15_iso * iso_lw + iso_season * (1 - iso_lw)
    else:
        iso_final = iso_season

    phr9 = p["phr9"] if p["phr9"] is not None else 1.2
    whip = p["whip"] if p["whip"] is not None else 1.30
    avgmix = avgmix_confidence_blend(p["avgmix"])
    wind = p["wind"] or 0
    park = p["park"] or 0
    l15hr = p.get("l15hrCredit") if p.get("l15hrCredit") is not None else (p["l15hr"] if p["l15hr"] is not None else 0)
    l5hr = p.get("l5hrCredit") if p.get("l5hrCredit") is not None else (p["l5hr"] if p.get("l5hr") is not None else 0)
    lbonus = p["lbonus"] if p["lbonus"] is not None else 3
    crush = p["crush"] or 0
    split = p["split"] or 0

    conf = barrel_confidence(barrel_final, ev_final)
    barrel_adj = barrel_final * conf

    # --- RECALIBRATED POWER (v3.13, see docstring above) ---
    # Denominators anchored to realistic MLB floor->rare-peak-season
    # ranges instead of round numbers no real hitter ever reaches.
    barrel_n = clamp01((barrel_adj - 0.05) / 0.15)       # floor 5%, peak-season ceiling 20%
    ev_n = clamp01((ev_final - 87.0) / 7.0)               # floor 87mph, peak-season ceiling 94mph
    hardhit_n = clamp01((hardhit_final - 0.30) / 0.22)    # floor 30%, peak-season ceiling 52%
    # v3.18: barrel% now weighted HEAVIER than EV/hard-hit% within
    # quality_of_contact, instead of a flat 3-way average. Hard-hit% only
    # requires exit velo >=95mph - a line-drive or ground-ball hitter can
    # post a genuinely elite hard-hit% while barely ever actually
    # producing HR-shaped contact, because hard-hit% has zero regard for
    # launch angle. Barrel% requires BOTH real exit velo AND the launch
    # angle that actually turns hard contact into a home run - it's the
    # single most HR-specific Statcast metric here, and shouldn't be
    # diluted 1-for-1 by a much weaker, angle-blind signal. A hitter with
    # a mediocre barrel% but inflated hard-hit% (hits the ball hard, just
    # not in the air) no longer gets nearly as much power credit for it.
    quality_of_contact = barrel_n * 0.50 + ev_n * 0.25 + hardhit_n * 0.25

    iso_n = clamp01((iso_final - 0.08) / 0.27)            # floor .080, peak-season ceiling .350

    # v3.15: season HR rate now gets the SAME sample-size shrinkage every
    # other power component already gets (barrel/EV/hard-hit/ISO all
    # blend toward league average via `pw` above) - this was the one
    # component still using a raw, unshrunk rate. Without shrinkage, a
    # guy with 3 HR in 25 PA (12% rate, pure small-sample noise) would
    # max this component out completely - scoring HIGHER than a real
    # 25-HR, 500-PA season (5.5% rate), which is backwards. `pw` is
    # already computed above from the same PA count used for the other
    # components, so this is just applying the existing pattern here too.
    HR_RATE_FLOOR = 0.01
    HR_RATE_CEIL = 0.07                                    # ~peak Judge/Ohtani-level season rate
    season_hr_rate_raw = p.get("seasonHrRate")
    season_hr_rate_shrunk = (season_hr_rate_raw * pw + LEAGUE_AVG_HR_RATE_PA * (1 - pw)
                              if season_hr_rate_raw is not None else LEAGUE_AVG_HR_RATE_PA)
    hr_rate_n = clamp01((season_hr_rate_shrunk - HR_RATE_FLOOR) / (HR_RATE_CEIL - HR_RATE_FLOOR))
    converted_power = (iso_n + hr_rate_n) / 2

    power = quality_of_contact * 0.55 + converted_power * 0.45
    # --- end recalibrated power ---

    phr9_s = clamp01((phr9 - 0.3) / 2.2)
    whip_s = clamp01((whip - 0.9) / 1.15)
    pitcher_s = (phr9_s + whip_s) / 2

    # FIX #2 (v3.12): wind_s now reflects wind_score's real range (can be
    # negative for in-blowing wind), same clamp01/normalize shape as
    # before - the fix lives in wind_park_factor()/wind_score, not here.
    wind_s = clamp01((wind + 2) / 4)
    park_s = clamp01((park + 2) / 4)

    # v3.16: recent HR bucket now REGRESSED toward this player's own
    # power level, weighted by real recent PA sample size - separately
    # for the L15 and L5 windows, since a burst that's really just his
    # last 2 games can inflate the L5 rate hugely even though it's PA-
    # thin. Previously this used raw HR counts against a fixed cap with
    # zero regard for how many actual plate appearances backed them up -
    # exactly the small-sample problem already fixed everywhere else in
    # this file (barrel%/EV/hard-hit%/ISO/season HR rate), just not here
    # yet. A modest-power hitter's 2-game hot streak now gets pulled back
    # toward what he actually is instead of standing on its own.
    RECENT_SHRINK_K = 20
    l15_recent_pa = p.get("l15IsoPa") or 0
    l5_recent_pa = p.get("l5Pa") or 0
    l15_trust = l15_recent_pa / (l15_recent_pa + RECENT_SHRINK_K) if l15_recent_pa > 0 else 0.0
    l5_trust = l5_recent_pa / (l5_recent_pa + RECENT_SHRINK_K) if l5_recent_pa > 0 else 0.0
    recent_l15 = clamp01(l15hr / 9) * l15_trust + power * (1 - l15_trust)
    recent_l5 = clamp01(l5hr / 3) * l5_trust + power * (1 - l5_trust)
    recent = recent_l15 * 0.6 + recent_l5 * 0.4

    handedness_platoon = (crush + split) / 2
    pitch_mix_raw = p.get("pitchMixMatch")
    if pitch_mix_raw is not None:
        pitch_mix_norm = clamp01(pitch_mix_raw / 0.45)
        pitch_mix_platoon = pitch_mix_norm * 0.7 + handedness_platoon * 0.3
    else:
        pitch_mix_platoon = handedness_platoon
    avgmix_s = clamp01(avgmix / 0.5)
    avg_vs_mix = p.get("avgVsMix")
    if avg_vs_mix is not None:
        avg_vs_mix_s = clamp01(avg_vs_mix / 0.320)
        platoon = pitch_mix_platoon * 0.5 + avgmix_s * 0.2 + avg_vs_mix_s * 0.3
    else:
        avg_vs_mix_s = None
        platoon = pitch_mix_platoon * 0.7 + avgmix_s * 0.3

    opportunity = clamp01((lbonus - 1) / 5)

    return {
        "power": power, "pitcher_s": pitcher_s, "platoon": platoon,
        "recent": recent, "opportunity": opportunity,
        "park_s": park_s, "wind_s": wind_s,
        "conf": conf, "avg_vs_mix": avg_vs_mix, "avg_vs_mix_s": avg_vs_mix_s,
    }


def compute_score(p):
    """HR Board composite score - Power 35% / Pitcher 15% / Platoon 10% /
    Recent 15% / Opportunity 10% / Park 8% / Wind 7%, plus the stacking
    dampener and the bounded home/road, day/night, bullpen multipliers.
    Built from the shared compute_hr_subfactors() so it can never drift
    from hrProb again.

    FIX #3 (v3.12): the stacking dampener previously only watched
    [power, pitcher_s, recent]. It now also counts a large combined
    situational boost (home/road + day/night + bullpen + day-of-week all
    lining up favorably) as equivalent to one more "maxed" bucket, so a
    pileup of small favorable factors can't sail through unchecked just
    because none of them individually cleared STACK_THRESHOLD."""
    sf = compute_hr_subfactors(p)
    power, pitcher_s, platoon, recent, opportunity, park_s, wind_s = (
        sf["power"], sf["pitcher_s"], sf["platoon"], sf["recent"],
        sf["opportunity"], sf["park_s"], sf["wind_s"])

    score = (power * 35 + pitcher_s * 15 + platoon * 10 + recent * 15
             + opportunity * 10 + park_s * 8 + wind_s * 7)

    # v3.15: dampener REWORKED. Previously any 2+ of [power, pitcher_s,
    # recent] individually clearing STACK_THRESHOLD triggered a penalty -
    # but these are genuine PRIMARY performance signals, not small
    # independent nudges. A hitter who is legitimately elite in power AND
    # legitimately hot right now AND legitimately facing a bad pitcher is
    # exactly the profile that SHOULD score highest, not get penalized
    # for it - especially now that the power subfactor (v3.13) can
    # actually reach real elite territory, this was punishing genuinely
    # great matchups as if they were suspicious stacking.
    #
    # The dampener now targets what it was actually meant to catch: many
    # small, semi-independent SITUATIONAL factors (home/road, day/night,
    # bullpen, day-of-week) coincidentally lining up favorably at once -
    # that kind of alignment is much more plausibly noise than a hitter
    # simply being great, hot, and well-matched all at the same time.
    SITUATIONAL_BOOST_TRIGGER = 0.10
    STACK_PENALTY_MAX = 6.0
    sit_boost = situational_multiplier_boost(p)
    if sit_boost >= SITUATIONAL_BOOST_TRIGGER:
        stack_penalty = min(STACK_PENALTY_MAX, sit_boost * 20)
        score -= stack_penalty

    score *= home_road_adjustment(p)
    score *= day_night_adjustment(p)
    score *= bullpen_adjustment(p)
    score *= day_of_week_adjustment(p)
    score *= trend_adjustment(p)
    score = max(0.0, min(100.0, score))

    return {
        "score": round(score, 1),
        "conf": sf["conf"],
        "powerPct": round(power * 100, 1),
        "pitcherPct": round(pitcher_s * 100, 1),
        "platoonPct": round(platoon * 100, 1),
        "recentPct": round(recent * 100, 1),
        "opportunityPct": round(opportunity * 100, 1),
        "parkPct": round(park_s * 100, 1),
        "windPct": round(wind_s * 100, 1),
        "avgVsMixPct": round(sf["avg_vs_mix_s"] * 100, 1) if sf["avg_vs_mix_s"] is not None else None,
    }


def compute_hrr_score(p):
    """HRR Board scoring - OnBase 35 / Matchup 30 / Recent 15 / RISP 10 /
    Opportunity 10. Implicitly benefits from the pitcher-recent-form WHIP
    blend since main() overwrites p["whip"] before this runs."""
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

    recent = clamp01(l15hrr / 10)
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


def compute_tb_score(p):
    """TB Board scoring - same implicit-benefit note as HRR above."""
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

    recent = clamp01(l15tb / 10)

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


LEAGUE_AVG_PITCHER_HR9 = 1.20
LEAGUE_AVG_PITCHER_WHIP = 1.30
LEAGUE_AVG_HR_RATE = 0.12
LEAGUE_AVG_HRR_CLEAR_RATE = 0.52
LEAGUE_AVG_TB_CLEAR_RATE = 0.43


def compute_hr_probability(p):
    """Built from the SAME compute_hr_subfactors() the composite score
    uses, so power automatically includes pure-L15 barrel/EV/hard-hit%
    and real L15 ISO, and platoon/opportunity/park/wind factor in here
    too.

    v3.13 CHANGES (see module docstring for full why):
      - POWER_QUALITY_MULTIPLIER 1.3 -> 1.7 (power_quality can now
        legitimately approach 1.0 for real elite profiles, so the rate
        ceiling it implies needed to widen to match).
      - RECENT_TRUST 0.4 -> 0.5 (actual demonstrated recent production
        trusted evenly with the power-quality-implied rate, not
        discounted below it).
      - Old exponential soft-cap (0.22 -> 0.30) REMOVED, replaced with a
        single hard sanity ceiling (0.45) - the stacking dampener a few
        lines below already exists to keep unrealistic pileups in check;
        this avoids doing that job twice."""
    l15hr = p.get("l15hr")
    if l15hr is None:
        return None
    l5hr = p.get("l5hr")
    l15hr_for_rate = p.get("l15hrCredit") if p.get("l15hrCredit") is not None else l15hr
    l5hr_for_rate = p.get("l5hrCredit") if p.get("l5hrCredit") is not None else l5hr

    sf = compute_hr_subfactors(p)
    power_quality = sf["power"]
    POWER_QUALITY_MULTIPLIER = 1.7  # was 1.3
    season_implied_rate = LEAGUE_AVG_HR_RATE * (0.3 + power_quality * POWER_QUALITY_MULTIPLIER)

    # v3.16: recent HR rate now REGRESSED toward this player's own
    # established (power-quality-implied) rate, weighted by real recent
    # PA sample size - separately for L15 and L5, since e.g. 2 HR in a
    # player's last 2 games inflates the L5 rate hugely even though it's
    # PA-thin. Previously recent_rate was blended at a flat 50% trust
    # regardless of whether "recent" meant a real 15-day sample or a tiny
    # 2-game hot streak - this is exactly what let a short burst from a
    # modest-power hitter climb the board on raw recency alone instead of
    # being pulled back toward what he actually is.
    RECENT_SHRINK_K = 20
    l15_pa = p.get("l15IsoPa") or 0
    l5_pa = p.get("l5Pa") or 0
    l15_rate_raw = l15hr_for_rate / 15
    l5_rate_raw = (l5hr_for_rate / 5) if l5hr_for_rate is not None else l15_rate_raw
    l15_trust = l15_pa / (l15_pa + RECENT_SHRINK_K) if l15_pa > 0 else 0.0
    l5_trust = l5_pa / (l5_pa + RECENT_SHRINK_K) if l5_pa > 0 else 0.0
    l15_rate_regressed = l15_rate_raw * l15_trust + season_implied_rate * (1 - l15_trust)
    l5_rate_regressed = l5_rate_raw * l5_trust + season_implied_rate * (1 - l5_trust)
    recent_rate = l15_rate_regressed * 0.6 + l5_rate_regressed * 0.4

    RECENT_TRUST = 0.5
    blended_rate = recent_rate * RECENT_TRUST + season_implied_rate * (1 - RECENT_TRUST)

    pw = power_sample_weight(p.get("pa"))
    base_rate = blended_rate * pw + season_implied_rate * (1 - pw)

    phr9 = p.get("phr9") if p.get("phr9") is not None else LEAGUE_AVG_PITCHER_HR9
    pw_pitcher = pitcher_sample_weight(p.get("pip"))
    effective_phr9 = phr9 * pw_pitcher + LEAGUE_AVG_PITCHER_HR9 * (1 - pw_pitcher)
    PITCHER_MATCHUP_SENSITIVITY = 0.6
    matchup_mult = 1 + ((effective_phr9 / LEAGUE_AVG_PITCHER_HR9) - 1) * PITCHER_MATCHUP_SENSITIVITY
    mean = max(0.01, base_rate * matchup_mult)

    # v3.14: situational nudges (platoon/opportunity/park/wind) and the
    # home-road/day-night/bullpen/day-of-week multipliers are now GATED
    # by how much real power is actually behind the pick. Previously
    # these applied in full to every player regardless of power_quality -
    # so an average-power hitter with a great platoon matchup, a hot
    # lineup spot, and helpful wind could stack several multiplicative
    # tailwinds and land in PRIME territory on situational alignment
    # alone, without the raw ability to actually drive a ball out. A
    # tailwind only helps a ball that was hit hard enough to be helped.
    # A real elite bat still gets these nudges at full strength; a
    # well-below-average bat gets them at a fraction of that
    # (POWER_GATE_MIN_STRENGTH), scaling linearly in between. This is
    # the direct fix for "too many PRIME picks that were really just
    # decent-power guys with a good matchup on paper."
    POWER_GATE_FLOOR = 0.25
    POWER_GATE_CEIL = 0.65
    POWER_GATE_MIN_STRENGTH = 0.35
    power_gate = clamp01((power_quality - POWER_GATE_FLOOR) / (POWER_GATE_CEIL - POWER_GATE_FLOOR))
    situational_strength = POWER_GATE_MIN_STRENGTH + (1 - POWER_GATE_MIN_STRENGTH) * power_gate

    NUDGE_STRENGTH = 0.14
    for factor_val in (sf["platoon"], sf["opportunity"], sf["park_s"], sf["wind_s"]):
        mean *= 1 + (factor_val - 0.5) * NUDGE_STRENGTH * situational_strength

    for adj in (home_road_adjustment(p), day_night_adjustment(p),
                bullpen_adjustment(p), day_of_week_adjustment(p)):
        mean *= 1 + (adj - 1) * situational_strength

    # v3.17: trend applied directly, NOT power-gated like the four
    # situational adjustments above - this is about the player's OWN
    # recent trajectory, same category as `recent` and `power`, not an
    # external tailwind unrelated to his own performance.
    mean *= trend_adjustment(p)

    # FIX #3 (v3.12): same situational-pileup guard added to compute_score()
    # is now applied here too - previously this stacking dampener only
    # checked [power, pitcher_s, recent], same gap as compute_score() had.
    # v3.15: same dampener rework as compute_score() - see the detailed
    # comment there. No longer penalizes power/pitcher_s/recent for being
    # simultaneously strong (those are real signal, not stacking noise);
    # only fires on genuinely situational alignment (home/road, day/night,
    # bullpen, day-of-week).
    SITUATIONAL_BOOST_TRIGGER = 0.10
    STACK_REDUCTION_MAX = 0.20
    sit_boost = situational_multiplier_boost(p)
    if sit_boost >= SITUATIONAL_BOOST_TRIGGER:
        reduction = min(STACK_REDUCTION_MAX, sit_boost * 0.7)
        mean *= (1 - reduction)

    mean = max(0.01, mean)

    raw_prob = poisson_over_prob(mean, 0.5)
    if raw_prob is None:
        return None

    # v3.13: single hard sanity ceiling instead of the old exponential
    # soft-cap. Only clips truly extreme outputs, doesn't compress the
    # normal PRIME/STRONG range the way the old 0.22->0.30 squeeze did.
    HR_PROB_HARD_CAP = 0.45
    return min(raw_prob, HR_PROB_HARD_CAP)


LEAGUE_AVG_AVG = 0.245
LEAGUE_AVG_OBP = 0.315


def compute_hrr_probability(p):
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

    RECENT_TRUST = 0.4
    blended_rate = recent_rate * RECENT_TRUST + season_implied_rate * (1 - RECENT_TRUST)

    pw = power_sample_weight(p.get("pa"))
    base_rate = blended_rate * pw + season_implied_rate * (1 - pw)

    whip = p.get("whip") if p.get("whip") is not None else LEAGUE_AVG_PITCHER_WHIP
    matchup_mult = 1 + ((whip / LEAGUE_AVG_PITCHER_WHIP) - 1) * 0.5
    return clamp01(base_rate * matchup_mult)


def compute_tb_probability(p):
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

    combined_multiplier = contact_quality * 0.45 + power_multiplier * 0.55
    season_implied_rate = clamp01(LEAGUE_AVG_TB_CLEAR_RATE * combined_multiplier)

    RECENT_TRUST = 0.4
    blended_rate = recent_rate * RECENT_TRUST + season_implied_rate * (1 - RECENT_TRUST)

    pw = power_sample_weight(p.get("pa"))
    base_rate = blended_rate * pw + season_implied_rate * (1 - pw)

    whip = p.get("whip") if p.get("whip") is not None else LEAGUE_AVG_PITCHER_WHIP
    matchup_mult = 1 + ((whip / LEAGUE_AVG_PITCHER_WHIP) - 1) * 0.5
    return clamp01(base_rate * matchup_mult)


LEAGUE_AVG_TEAM_K_RATE = 0.220


def diminishing_hr_credit(games):
    return sum(min(g["hr"], 1) + max(g["hr"] - 1, 0) * 0.4 for g in games)


def poisson_over_prob(mean, line):
    if mean is None or mean <= 0:
        return None
    threshold = math.floor(line) + 1
    cdf = math.exp(-mean)
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

    matchup_mult = 1 + ((opp_k_rate / LEAGUE_AVG_TEAM_K_RATE) - 1) * 0.4
    proj_k = ip_per_start * (k9 / 9) * matchup_mult

    expected_l3 = proj_k * 3
    if expected_l3 > 0:
        nudge = clamp01((l3k - expected_l3) / (expected_l3 * 1.5) + 0.5) - 0.5
        proj_k *= (1 + nudge * 0.3)

    proj_k = max(0.5, round(proj_k, 2))

    model_line = math.floor(proj_k * 2) / 2
    if model_line < 0.5:
        model_line = 0.5

    over_prob = poisson_over_prob(proj_k, model_line)
    score = round((over_prob if over_prob is not None else 0.5) * 100, 1)

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

    print("Fetching last-15-day Statcast batter data (barrel%, EV, hard-hit%)...")
    statcast_l15 = fetch_batter_statcast_l15()
    print(f"  parsed {len(statcast_l15)} batters with L15 Statcast data")

    print("Fetching pitch-mix data (batter vs pitch type, pitcher usage)...")
    batter_pitch_data, pitcher_pitch_mix, batter_pitch_avg = fetch_pitch_mix_data()

    print("Fetching bullpen quality for today's teams...")
    all_team_ids = set()
    for g in games:
        all_team_ids.add(g["home_team_id"])
        all_team_ids.add(g["away_team_id"])

    def fetch_one_bullpen(tid):
        return tid, get_team_bullpen_stats(tid, YEAR)

    bullpen_cache = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        for tid, stats in executor.map(fetch_one_bullpen, all_team_ids):
            bullpen_cache[tid] = stats
    trusted_bullpens = sum(1 for s in bullpen_cache.values() if s.get("bullpenEra") is not None)
    print(f"  got trusted bullpen reads for {trusted_bullpens} of {len(all_team_ids)} teams "
          f"(rest fell below {BULLPEN_MIN_IP} IP relief sample and will show no adjustment)")

    rows = []
    sides_with_pitcher = 0
    sides_confirmed_lineup = 0
    sides_projected_lineup = 0
    sides_missing_pitcher = 0
    sides_no_lineup_at_all = 0
    sides_projected_pitcher = 0
    sides_no_pitcher_at_all = 0

    for g in games:
        is_day = is_day_game(g.get("game_time"))
        for side, opp_side in [("home", "away"), ("away", "home")]:
            team = g[f"{side}_team"]
            team_id = g[f"{side}_team_id"]
            opp_pitcher = g[f"{opp_side}_pitcher"]
            opp_team_id = g[f"{opp_side}_team_id"]
            pitcher_confirmed = True
            if not opp_pitcher:
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

            effective_hr9 = pitcher_stat.get("hr9", 1.20)
            effective_whip = pitcher_stat.get("whip", 1.30)

            # FIX #1 (v3.12): every home team now has a real PARKS entry
            # (all 30 present), so this .get() fallback should no longer
            # actually trigger for a real MLB team - left in place only as
            # a genuine defensive fallback (e.g. an unrecognized abbrev).
            park = PARKS.get(g["home_team"], {"factor": 0, "lat": None, "lon": None, "orient": None})
            wind_speed, wind_dir = (None, None)
            if park.get("lat") is not None:
                wind_speed, wind_dir = get_wind(park["lat"], park["lon"])
            # FIX #2 (v3.12): pass wind direction + park orientation through
            # so wind can now suppress, not just boost.
            wind_score = wind_park_factor(wind_speed, wind_dir, park.get("orient"))

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
                name = bstats.get("name") or ""
                sc = statcast.get(batter_id, {})
                sc_l15 = statcast_l15.get(batter_id, {})
                avg_vs_mix_val, avg_vs_mix_pa = compute_avg_vs_mix(
                    batter_id, pitcher_id, batter_pitch_avg, pitcher_pitch_mix, bstats.get("avg"))

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
                    "isDayGame": is_day,
                    "isHomeGame": side == "home",
                    "playerId": batter_id,
                    "pitchMixMatch": compute_pitch_mix_match(
                        batter_id, pitcher_id, batter_pitch_data, pitcher_pitch_mix),
                    "avgVsMix": avg_vs_mix_val,
                    "avgVsMixPa": avg_vs_mix_pa,
                    "oppBullpenEra": bullpen_cache.get(opp_team_id, {}).get("bullpenEra"),
                    "oppBullpenWhip": bullpen_cache.get(opp_team_id, {}).get("bullpenWhip"),
                    "oppBullpenIp": bullpen_cache.get(opp_team_id, {}).get("bullpenIp"),
                    "barrel": sc.get("barrel"),
                    "ev": sc.get("ev"),
                    "hardhit": sc.get("hardhit"),
                    "l15Barrel": sc_l15.get("barrel"),
                    "l15Ev": sc_l15.get("ev"),
                    "l15Hardhit": sc_l15.get("hardhit"),
                    "l15PowerPa": sc_l15.get("pa"),
                    "iso": bstats.get("iso"),
                    "pa": bstats.get("pa"),
                    "slg": bstats.get("slg"),
                    "avg": bstats.get("avg"),
                    "obp": bstats.get("obp"),
                    "phr9": effective_hr9,
                    "whip": effective_whip,
                    "phr9Season": pitcher_stat.get("hr9"),
                    "whipSeason": pitcher_stat.get("whip"),
                    "pip": pitcher_stat.get("ip"),
                    "avgmix": None,
                    "wind": wind_score,
                    "park": park["factor"],
                    "l15hr": None,
                    "l5hr": None,
                    "l15hrr": None,
                    "l15tb": None,
                    "risp": None,
                    "lbonus": max(1, 9 - order_pos),
                    "hrrLbonus": hrr_lineup_bonus(order_pos),
                    "crush": 0,
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
        l5hr = sum(g["hr"] for g in games_this_year[-5:]) if games_this_year else None
        # v3.17: L30 window - a stable, PA-larger baseline to compare L15
        # against for real trend detection (heating up / cooling down),
        # without splitting the already-thin L15 sample in half. Rates
        # use the ACTUAL window length (not a fixed 15/30) so early-
        # season players with fewer games played don't get an
        # artificially deflated baseline.
        l30_games = games_this_year[-30:] if games_this_year else []
        l30hr = sum(g["hr"] for g in l30_games)
        l30_pa = sum(g["pa"] for g in l30_games)
        l15_games_for_trend = games_this_year[-15:] if games_this_year else []
        l15hr_rate = (l15hr / len(l15_games_for_trend)) if l15_games_for_trend else None
        l30hr_rate = (l30hr / len(l30_games)) if l30_games else None
        l15_games_for_iso = games_this_year[-15:] if games_this_year else []
        l15_iso_pa = sum(g["pa"] for g in l15_games_for_iso)
        l15_iso_val = (round((sum(g["tb"] for g in l15_games_for_iso) - sum(g["hits"] for g in l15_games_for_iso)) / l15_iso_pa, 3)
                       if l15_iso_pa > 0 else None)
        # v3.16: real PA count over the last 5 games specifically - lets
        # the recent-HR regression in compute_hr_subfactors()/
        # compute_hr_probability() tell a genuinely sustained short hot
        # streak apart from a 2-game blip that just happens to land in a
        # 5-game window.
        l5_games_for_pa = games_this_year[-5:] if games_this_year else []
        l5_pa = sum(g["pa"] for g in l5_games_for_pa)
        l15hr_credit = (diminishing_hr_credit(games_this_year[-15:])
                         if games_this_year else None)
        l5hr_credit = (diminishing_hr_credit(games_this_year[-5:])
                        if games_this_year else None)
        l15hrr = (sum(1 for g in games_this_year[-15:] if g["hrr"] >= 2)
                  if games_this_year else None)
        l15tb = (sum(1 for g in games_this_year[-15:] if g["tb"] >= 2)
                 if games_this_year else None)
        l15hits = (sum(g["hits"] for g in games_this_year[-15:])
                   if games_this_year else None)
        risp = get_risp_avg(batter_id)

        hr_road = home_road_split(games_this_year) if games_this_year else {}
        dow_hr_rate, dow_pa = day_of_week_split(games_this_year, TODAY_WEEKDAY) if games_this_year else (None, 0)
        day_night = get_day_night_split(batter_id)
        season_pa = sum(g["pa"] for g in games_this_year) if games_this_year else 0
        season_hr = sum(g["hr"] for g in games_this_year) if games_this_year else 0
        season_hr_rate = (season_hr / season_pa) if season_pa > 0 else None

        player_row["avgmix"] = platoon_avg
        player_row["l15hr"] = l15hr
        player_row["l15Iso"] = l15_iso_val
        player_row["l15IsoPa"] = l15_iso_pa
        player_row["l5Pa"] = l5_pa
        player_row["l5hr"] = l5hr
        player_row["l15hrCredit"] = l15hr_credit
        player_row["l5hrCredit"] = l5hr_credit
        player_row["l30hr"] = l30hr
        player_row["l30Pa"] = l30_pa
        player_row["l15hrRate"] = l15hr_rate
        player_row["l30hrRate"] = l30hr_rate
        player_row["l15hrr"] = l15hrr
        player_row["l15tb"] = l15tb
        player_row["l15hits"] = l15hits
        player_row["risp"] = risp
        player_row["crush"] = 1 if (platoon_avg or 0) >= 0.280 else 0
        player_row["split"] = 1 if (platoon_avg or 0) >= 0.260 else 0
        player_row["homeHrRate"] = hr_road.get("homeHrRate")
        player_row["homePa"] = hr_road.get("homePa")
        player_row["roadHrRate"] = hr_road.get("roadHrRate")
        player_row["roadPa"] = hr_road.get("roadPa")
        player_row["dayHrRate"] = day_night.get("dayHrRate")
        player_row["dayPa"] = day_night.get("dayPa")
        player_row["nightHrRate"] = day_night.get("nightHrRate")
        player_row["nightPa"] = day_night.get("nightPa")
        player_row["seasonHrRate"] = season_hr_rate
        player_row["dowHrRate"] = dow_hr_rate
        player_row["dowPa"] = dow_pa
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
        hr_prob = compute_hr_probability(player_row)
        if hr_prob is not None:
            player_row["hrProb"] = round(hr_prob * 100, 1)
        hrr_prob = compute_hrr_probability(player_row)
        if hrr_prob is not None:
            player_row["hrrProb"] = round(hrr_prob * 100, 1)
        tb_prob = compute_tb_probability(player_row)
        if tb_prob is not None:
            player_row["tbProb"] = round(tb_prob * 100, 1)

    print("Fetching team strikeout rates (opposing-lineup matchup signal)...")
    team_k_rate = get_team_k_rate()

    pitcher_rows = {}
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
                "l3k": None,
                "l5k": None,
            }

    print(f"Fetching per-pitcher recent form ({len(pitcher_rows)} starters, concurrently)...")

    def fetch_pitcher(item):
        pitcher_id, row = item
        own_season = reliable_pitcher_stats.get(pitcher_id) or get_pitcher_season_stats(pitcher_id, YEAR)
        if own_season:
            row.update(own_season)
        starts = get_pitcher_gamelog(pitcher_id, YEAR)
        row["l3k"] = sum(g["k"] for g in starts[-3:]) if starts else None
        row["l5k"] = sum(g["k"] for g in starts[-5:]) if starts else None

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
        row["starts"] = starts[-10:]
        return row

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        pitchers = list(executor.map(fetch_pitcher, pitcher_rows.items()))

    print(f"  {len(pitchers)} probable starters found")
    players.extend(pitchers)

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

    players.sort(key=lambda p: -p.get("score", 0))

    with open("players.json", "w") as f:
        json.dump(players, f, indent=2, allow_nan=False)

    print(f"Wrote players.json with {len(players)} players.")

    write_daily_snapshot(players)


def write_daily_snapshot(players):
    """Daily snapshot for the historical accuracy tracker - saves the raw
    inputs compute_score() actually used, not just the final score, so a
    real future backtest is possible."""
    os.makedirs("history", exist_ok=True)
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
                "barrel": p.get("barrel"), "ev": p.get("ev"), "hardhit": p.get("hardhit"),
                "l15Barrel": p.get("l15Barrel"), "l15Ev": p.get("l15Ev"),
                "l15Hardhit": p.get("l15Hardhit"), "l15PowerPa": p.get("l15PowerPa"),
                "iso": p.get("iso"), "pa": p.get("pa"), "slg": p.get("slg"),
                "avg": p.get("avg"), "obp": p.get("obp"),
                "phr9": p.get("phr9"), "whip": p.get("whip"),
                "phr9Season": p.get("phr9Season"), "whipSeason": p.get("whipSeason"),
                "avgmix": p.get("avgmix"), "wind": p.get("wind"), "park": p.get("park"),
                "l15hr": p.get("l15hr"), "l5hr": p.get("l5hr"),
                "l15hrr": p.get("l15hrr"), "l15tb": p.get("l15tb"),
                "risp": p.get("risp"), "lbonus": p.get("lbonus"),
                "hrrLbonus": p.get("hrrLbonus"), "crush": p.get("crush"), "split": p.get("split"),
                "pitchMixMatch": p.get("pitchMixMatch"),
                "isDayGame": p.get("isDayGame"), "isHomeGame": p.get("isHomeGame"),
                "homeHrRate": p.get("homeHrRate"), "homePa": p.get("homePa"),
                "roadHrRate": p.get("roadHrRate"), "roadPa": p.get("roadPa"),
                "dayHrRate": p.get("dayHrRate"), "dayPa": p.get("dayPa"),
                "nightHrRate": p.get("nightHrRate"), "nightPa": p.get("nightPa"),
                "seasonHrRate": p.get("seasonHrRate"),
                "dowHrRate": p.get("dowHrRate"), "dowPa": p.get("dowPa"),
                "avgVsMix": p.get("avgVsMix"), "avgVsMixPa": p.get("avgVsMixPa"),
                "l15Iso": p.get("l15Iso"), "l15IsoPa": p.get("l15IsoPa"), "l5Pa": p.get("l5Pa"),
                "l30hr": p.get("l30hr"), "l30Pa": p.get("l30Pa"),
                "l15hrRate": p.get("l15hrRate"), "l30hrRate": p.get("l30hrRate"),
                "oppBullpenEra": p.get("oppBullpenEra"),
                "oppBullpenWhip": p.get("oppBullpenWhip"),
                "oppBullpenIp": p.get("oppBullpenIp"),
            })
    path = f"history/{date_str}.json"
    with open(path, "w") as f:
        json.dump(snapshot, f)
    print(f"Wrote daily snapshot to {path} ({len(snapshot)} players)")


if __name__ == "__main__":
    main()
