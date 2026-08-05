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
from datetime import datetime
from zoneinfo import ZoneInfo
import requests

YEAR = 2026
TODAY = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")

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


def get_todays_games():
    """Today's schedule with probable starting pitchers."""
    data = statsapi_get("schedule", {
        "sportId": 1,
        "date": TODAY,
        "hydrate": "probablePitcher,linescore"
    })

    games = []

    for date_block in data.get("dates", []):
        for g in date_block.get("games", []):
            games.append({
                "game_pk": g["gamePk"],
                "home_team": g["teams"]["home"]["team"].get("name"),
                "away_team": g["teams"]["away"]["team"].get("name"),
                "home_team_id": g["teams"]["home"]["team"].get("id"),
                "away_team_id": g["teams"]["away"]["team"].get("id"),
                "home_pitcher": g["teams"]["home"].get("probablePitcher", {}),
                "away_pitcher": g["teams"]["away"].get("probablePitcher", {}),
            })

    return games


def get_lineup(game_pk, side):
    """Gets batting order from MLB live feed/boxscore."""
    try:
        url = f"https://statsapi.mlb.com/api/v1.1/game/{game_pk}/feed/live"
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        team_box = data["liveData"]["boxscore"]["teams"][side]

        batters = team_box.get("batters", [])
        players = team_box.get("players", {})

        lineup = {}

        for player_id in batters:
            player_key = f"ID{player_id}"
            player = players.get(player_key, {})

            batting_order = player.get("battingOrder")

            if batting_order:
                lineup[player_id] = int(batting_order)

        return lineup

    except Exception as e:
        print("Lineup error:", e)
        return {}

def get_team_roster(team_id):
    """Fallback hitters when official lineup isn't posted."""
    try:
        data = statsapi_get(f"teams/{team_id}/roster")

        hitters = {}

        for player in data.get("roster", []):
            person = player.get("person", {})
            position = player.get("position", {}).get("code")

            # exclude pitchers
            if position != "1":
                hitters[person["id"]] = 5

        return hitters

    except Exception as e:
        print("Roster error:", e)
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
    """ISO (computed from SLG-AVG) for every batter this season."""
    data = statsapi_get("stats", {
        "stats": "season", "group": "hitting", "season": YEAR, "sportId": 1, "limit": 1500
    })
    out = {}
    for split in data.get("stats", [{}])[0].get("splits", []):
        pid = split.get("player", {}).get("id")
        name = split.get("player", {}).get("fullName")
        stat = split.get("stat", {})
        avg = stat.get("avg")
        slg = stat.get("slg")
        if pid and avg is not None and slg is not None:
            try:
                iso = float(slg) - float(avg)
            except ValueError:
                iso = None
            out[pid] = {"name": name, "iso": iso}
    return out


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


def get_l15_hr(batter_id):
    """Home runs over the batter's last 15 games played."""
    try:
        data = statsapi_get(f"people/{batter_id}/stats", {
            "stats": "gameLog", "group": "hitting", "season": YEAR
        })
        splits = data.get("stats", [{}])[0].get("splits", [])
        last15 = splits[-15:] if len(splits) >= 15 else splits
        return sum(int(s["stat"].get("homeRuns", 0)) for s in last15)
    except Exception:
        return None


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
    url = (f"https://baseballsavant.mlb.com/leaderboard/statcast"
           f"?type=batter&year={YEAR}&position=&team=&min=q&csv=true")
    resp = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    reader = csv.DictReader(io.StringIO(resp.text))
    out = {}
    for row in reader:
        name = row.get("player_name") or row.get("last_name, first_name", "")
        try:
            out[name] = {
                "ev": float(row.get("avg_hit_speed") or 0),
                "barrel": float(row.get("brl_percent") or 0) / 100,
                "hardhit": float(row.get("hard_hit_percent") or 0) / 100,
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
    if barrel is None or ev is None:
        return 1.0
    if barrel >= 0.12:
        if ev >= 92:
            return 1.0
        elif ev >= 90:
            return 0.7
        else:
            return 0.5
    return 1.0


def avgmix_confidence_blend(avgmix):
    if avgmix is None:
        return 0.24
    if avgmix <= 0.10 or avgmix >= 0.40:
        return avgmix * 0.5 + 0.24 * 0.5
    return avgmix


def compute_score(p):
    barrel = p["barrel"] or 0
    ev = p["ev"] or 85
    iso = p["iso"] or 0
    hardhit = p["hardhit"] or 0.30
    phr9 = p["phr9"] if p["phr9"] is not None else 1.2
    whip = p["whip"] if p["whip"] is not None else 1.30
    avgmix = avgmix_confidence_blend(p["avgmix"])
    wind = p["wind"] or 0
    park = p["park"] or 0
    l15hr = p["l15hr"] if p["l15hr"] is not None else 0
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
    matchup = (pitcher_quality + avgmix_s + park_s + wind_s * 0.5) / 3.5

    recent = clamp01(l15hr / 6)
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


def main():
    print("Fetching today's schedule and probable pitchers...")
    games = get_todays_games()
    print(f"MLB date requested: {TODAY}")
    print(f"  {len(games)} games today")

    print("Fetching season pitching stats (WHIP, HR/9)...")
    pitching_stats = get_season_pitching_stats()

    print("Fetching season batting stats (ISO)...")
    batting_stats = get_season_batting_stats()

    print("Fetching Statcast batter data (barrel%, EV, hard-hit%)...")
    statcast = fetch_batter_statcast()

    players = []

    for g in games:
        for side, opp_side in [("home", "away"), ("away", "home")]:
            team = g[f"{side}_team"]
            opp_pitcher = g[f"{opp_side}_pitcher"]

            if not opp_pitcher:
                continue

            pitcher_id, pitcher_hand = get_pitcher_hand_and_id(opp_pitcher)

            pitcher_stat = pitching_stats.get(
                pitcher_id,
                {"whip": 1.30, "hr9": 1.20}
            )

            park = PARKS.get(
                g["home_team"],
                {"factor": 0, "lat": None, "lon": None}
            )

            wind_speed, wind_dir = None, None

            if park["lat"] is not None:
                wind_speed, wind_dir = get_wind(
                    park["lat"],
                    park["lon"]
                )

            wind_score = wind_park_factor(
                wind_speed,
                wind_dir
            )

            # Try official lineup first
            lineup = get_lineup(g["game_pk"], side)

            # If lineup isn't released, use roster fallback
            if not lineup:
                print(team, "using roster fallback")

                team_id = g[f"{side}_team_id"]
                lineup = get_team_roster(team_id)

            else:
                print(team, "using confirmed lineup")

            for batter_id, order_pos in lineup.items():

                bstats = batting_stats.get(batter_id, {})
                name = bstats.get("name", "")

                sc = statcast.get(name, {})

                platoon_avg = get_platoon_split(
                    batter_id,
                    pitcher_hand
                )

                player_row = {
                    "player": name,
                    "team": team,
                    "pitcher": opp_pitcher.get("fullName", ""),
                    "hand": pitcher_hand,
                    "game": f"{g['away_team']} @ {g['home_team']}",

                    "barrel": sc.get("barrel"),
                    "ev": sc.get("ev"),
                    "hardhit": sc.get("hardhit"),

                    "iso": bstats.get("iso"),

                    "phr9": pitcher_stat["hr9"],
                    "whip": pitcher_stat["whip"],

                    "avgmix": platoon_avg,

                    "wind": wind_score,
                    "park": park["factor"],

                    "l15hr": get_l15_hr(batter_id),

                    # confirmed lineup gets real spot
                    # fallback roster gets neutral spot
                    "lbonus": (
                        max(1, 9 - order_pos)
                        if order_pos
                        else 3
                    ),

                    "crush": 1 if (platoon_avg or 0) >= 0.280 else 0,
                    "split": 1 if (platoon_avg or 0) >= 0.260 else 0,

                    "hrprob": None,
                }

                player_row.update(
                    compute_score(player_row)
                )

                players.append(player_row)

                time.sleep(0.05)

    players.sort(
        key=lambda p: -p["score"]
    )

    with open("players.json", "w") as f:
        json.dump(
            players,
            f,
            indent=2
        )

    print(f"Wrote players.json with {len(players)} players.")


if __name__ == "__main__":
    main()
