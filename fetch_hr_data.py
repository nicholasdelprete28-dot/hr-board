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

WHAT CHANGED IN v3.70 (AVG VS MIX real override): a genuinely bad AVG
VS MIX read (near/at the 0.150 floor introduced in v3.63) was only ever
able to act through its 18%-weighted share of W_SITUATIONAL - meaning
even a player pinned at the worst possible matchup-type read could still
rank #1 overall on the strength of a hot power/recent-form read alone,
because nothing in the blend could ever let one bad signal actually cap
the outcome. Added avg_vs_mix_override_mult() - a real, sample-size-gated
multiplicative penalty applied to the FINAL blended mean (not buried
inside one component), so a genuinely bad, well-sampled matchup-type
read can now meaningfully drag a player down regardless of how hot
everything else is reading.

WHAT CHANGED IN v3.12 (per formula review):
  - FIX #1: PARKS was only covering 15 of 30 teams. All 30 teams are now
    present. HONESTY NOTE: the `factor` values for the 15 newly-added
    parks (and the `orient` values for ALL 30 parks) are reasonable
    placeholders based on each park's general reputation, NOT derived
    from real Statcast park-factor data at the time - since superseded
    by the v3.60 real-park-factor rebuild below.
  - FIX #2: wind_park_factor() took a `direction` parameter and never
    used it - wind could only ever help a batter, never hurt. Each park
    now carries an `orient` field and wind_park_factor() computes the
    in/out component via the angle between actual wind direction and
    that bearing.
  - FIX #3: the stacking dampener in both compute_score() and
    compute_hr_probability() only ever looked at [power, pitcher_s,
    recent] - every other multiplier could still all stack favorably on
    the same player completely unchecked. Both functions now also fold
    in how much the situational multipliers alone are boosting a player.

WHAT CHANGED IN v3 (weighting reform): compute_score() reweighted from 5
buckets to 7: Power 35%, Pitcher 15%, Platoon 10%, Recent 15%,
Opportunity 10%, Park 8%, Wind 7%. Power now blends SEASON Statcast
power with a LAST-15-DAYS version of the same three stats. Pitcher's own
hr9/whip now blend in his last-3-starts form. A day/night HR-rate split
and a bounded multiplier on the final score were added.

KNOWN LIMITATION - SEASON TOTAL/RATE, NOT WITHIN-SEASON TREND: every
"recent" signal this file computes is a fixed recent window measured
against a season baseline. None of it tracks WHERE within the season a
player's production happened. A real trend/trajectory feature - e.g. a
rolling weekly HR rate with a slope or acceleration term - is a
separate, buildable addition and is NOT implemented anywhere in this
file.

WHAT CHANGED IN v3.81 (this session - see chat discussion): a real
double-count bug in compute_hr_probability()'s RECENT FORM component was
found and fixed. The same three barrel/EV/hard-hit L15-vs-season diffs
were being used TWICE in a row to boost the recent-HR-rate signal: once
via the existing outcome-vs-process confirmation block (which raises or
lowers l15_trust/l5_trust when 2+ of those diffs agree with the
direction of a hot/cold outcome streak), and then AGAIN a few dozen
lines later via a newer v3.80 "process_signal" block, which took the
exact same three diffs and applied a second, independent +-16% multiplier
on top of the already-adjusted recent_form_rate. Nothing gated the second
block on whether the first one had already acted on the same signal, so
a batter with genuinely improving/declining Statcast reads got rewarded
(or penalized) for it twice inside one component - the same "component
secretly leaks into a correlated input" failure mode previously found
and fixed for power_baseline_rate (see the v3.50 note further down).

  Empirical confirmation from a real daily snapshot: across ~270 batters
  on the 2026-08-18 slate, raw l15hr (recent HR count) correlated with
  hrProb at 0.555 and barrel% at 0.528 - both HIGHER than
  pitcherHr9Percentile (0.229) despite matchup nominally carrying the
  largest single weight (0.34), and far higher than iso (0.108) despite
  iso being 45% of power's own converted_power half. Recent form and
  barrel were both punching well above their stated weight - this is why
  the same already-hot, established players kept clustering at the top
  regardless of matchup: any real hot streak in barrel/EV/hard-hit was
  being counted against the board twice.

  FIX: the v3.80 process_signal block has been removed entirely from
  compute_hr_probability(). The existing outcome-vs-process confirmation
  logic (v3.51/v3.57, which is sample-size gated and direction-aware) is
  left as the sole mechanism for letting real Statcast movement affect
  trust in a hot/cold recent-HR streak. No other component was touched.
  This is expected to pull barrel/EV/hard-hit's true influence back down
  toward matchup and power's stated weights, and should reduce night-to-
  night top-5 repetition among players whose only real edge was a
  double-counted process read. Recommend a few days of live output
  before making any further weight changes (e.g. raising matchup's real
  pull) - see one clean run first.
"""

import csv
import io
import json
import math
import os
import time
import bisect
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
PARKS = {
    "COL": {"factor": 1.28, "lat": 39.7559, "lon": -104.9942, "orient": 30},
    "LAD": {"factor": 1.20, "lat": 34.0739, "lon": -118.2400, "orient": 25},
    "HOU": {"factor": 1.14, "lat": 29.7573, "lon": -95.3555,  "orient": 55},
    "BOS": {"factor": 1.14, "lat": 42.3467, "lon": -71.0972,  "orient": 45},
    "NYY": {"factor": 1.13, "lat": 40.8296, "lon": -73.9262,  "orient": 75},
    "BAL": {"factor": 1.12, "lat": 39.2839, "lon": -76.6218,  "orient": 30},
    "CIN": {"factor": 1.10, "lat": 39.0975, "lon": -84.5068,  "orient": 100},
    "PHI": {"factor": 1.08, "lat": 39.9061, "lon": -75.1665,  "orient": 5},
    "TEX": {"factor": 1.06, "lat": 32.7473, "lon": -97.0842,  "orient": 30},
    "ATL": {"factor": 1.02, "lat": 33.8908, "lon": -84.4678,  "orient": 15},
    "MIL": {"factor": 1.00, "lat": 43.0280, "lon": -87.9712,  "orient": 130},
    "AZ":  {"factor": 1.00, "lat": 33.4453, "lon": -112.0667, "orient": 55},
    "TOR": {"factor": 1.00, "lat": 43.6414, "lon": -79.3894,  "orient": 60},
    "MIN": {"factor": 1.00, "lat": 44.9817, "lon": -93.2776,  "orient": 90},
    "WSH": {"factor": 1.00, "lat": 38.8730, "lon": -77.0074,  "orient": 55},
    "CWS": {"factor": 1.00, "lat": 41.8299, "lon": -87.6338,  "orient": 135},
    "CLE": {"factor": 1.00, "lat": 41.4962, "lon": -81.6852,  "orient": 5},
    "SEA": {"factor": 1.00, "lat": 47.5914, "lon": -122.3325, "orient": 45},
    "TB":  {"factor": 1.00, "lat": 27.7683, "lon": -82.6534,  "orient": 90},
    "MIA": {"factor": 1.00, "lat": 25.7781, "lon": -80.2196,  "orient": 30},
    "CHC": {"factor": 1.00, "lat": 41.9484, "lon": -87.6553,  "orient": 30},
    "SD":  {"factor": 0.93, "lat": 32.7073, "lon": -117.1566, "orient": 5},
    "KC":  {"factor": 0.95, "lat": 39.0517, "lon": -94.4803,  "orient": 60},
    "DET": {"factor": 0.95, "lat": 42.3390, "lon": -83.0485,  "orient": 25},
    "LAA": {"factor": 0.94, "lat": 33.8003, "lon": -117.8827, "orient": 20},
    "ATH": {"factor": 0.97, "lat": 37.7516, "lon": -122.2005, "orient": 45},
    "STL": {"factor": 0.88, "lat": 38.6226, "lon": -90.1928,  "orient": 45},
    "NYM": {"factor": 0.87, "lat": 40.7571, "lon": -73.8458,  "orient": 30},
    "PIT": {"factor": 0.85, "lat": 40.4469, "lon": -80.0057,  "orient": 65},
    "SF":  {"factor": 0.85, "lat": 37.7786, "lon": -122.3893, "orient": 95},
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
            if g.get("gameNumber") == 2:
                continue
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


def get_pitcher_recent_hr9(pitcher_id, season):
    starts = get_pitcher_gamelog(pitcher_id, season)
    last3 = starts[-3:]
    ip_total = sum(g["ip"] or 0 for g in last3)
    if ip_total <= 0:
        return None, 0
    recent_hr9 = sum(g["hr"] for g in last3) * 9 / ip_total
    return round(recent_hr9, 2), round(ip_total, 1)


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


_batside_cache = {}


def get_player_bat_side(player_id):
    if player_id in _batside_cache:
        return _batside_cache[player_id]
    try:
        data = statsapi_get(f"people/{player_id}")
        side = data.get("people", [{}])[0].get("batSide", {}).get("code", "R")
    except Exception:
        side = "R"
    _batside_cache[player_id] = side
    return side


_pitcher_platoon_cache = {}


def get_pitcher_platoon_split(pitcher_id, vs_hand):
    cache_key = (pitcher_id, vs_hand)
    if cache_key in _pitcher_platoon_cache:
        return _pitcher_platoon_cache[cache_key]
    sit_code = "vl" if vs_hand == "L" else "vr"
    result = (None, 0)
    try:
        data = statsapi_get(f"people/{pitcher_id}/stats", {
            "stats": "statSplits", "sitCodes": sit_code,
            "group": "pitching", "season": YEAR, "sportId": 1
        })
        splits = data.get("stats", [{}])[0].get("splits", [])
        if splits:
            stat = splits[0]["stat"]
            ip = _parse_innings(stat.get("inningsPitched"))
            hr = int(stat.get("homeRuns", 0) or 0)
            if ip and ip > 0:
                result = (round(hr * 9 / ip, 2), ip)
    except Exception:
        pass
    _pitcher_platoon_cache[cache_key] = result
    return result


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
    matching_games = [g for g in games
                       if g.get("date") and datetime.date.fromisoformat(g["date"]).weekday() == target_weekday]
    pa = sum(g["pa"] for g in matching_games)
    hr = sum(g["hr"] for g in matching_games)
    return (hr / pa if pa > 0 else None), pa


def home_road_split(games):
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
            "current": "wind_speed_10m,wind_direction_10m,temperature_2m",
            "wind_speed_unit": "mph",
            "temperature_unit": "fahrenheit",
        }, timeout=15)
        resp.raise_for_status()
        cur = resp.json().get("current", {})
        return cur.get("wind_speed_10m"), cur.get("wind_direction_10m"), cur.get("temperature_2m")
    except Exception:
        return None, None, None


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
    la_columns = ["launch_angle"]
    la_col = next((c for c in la_columns if rows and c in rows[0]), None)
    print(f"  L15 using batter_id={id_col} type={type_col} "
          f"launch_speed={ls_col} launch_speed_angle={lsa_col} launch_angle={la_col}")

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
        d = per_batter.setdefault(pid, {"ev_sum": 0.0, "n": 0, "barrels": 0, "hardhit": 0,
                                         "la_sum": 0.0, "la_n": 0})
        d["ev_sum"] += ls
        d["n"] += 1
        if is_barrel:
            d["barrels"] += 1
        if ls >= 95:
            d["hardhit"] += 1
        la_raw = row.get(la_col) if la_col else None
        if la_raw not in (None, ""):
            try:
                d["la_sum"] += float(la_raw)
                d["la_n"] += 1
            except (TypeError, ValueError):
                pass

    out = {}
    for pid, d in per_batter.items():
        if d["n"] <= 0:
            continue
        out[pid] = {
            "ev": round(d["ev_sum"] / d["n"], 1),
            "barrel": round(d["barrels"] / d["n"], 3),
            "hardhit": round(d["hardhit"] / d["n"], 3),
            "pa": d["n"],
            "launchAngle": round(d["la_sum"] / d["la_n"], 1) if d["la_n"] > 0 else None,
        }
    print(f"  parsed L15 power data for {len(out)} batters from "
          f"{sum(d['n'] for d in per_batter.values())} batted-ball events")
    return out


def fetch_pitch_mix_data():
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
    try:
        data = statsapi_get(f"teams/{team_id}/roster", {"rosterType": "active"})
        return data.get("roster", [])
    except Exception:
        return []


BULLPEN_MIN_IP = 20

LEAGUE_AVG_BULLPEN_ERA = 4.20
LEAGUE_AVG_BULLPEN_WHIP = 1.32
LEAGUE_AVG_IP_PER_START = 5.2


def bullpen_exposure_weight(opp_ip_per_start):
    if opp_ip_per_start is None or opp_ip_per_start <= 0:
        return 1.0
    ratio = LEAGUE_AVG_IP_PER_START / opp_ip_per_start
    return max(0.6, min(1.5, ratio))


def get_team_bullpen_stats(team_id, season):
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


def wind_park_factor(speed, direction, park_orientation=None):
    if speed is None or speed < 5:
        return 0
    if direction is None or park_orientation is None:
        base = 2 if speed >= 15 else (1 if speed >= 8 else 0)
        return base
    diff = abs((direction - park_orientation + 180) % 360 - 180)
    out_component = math.cos(math.radians(diff))
    if speed >= 15:
        return round(2 * out_component, 2)
    if speed >= 8:
        return round(1 * out_component, 2)
    return 0


def describe_wind(speed, direction, park_orientation):
    if speed is None:
        return None
    if speed < 5:
        return "Calm"
    if direction is None or park_orientation is None:
        return f"{round(speed)} mph"
    diff = abs((direction - park_orientation + 180) % 360 - 180)
    out_component = math.cos(math.radians(diff))
    tier = "Strong" if speed >= 15 else "Light"
    if out_component >= 0.5:
        return f"{tier} - Blowing Out ({round(speed)} mph)"
    if out_component <= -0.5:
        return f"{tier} - Blowing In ({round(speed)} mph)"
    return f"{tier} Crosswind ({round(speed)} mph)"


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
LEAGUE_AVG_HR_RATE_PA = 0.033
LEAGUE_AVG_AVG = 0.245
LEAGUE_AVG_OBP = 0.315

POWER_L15_WEIGHT = 0.35
POWER_L15_MIN_PA = 15
L15_ISO_MIN_PA = 30

DAY_NIGHT_MAX_ADJ = 0.04
DAY_NIGHT_MIN_PA = 60
DOW_MAX_ADJ = 0.03
DOW_MIN_PA = 25
TREND_MAX_ADJ = 0.06
TREND_MIN_PA = 40
TREND_L5_MIN_PA = 15
TREND_L5_SHRINK_K = 38


def power_sample_weight(pa):
    if pa is None or pa <= 0:
        return 0.3
    return pa / (pa + POWER_SHRINK_K)


PITCHER_SHRINK_K = 20


def pitcher_sample_weight(ip):
    if ip is None or ip <= 0:
        return 0.0
    return min(1.0, ip / (ip + PITCHER_SHRINK_K))


def day_of_week_adjustment(p):
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
    l15_rate = p.get("l15hrRate")
    l30_rate = p.get("l30hrRate")
    l30_pa = p.get("l30Pa") or 0
    l5_credit = p.get("l5hrCredit")
    l5_pa = p.get("l5Pa") or 0

    if l15_rate is None or l30_rate is None or l30_rate <= 0 or l30_pa < TREND_MIN_PA:
        return 1.0

    raw_ratio = l15_rate / l30_rate
    broad_adj = clamp01((raw_ratio - 1) * 0.3 + 0.5) - 0.5
    broad_adj = max(-TREND_MAX_ADJ, min(TREND_MAX_ADJ, broad_adj))

    if l5_credit is None or l5_pa < TREND_L5_MIN_PA or l15_rate <= 0:
        return 1 + broad_adj

    l5_rate = l5_credit / 5
    short_ratio = l5_rate / l15_rate
    short_direction = max(-1.0, min(1.0, (short_ratio - 1.0) * 2.0))
    short_trust = l5_pa / (l5_pa + TREND_L5_SHRINK_K)

    if broad_adj != 0 and short_direction * broad_adj > 0:
        broad_adj *= 1 + 0.30 * short_trust * abs(short_direction)
    elif broad_adj != 0 and short_direction * broad_adj < 0:
        broad_adj *= max(0.55, 1 - 0.35 * short_trust * abs(short_direction))

    broad_adj = max(-TREND_MAX_ADJ, min(TREND_MAX_ADJ, broad_adj))
    return 1 + broad_adj


def compute_hr_subfactors(p):
    barrel = p["barrel"] or 0
    ev = p["ev"] or 85
    iso = p["iso"] or 0
    hardhit = p["hardhit"] or 0.30
    pw = power_sample_weight(p.get("pa"))
    barrel_season = barrel * pw + LEAGUE_AVG_BARREL * (1 - pw)
    ev_season = ev * pw + LEAGUE_AVG_EV * (1 - pw)
    iso_season = iso * pw + LEAGUE_AVG_ISO * (1 - pw)
    hardhit_season = hardhit * pw + LEAGUE_AVG_HARDHIT * (1 - pw)

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

    if lw > 0 and l15_barrel is not None and l15_ev is not None and l15_hardhit is not None:
        AGREEMENT_MIN_BARREL = 0.02
        AGREEMENT_MIN_EV = 1.0
        AGREEMENT_MIN_HARDHIT = 0.03
        AGREEMENT_CAP_2OF3 = 0.40
        AGREEMENT_CAP_3OF3 = 0.60
        barrel_diff = l15_barrel - barrel_season
        ev_diff = l15_ev - ev_season
        hardhit_diff = l15_hardhit - hardhit_season
        moves = []
        if abs(barrel_diff) >= AGREEMENT_MIN_BARREL:
            moves.append(1 if barrel_diff > 0 else -1)
        if abs(ev_diff) >= AGREEMENT_MIN_EV:
            moves.append(1 if ev_diff > 0 else -1)
        if abs(hardhit_diff) >= AGREEMENT_MIN_HARDHIT:
            moves.append(1 if hardhit_diff > 0 else -1)
        if len(moves) >= 2 and len(set(moves)) == 1:
            if len(moves) == 3:
                lw = min(AGREEMENT_CAP_3OF3, lw * 4.0)
            else:
                lw = min(AGREEMENT_CAP_2OF3, lw * 1.5)

    if l15_barrel is not None and lw > 0:
        barrel_final = l15_barrel * lw + barrel_season * (1 - lw)
        ev_final = l15_ev * lw + ev_season * (1 - lw)
        hardhit_final = l15_hardhit * lw + hardhit_season * (1 - lw)
    else:
        barrel_final, ev_final, hardhit_final = barrel_season, ev_season, hardhit_season

    barrel_diff = (l15_barrel - barrel_season) if l15_barrel is not None else None
    ev_diff = (l15_ev - ev_season) if l15_ev is not None else None
    hardhit_diff = (l15_hardhit - hardhit_season) if l15_hardhit is not None else None

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
    park = p["park"] if p["park"] is not None else 1.0
    l15hr = p.get("l15hrCredit") if p.get("l15hrCredit") is not None else (p["l15hr"] if p["l15hr"] is not None else 0)
    l5hr = p.get("l5hrCredit") if p.get("l5hrCredit") is not None else (p["l5hr"] if p.get("l5hr") is not None else 0)
    lbonus = p["lbonus"] if p["lbonus"] is not None else 3
    crush = p["crush"] or 0
    split = p["split"] or 0

    conf = barrel_confidence(barrel_final, ev_final)
    barrel_adj = barrel_final * conf

    barrel_n = clamp01((barrel_adj - 0.05) / 0.15)
    ev_n = clamp01((ev_final - 87.0) / 9.0)
    hardhit_n = clamp01((hardhit_final - 0.30) / 0.28)
    quality_of_contact = barrel_n * 0.50 + ev_n * 0.25 + hardhit_n * 0.25

    iso_n = clamp01((iso_final - 0.08) / 0.27)

    HR_RATE_FLOOR = 0.01
    HR_RATE_CEIL = 0.07
    season_hr_rate_raw = p.get("seasonHrRate")
    season_hr_rate_shrunk = (season_hr_rate_raw * pw + LEAGUE_AVG_HR_RATE_PA * (1 - pw)
                              if season_hr_rate_raw is not None else LEAGUE_AVG_HR_RATE_PA)
    hr_rate_n = clamp01((season_hr_rate_shrunk - HR_RATE_FLOOR) / (HR_RATE_CEIL - HR_RATE_FLOOR))
    converted_power = (iso_n + hr_rate_n) / 2

    power = quality_of_contact * 0.55 + converted_power * 0.45

    phr9_s = clamp01((phr9 - 0.3) / 2.2)
    whip_s = clamp01((whip - 0.9) / 1.15)
    pitcher_s = (phr9_s + whip_s) / 2

    wind_s = clamp01((wind + 2) / 4)
    park_s = clamp01((park - 0.85) / 0.43)

    temp_f = p.get("temp")
    if temp_f is not None:
        temp_s = clamp01(0.5 + (temp_f - 70) * 0.0075)
    else:
        temp_s = 0.5

    RECENT_SHRINK_K = 38
    L5_SHRINK_K = 38
    l15_recent_pa = p.get("l15IsoPa") or 0
    l5_recent_pa = p.get("l5Pa") or 0
    l15_trust = l15_recent_pa / (l15_recent_pa + RECENT_SHRINK_K) if l15_recent_pa > 0 else 0.0
    l5_trust = l5_recent_pa / (l5_recent_pa + L5_SHRINK_K) if l5_recent_pa > 0 else 0.0
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
        avg_vs_mix_s = clamp01((avg_vs_mix - 0.150) / (0.320 - 0.150))
        platoon = pitch_mix_platoon * 0.40 + avgmix_s * 0.10 + avg_vs_mix_s * 0.50
    else:
        avg_vs_mix_s = None
        platoon = pitch_mix_platoon * 0.65 + avgmix_s * 0.35

    opportunity = clamp01((lbonus - 1) / 5)

    return {
        "power": power, "pitcher_s": pitcher_s, "platoon": platoon,
        "recent": recent, "opportunity": opportunity,
        "park_s": park_s, "wind_s": wind_s, "temp_s": temp_s,
        "conf": conf, "avg_vs_mix": avg_vs_mix, "avg_vs_mix_s": avg_vs_mix_s,
        "barrel_diff": barrel_diff, "ev_diff": ev_diff, "hardhit_diff": hardhit_diff,
    }


def compute_score(p):
    sf = compute_hr_subfactors(p)
    power, pitcher_s, platoon, recent, park_s, wind_s, temp_s = (
        sf["power"], sf["pitcher_s"], sf["platoon"], sf["recent"], sf["park_s"], sf["wind_s"], sf["temp_s"])

    score = power * 39 + pitcher_s * 17 + platoon * 11 + recent * 17 + park_s * 7 + wind_s * 5 + temp_s * 4

    score *= day_night_adjustment(p)
    score *= trend_adjustment(p)
    score = max(0.0, min(100.0, score))

    return {
        "score": round(score, 1),
        "conf": sf["conf"],
        "powerPct": round(power * 100, 1),
        "pitcherPct": round(pitcher_s * 100, 1),
        "platoonPct": round(platoon * 100, 1),
        "recentPct": round(recent * 100, 1),
        "opportunityPct": round(sf["opportunity"] * 100, 1),
        "parkPct": round(park_s * 100, 1),
        "windPct": round(wind_s * 100, 1),
        "tempPct": round(temp_s * 100, 1),
        "avgVsMixPct": round(sf["avg_vs_mix_s"] * 100, 1) if sf["avg_vs_mix_s"] is not None else None,
    }


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

HR_TIER_PRIME_PCT = 0.06
HR_TIER_STRONG_PCT = 0.28
HR_TIER_INPLAY_PCT = 0.28


def hr_tier_for_percentile(hr_prob_val, todays_hr_probs):
    if hr_prob_val is None or len(todays_hr_probs) < 10:
        return None
    idx = bisect.bisect_left(todays_hr_probs, hr_prob_val)
    percentile = idx / (len(todays_hr_probs) - 1)
    if percentile >= 1 - HR_TIER_PRIME_PCT:
        return "prime"
    if percentile >= 1 - HR_TIER_PRIME_PCT - HR_TIER_STRONG_PCT:
        return "strong"
    if percentile >= 1 - HR_TIER_PRIME_PCT - HR_TIER_STRONG_PCT - HR_TIER_INPLAY_PCT:
        return "inplay"
    return "longshot"


AVG_VS_MIX_PENALTY_MIN_PA = 40
AVG_VS_MIX_PENALTY_THRESHOLD = 0.30
AVG_VS_MIX_MAX_PENALTY = 0.35


def avg_vs_mix_override_mult(avg_vs_mix_s, avg_vs_mix_pa):
    if avg_vs_mix_s is None or avg_vs_mix_pa is None:
        return 1.0
    if avg_vs_mix_pa < AVG_VS_MIX_PENALTY_MIN_PA:
        return 1.0
    if avg_vs_mix_s >= AVG_VS_MIX_PENALTY_THRESHOLD:
        return 1.0
    severity = (AVG_VS_MIX_PENALTY_THRESHOLD - avg_vs_mix_s) / AVG_VS_MIX_PENALTY_THRESHOLD
    return 1 - severity * AVG_VS_MIX_MAX_PENALTY


def compute_hr_probability(p):
    """v3.48: additive blend of independent rate estimates (see module
    docstring for the full v3.48 rationale).

    v3.81 FIX (this session): the v3.80 "process_signal" block that used
    to sit at the end of the RECENT FORM section - re-deriving a second,
    independent +-16% multiplier from the exact same barrel/EV/hard-hit
    L15-vs-season diffs already used a few lines above to boost/discount
    l15_trust and l5_trust - has been REMOVED. It was double-counting the
    same signal within a single component. The existing outcome-vs-
    process confirmation block (v3.51/v3.57) is left as the sole
    mechanism for letting real Statcast movement affect trust in a
    hot/cold recent-HR streak. See the module docstring for the
    empirical evidence (l15hr/barrel correlating with hrProb far above
    their stated weight) that motivated this."""
    sf = compute_hr_subfactors(p)
    power_quality = sf["power"]

    # --- Component 1: POWER BASELINE (his true talent level) ---
    POWER_QUALITY_MULTIPLIER = 1.1
    power_baseline_rate = LEAGUE_AVG_HR_RATE * (0.3 + power_quality * POWER_QUALITY_MULTIPLIER)

    prior_year = p.get("seasonPrev")
    if prior_year and prior_year.get("n", 0) >= 20:
        prior_hr_rate = prior_year["hrPct"] / 100
        current_pa = p.get("pa") or 0
        PRIOR_YEAR_SHRINK_K = 150
        current_trust = current_pa / (current_pa + PRIOR_YEAR_SHRINK_K) if current_pa > 0 else 0.0
        power_baseline_rate = power_baseline_rate * current_trust + prior_hr_rate * (1 - current_trust)

    # --- Component 2: MATCHUP QUALITY (batter-independent, day-specific) ---
    phr9 = p.get("phr9") if p.get("phr9") is not None else LEAGUE_AVG_PITCHER_HR9
    pw_pitcher = pitcher_sample_weight(p.get("pip"))
    effective_phr9 = phr9 * pw_pitcher + LEAGUE_AVG_PITCHER_HR9 * (1 - pw_pitcher)

    recent_phr9 = p.get("pitcherRecentHr9")
    recent_pip = p.get("pitcherRecentIp") or 0
    if recent_phr9 is not None and recent_pip > 0:
        PITCHER_RECENT_SHRINK_K = 15
        PITCHER_RECENT_DAMPEN = 0.5
        recent_trust = recent_pip / (recent_pip + PITCHER_RECENT_SHRINK_K)
        effective_phr9 += (recent_phr9 - effective_phr9) * recent_trust * PITCHER_RECENT_DAMPEN

    MATCHUP_QUALITY_SENSITIVITY = 1.1
    matchup_ratio = effective_phr9 / LEAGUE_AVG_PITCHER_HR9
    park_raw = p.get("park")
    if park_raw is None:
        park_mult = 1.0
    else:
        park_mult = max(0.92, min(1.12, 1 + (park_raw - 1.0) * 0.45))
    wind_mult = 1 + (sf["wind_s"] - 0.5) * 0.16
    temp_mult = 1 + (sf["temp_s"] - 0.5) * 0.08
    park_wind_mult = park_mult * wind_mult * temp_mult

    bullpen_era = p.get("oppBullpenEra")
    bullpen_whip = p.get("oppBullpenWhip")
    bullpen_ip = p.get("oppBullpenIp") or 0
    bullpen_mult = 1.0
    if bullpen_era is not None and bullpen_whip is not None and bullpen_ip >= BULLPEN_MIN_IP:
        bullpen_ratio = (bullpen_era / LEAGUE_AVG_BULLPEN_ERA + bullpen_whip / LEAGUE_AVG_BULLPEN_WHIP) / 2
        exposure = bullpen_exposure_weight(p.get("oppIpPerStart"))
        BULLPEN_SENSITIVITY = 0.25
        bullpen_mult = 1 + (bullpen_ratio - 1) * BULLPEN_SENSITIVITY * exposure

    hand_hr9 = p.get("phr9VsHand")
    hand_ip = p.get("phr9VsHandIp") or 0
    if hand_hr9 is not None and hand_ip > 0 and effective_phr9 > 0:
        HAND_SPLIT_SHRINK_K = 60
        HAND_SPLIT_MAX_ADJ = 0.12
        hand_trust = hand_ip / (hand_ip + HAND_SPLIT_SHRINK_K)
        hand_ratio = hand_hr9 / effective_phr9
        hand_adj = max(-HAND_SPLIT_MAX_ADJ, min(HAND_SPLIT_MAX_ADJ, (hand_ratio - 1) * hand_trust))
        matchup_ratio_for_batter = matchup_ratio * (1 + hand_adj)
    else:
        matchup_ratio_for_batter = matchup_ratio

    matchup_deviation = matchup_ratio_for_batter - 1.0
    deviation_abs = abs(matchup_deviation)
    ordinary_dev = 0.65 * min(deviation_abs, 0.20)
    extreme_dev = (max(deviation_abs - 0.20, 0.0) ** 1.25) * 1.15
    nonlinear_deviation = (ordinary_dev + extreme_dev) * (1 if matchup_deviation >= 0 else -1)

    absolute_matchup_rate = max(0.01, LEAGUE_AVG_HR_RATE
                                 * (1 + nonlinear_deviation * MATCHUP_QUALITY_SENSITIVITY)
                                 * park_wind_mult * bullpen_mult)

    pitcher_percentile_today = p.get("pitcherHr9Percentile")
    if pitcher_percentile_today is not None:
        relative_matchup_rate = LEAGUE_AVG_HR_RATE * (0.70 + pitcher_percentile_today * 0.60) * park_wind_mult
        matchup_quality_rate = max(0.01, absolute_matchup_rate * 0.70 + relative_matchup_rate * 0.30)
    else:
        matchup_quality_rate = absolute_matchup_rate

    # --- Component 3: RECENT FORM (existing mechanism, reused as-is) ---
    l15hr = p.get("l15hr")
    l5hr = p.get("l5hr")
    if l15hr is not None:
        l15hr_for_rate = p.get("l15hrCredit") if p.get("l15hrCredit") is not None else l15hr
        l5hr_for_rate = p.get("l5hrCredit") if p.get("l5hrCredit") is not None else l5hr

        RECENT_SHRINK_K = 38
        L5_SHRINK_K = 38
        l15_pa = p.get("l15IsoPa") or 0
        l5_pa = p.get("l5Pa") or 0
        l15_rate_raw = l15hr_for_rate / 15
        l5_rate_raw = (l5hr_for_rate / 5) if l5hr_for_rate is not None else l15_rate_raw
        l15_trust = l15_pa / (l15_pa + RECENT_SHRINK_K) if l15_pa > 0 else 0.0
        l5_trust = l5_pa / (l5_pa + L5_SHRINK_K) if l5_pa > 0 else 0.0

        matchup_is_great = matchup_ratio >= 2.0

        HOT_OUTCOME_MULT = 1.5
        if l15_rate_raw > power_baseline_rate * HOT_OUTCOME_MULT:
            proc_moves = []
            for diff, min_move, vote_weight in (
                (sf.get("barrel_diff"), 0.01, 2),
                (sf.get("ev_diff"), 1.0, 1),
                (sf.get("hardhit_diff"), 0.03, 1),
            ):
                if diff is not None and abs(diff) >= min_move:
                    proc_moves.extend([1 if diff > 0 else -1] * vote_weight)
            if len(proc_moves) >= 2 and len(set(proc_moves)) == 1:
                if proc_moves[0] < 0:
                    l15_trust *= 0.75 if matchup_is_great else 0.6
                    l5_trust *= 0.75 if matchup_is_great else 0.6
                else:
                    boost = 1.35 if len(proc_moves) >= 3 else 1.20
                    l15_trust = min(1.0, l15_trust * boost)
                    l5_trust = min(1.0, l5_trust * boost)
            else:
                discount = 0.97 if matchup_is_great else 0.9
                l15_trust *= discount
                l5_trust *= discount

        platoon_score = sf.get("platoon")
        if platoon_score is not None and platoon_score < 0.30 and l15_rate_raw > LEAGUE_AVG_HR_RATE * 1.3:
            l15_trust *= 0.7
            l5_trust *= 0.7

        l15_rate_regressed = l15_rate_raw * l15_trust + LEAGUE_AVG_HR_RATE * (1 - l15_trust)
        l5_rate_regressed = l5_rate_raw * l5_trust + LEAGUE_AVG_HR_RATE * (1 - l5_trust)
        recent_rate_raw = l15_rate_regressed * 0.75 + l5_rate_regressed * 0.25

        pw = power_sample_weight(p.get("pa"))
        absolute_recent_rate = recent_rate_raw * pw + LEAGUE_AVG_HR_RATE * (1 - pw)

        recent_percentile_today = p.get("recentFormPercentile")
        if recent_percentile_today is not None:
            relative_recent_rate = LEAGUE_AVG_HR_RATE * (0.75 + recent_percentile_today * 0.50)
            recent_form_rate = max(0.01, absolute_recent_rate * 0.70 + relative_recent_rate * 0.30)
        else:
            recent_form_rate = absolute_recent_rate

        # v3.81: the old v3.80 "process_signal" re-application of
        # barrel_diff/ev_diff/hardhit_diff has been removed here - it was
        # a second, independent multiplier built from the exact same
        # inputs the l15_trust/l5_trust confirmation block above already
        # used. See the module docstring and this function's docstring
        # for the full reasoning.
    else:
        recent_form_rate = LEAGUE_AVG_HR_RATE

    # --- Component 4: PERSONAL SITUATIONAL (his own real tendencies) ---
    PERSONAL_STRENGTH = 0.65
    personal_mult = 1 + (sf["platoon"] - 0.5) * PERSONAL_STRENGTH
    personal_mult *= day_night_adjustment(p)
    personal_situational_rate = LEAGUE_AVG_HR_RATE * personal_mult

    # --- Blend, with the matchup-quality weight power-gated ---
    W_POWER = 0.24
    W_MATCHUP = 0.34
    W_RECENT = 0.24
    W_SITUATIONAL = 0.18

    POWER_GATE_FLOOR = 0.20
    POWER_GATE_CEIL = 0.35
    MATCHUP_WEIGHT_MIN = 0.15
    power_gate = clamp01((power_quality - POWER_GATE_FLOOR) / (POWER_GATE_CEIL - POWER_GATE_FLOOR))
    effective_w_matchup = MATCHUP_WEIGHT_MIN + (W_MATCHUP - MATCHUP_WEIGHT_MIN) * power_gate
    effective_w_power = W_POWER + (W_MATCHUP - effective_w_matchup)

    mean = (effective_w_power * power_baseline_rate
            + effective_w_matchup * matchup_quality_rate
            + W_RECENT * recent_form_rate
            + W_SITUATIONAL * personal_situational_rate)

    GLOBAL_SCALE = 1.6
    mean *= GLOBAL_SCALE

    batter_pa = p.get("pa") or 0
    confidence = 0.88 + 0.12 * (batter_pa / (batter_pa + 250)) if batter_pa > 0 else 0.88
    mean = LEAGUE_AVG_HR_RATE + (mean - LEAGUE_AVG_HR_RATE) * confidence

    mean *= trend_adjustment(p)

    mean *= avg_vs_mix_override_mult(sf.get("avg_vs_mix_s"), p.get("avgVsMixPa"))

    mean = max(0.01, mean)

    raw_prob = poisson_over_prob(mean, 0.5)
    if raw_prob is None:
        return None

    HR_PROB_HARD_CAP = 0.45
    return min(raw_prob, HR_PROB_HARD_CAP)


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
    return sum(min(g["hr"], 1) + max(g["hr"] - 1, 0) * 0.15 for g in games)


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

    print("Fetching pitcher recent form (last 3 starts HR/9)...")

    def fetch_one_pitcher_recent(pid):
        return pid, get_pitcher_recent_hr9(pid, YEAR)

    pitcher_recent_hr9 = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        for pid, (recent_hr9, recent_ip) in executor.map(fetch_one_pitcher_recent, todays_pitcher_ids):
            if recent_hr9 is not None:
                pitcher_recent_hr9[pid] = (recent_hr9, recent_ip)
    print(f"  got recent-form data for {len(pitcher_recent_hr9)} of {len(todays_pitcher_ids)} probable starters")

    def _pool_hr9(pid):
        stat = reliable_pitcher_stats.get(pid, pitching_stats.get(pid, {}))
        season_hr9 = stat.get("hr9")
        if season_hr9 is None:
            return None
        recent_hr9_val, recent_ip_val = pitcher_recent_hr9.get(pid, (None, 0))
        if recent_hr9_val is not None and recent_ip_val > 0:
            _trust = recent_ip_val / (recent_ip_val + 15)
            return season_hr9 + (recent_hr9_val - season_hr9) * _trust * 0.5
        return season_hr9

    todays_pitcher_hr9_values = sorted(
        v for v in (_pool_hr9(pid) for pid in todays_pitcher_ids) if v is not None
    )
    print(f"  today's real pitcher HR/9 pool: {len(todays_pitcher_hr9_values)} starters, "
          f"range {todays_pitcher_hr9_values[0]:.2f}-{todays_pitcher_hr9_values[-1]:.2f}"
          if todays_pitcher_hr9_values else "  today's real pitcher HR/9 pool: empty")

    def pitcher_hr9_percentile_today(hr9):
        if hr9 is None or len(todays_pitcher_hr9_values) < 2:
            return 0.5
        idx = bisect.bisect_left(todays_pitcher_hr9_values, hr9)
        return idx / (len(todays_pitcher_hr9_values) - 1)

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

            recent_hr9_val, recent_ip_val = pitcher_recent_hr9.get(pitcher_id, (None, 0))
            if recent_hr9_val is not None and recent_ip_val > 0:
                _k, _dampen = 15, 0.5
                _trust = recent_ip_val / (recent_ip_val + _k)
                percentile_hr9 = effective_hr9 + (recent_hr9_val - effective_hr9) * _trust * _dampen
            else:
                percentile_hr9 = effective_hr9

            park = PARKS.get(g["home_team"], {"factor": 1.0, "lat": None, "lon": None, "orient": None})
            wind_speed, wind_dir, temp_f = (None, None, None)
            if park.get("lat") is not None:
                wind_speed, wind_dir, temp_f = get_wind(park["lat"], park["lon"])
            wind_score = wind_park_factor(wind_speed, wind_dir, park.get("orient"))
            wind_desc = describe_wind(wind_speed, wind_dir, park.get("orient"))

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
                    "oppIpPerStart": pitcher_stat.get("ipPerStart"),
                    "oppPitcherId": pitcher_id,
                    "pitcherHr9Percentile": pitcher_hr9_percentile_today(percentile_hr9),
                    "pitcherRecentHr9": pitcher_recent_hr9.get(pitcher_id, (None, 0))[0],
                    "pitcherRecentIp": pitcher_recent_hr9.get(pitcher_id, (None, 0))[1],
                    "barrel": sc.get("barrel"),
                    "ev": sc.get("ev"),
                    "hardhit": sc.get("hardhit"),
                    "l15Barrel": sc_l15.get("barrel"),
                    "l15Ev": sc_l15.get("ev"),
                    "l15Hardhit": sc_l15.get("hardhit"),
                    "l15PowerPa": sc_l15.get("pa"),
                    "l15LaunchAngle": sc_l15.get("launchAngle"),
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
                    "temp": temp_f,
                    "windDesc": wind_desc,
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

        bat_side = get_player_bat_side(batter_id)
        effective_vs_hand = ("R" if pitcher_hand == "L" else "L") if bat_side == "S" else bat_side
        opp_pitcher_id = player_row.get("oppPitcherId")
        phr9_vs_hand, phr9_vs_hand_ip = (None, 0)
        if opp_pitcher_id:
            phr9_vs_hand, phr9_vs_hand_ip = get_pitcher_platoon_split(opp_pitcher_id, effective_vs_hand)
        player_row["phr9VsHand"] = phr9_vs_hand
        player_row["phr9VsHandIp"] = phr9_vs_hand_ip
        player_row["batSide"] = bat_side

        games_this_year = get_gamelog(batter_id, YEAR)
        totals_prev_year = get_season_totals_hitting(batter_id, YEAR - 1)
        last20 = games_this_year[-20:]
        l15hr = sum(g["hr"] for g in games_this_year[-15:]) if games_this_year else None
        l5hr = sum(g["hr"] for g in games_this_year[-5:]) if games_this_year else None
        l30_games = games_this_year[-30:] if games_this_year else []
        l30hr = sum(g["hr"] for g in l30_games)
        l30hr_credit = diminishing_hr_credit(l30_games)
        l30_pa = sum(g["pa"] for g in l30_games)
        l15_games_for_trend = games_this_year[-15:] if games_this_year else []
        l15hr_credit_for_trend = diminishing_hr_credit(l15_games_for_trend)
        l15hr_rate = (l15hr_credit_for_trend / len(l15_games_for_trend)) if l15_games_for_trend else None
        l30hr_rate = (l30hr_credit / len(l30_games)) if l30_games else None
        l15_games_for_iso = games_this_year[-15:] if games_this_year else []
        l15_iso_pa = sum(g["pa"] for g in l15_games_for_iso)
        l15_iso_val = (round((sum(g["tb"] for g in l15_games_for_iso) - sum(g["hits"] for g in l15_games_for_iso)) / l15_iso_pa, 3)
                       if l15_iso_pa > 0 else None)
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
        player_row["seasonPrev"] = season_totals_to_window(totals_prev_year)
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

    todays_recent_rates = sorted(
        (p.get("l15hrCredit") if p.get("l15hrCredit") is not None else (p.get("l15hr") or 0)) / 15 * 0.6
        + (p.get("l5hrCredit") if p.get("l5hrCredit") is not None else (p.get("l5hr") or 0)) / 5 * 0.4
        for p in players
    )
    print(f"  today's real recent-form pool: {len(todays_recent_rates)} batters")

    def recent_form_percentile_today(l15hr_credit, l5hr_credit):
        if len(todays_recent_rates) < 2:
            return 0.5
        raw_rate = (l15hr_credit or 0) / 15 * 0.6 + (l5hr_credit or 0) / 5 * 0.4
        idx = bisect.bisect_left(todays_recent_rates, raw_rate)
        return idx / (len(todays_recent_rates) - 1)

    for player_row in players:
        player_row["recentFormPercentile"] = recent_form_percentile_today(
            player_row.get("l15hrCredit"), player_row.get("l5hrCredit"))

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

    todays_hr_probs = sorted(
        p["hrProb"] for p in players
        if p.get("playerType") == "batter" and p.get("hrProb") is not None
    )
    print(f"  today's real hrProb pool for tiering: {len(todays_hr_probs)} batters, "
          f"range {todays_hr_probs[0]:.1f}-{todays_hr_probs[-1]:.1f}"
          if todays_hr_probs else "  today's real hrProb pool for tiering: empty")
    for player_row in players:
        if player_row.get("playerType") == "batter":
            player_row["tier"] = hr_tier_for_percentile(player_row.get("hrProb"), todays_hr_probs)
    if todays_hr_probs:
        from collections import Counter
        tier_counts = Counter(p.get("tier") for p in players if p.get("playerType") == "batter")
        print(f"  HR tier counts (percentile-based): prime={tier_counts.get('prime',0)} "
              f"strong={tier_counts.get('strong',0)} inplay={tier_counts.get('inplay',0)} "
              f"longshot={tier_counts.get('longshot',0)}")

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
    os.makedirs("history", exist_ok=True)
    date_str = TODAY
    snapshot = []
    for p in players:
        if p.get("playerType") == "pitcher":
            snapshot.append({
                "playerId": p.get("playerId"), "player": p.get("player"),
                "playerType": "pitcher", "team": p.get("team"), "opponent": p.get("opponent"),
                "game": p.get("game"), "gameStatus": p.get("gameStatus"), "gameTime": p.get("gameTime"),
                "hand": p.get("hand"),
                "kScore": p.get("kScore"), "kLine": p.get("kLine"), "projK": p.get("projK"),
                "k9": p.get("k9"), "era": p.get("era"), "whip": p.get("whip"), "bb9": p.get("bb9"),
                "oppKRate": p.get("oppKRate"), "ipPerStart": p.get("ipPerStart"),
                "seasonK": p.get("seasonK"), "l3k": p.get("l3k"), "l5k": p.get("l5k"),
                "gamesStarted": p.get("gamesStarted"),
            })
        else:
            snapshot.append({
                "playerId": p.get("playerId"), "player": p.get("player"),
                "playerType": "batter", "team": p.get("team"),
                "game": p.get("game"), "gameStatus": p.get("gameStatus"), "gameTime": p.get("gameTime"),
                "pitcher": p.get("pitcher"), "hand": p.get("hand"),
                "lineupConfirmed": p.get("lineupConfirmed"), "pitcherConfirmed": p.get("pitcherConfirmed"),
                "score": p.get("score"), "hrrScore": p.get("hrrScore"), "tbScore": p.get("tbScore"),
                "hrProb": p.get("hrProb"), "hrrProb": p.get("hrrProb"), "tbProb": p.get("tbProb"),
                "tier": p.get("tier"),
                "barrel": p.get("barrel"), "ev": p.get("ev"), "hardhit": p.get("hardhit"),
                "l15Barrel": p.get("l15Barrel"), "l15Ev": p.get("l15Ev"),
                "l15Hardhit": p.get("l15Hardhit"), "l15PowerPa": p.get("l15PowerPa"),
                "iso": p.get("iso"), "pa": p.get("pa"), "slg": p.get("slg"),
                "avg": p.get("avg"), "obp": p.get("obp"),
                "phr9": p.get("phr9"), "whip": p.get("whip"),
                "phr9Season": p.get("phr9Season"), "whipSeason": p.get("whipSeason"),
                "avgmix": p.get("avgmix"), "wind": p.get("wind"), "park": p.get("park"),
                "temp": p.get("temp"), "windDesc": p.get("windDesc"),
                "l15LaunchAngle": p.get("l15LaunchAngle"),
                "l15hr": p.get("l15hr"), "l5hr": p.get("l5hr"),
                "l15hrr": p.get("l15hrr"), "l15tb": p.get("l15tb"), "l15hits": p.get("l15hits"),
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
                "oppIpPerStart": p.get("oppIpPerStart"),
                "batSide": p.get("batSide"),
                "phr9VsHand": p.get("phr9VsHand"), "phr9VsHandIp": p.get("phr9VsHandIp"),
                "pitcherHr9Percentile": p.get("pitcherHr9Percentile"),
                "recentFormPercentile": p.get("recentFormPercentile"),
            })
    path = f"history/{date_str}.json"
    with open(path, "w") as f:
        json.dump(snapshot, f)
    print(f"Wrote daily snapshot to {path} ({len(snapshot)} players)")


if __name__ == "__main__":
    main()
