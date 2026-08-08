"""fetch_odds.py - pulls real sportsbook lines for HR, Hits, Total Bases,
and Pitcher Strikeouts props from The Odds API, and matches them against
today's players.json so the frontend can show "our line vs. the real book
line" side by side.

Kept as a SEPARATE script from fetch_hr_data.py on purpose: odds are
fetched per-game (not in one bulk call), so this costs real API credits
every run. Run this on its own, less-frequent schedule (e.g. once daily)
rather than every time fetch_hr_data.py runs, to stay within a free-tier
budget.

Requires an ODDS_API_KEY environment variable (from the-odds-api.com's
free tier - see the setup notes in the conversation this was built in).

Note: there's no real "Hits+Runs+RBI combined" market at any sportsbook -
that's our own custom stat - so HRR has no real-odds comparison, only
HR/Hits/TB/K do.
"""
import json
import os
import sys
import unicodedata

import requests

ODDS_API_KEY = os.environ.get("ODDS_API_KEY")
BASE_URL = "https://api.the-odds-api.com/v4/sports/baseball_mlb"

# The four sportsbooks requested - restricting to just these both keeps
# the response smaller and matches exactly what should show on the card.
BOOKMAKERS = "draftkings,fanduel,betmgm,hardrockbet"

MARKETS = "batter_home_runs,batter_hits,batter_total_bases,pitcher_strikeouts"

# Maps our board's field names to the API's market keys, so the merge
# step below can write odds onto the right spot on each player row.
MARKET_TO_FIELD = {
    "batter_home_runs": "hr",
    "batter_hits": "hits",
    "batter_total_bases": "tb",
    "pitcher_strikeouts": "k",
}


def normalize_name(name):
    """Lowercase, strip accents/periods, so 'José Ramírez' and 'Jose
    Ramirez' (or 'J. Ramirez') match reliably against each other - the
    odds API and MLB's own API don't always format names identically."""
    if not name:
        return ""
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_name = "".join(c for c in nfkd if not unicodedata.combining(c))
    return ascii_name.lower().replace(".", "").strip()


def get_todays_events():
    if not ODDS_API_KEY:
        print("ERROR: ODDS_API_KEY environment variable not set - see fetch_odds.py's docstring.")
        return []
    resp = requests.get(f"{BASE_URL}/events", params={"apiKey": ODDS_API_KEY}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_event_odds(event_id):
    resp = requests.get(
        f"{BASE_URL}/events/{event_id}/odds",
        params={
            "apiKey": ODDS_API_KEY,
            "regions": "us",
            "markets": MARKETS,
            "bookmakers": BOOKMAKERS,
            "oddsFormat": "american",
        },
        timeout=15,
    )
    if resp.status_code != 200:
        print(f"  odds request failed for event {event_id}: {resp.status_code} {resp.text[:200]}")
        return None
    return resp.json()


def collect_odds():
    """Returns { normalized_player_name: { field: { book_key: {line, price} } } }"""
    events = get_todays_events()
    print(f"Found {len(events)} MLB events today.")
    odds_by_player = {}

    for event in events:
        event_id = event.get("id")
        matchup = f"{event.get('away_team')} @ {event.get('home_team')}"
        data = get_event_odds(event_id)
        if not data:
            continue
        for bookmaker in data.get("bookmakers", []):
            book_key = bookmaker.get("key")
            for market in bookmaker.get("markets", []):
                field = MARKET_TO_FIELD.get(market.get("key"))
                if not field:
                    continue
                for outcome in market.get("outcomes", []):
                    # Player props only have "Over"/"Under" outcomes - we
                    # only care about the Over side's line and price.
                    if outcome.get("name") != "Over":
                        continue
                    player_name = outcome.get("description")
                    line = outcome.get("point")
                    price = outcome.get("price")
                    if not player_name or line is None:
                        continue
                    key = normalize_name(player_name)
                    odds_by_player.setdefault(key, {}).setdefault(field, {})
                    odds_by_player[key][field][book_key] = {
                        "line": line, "price": price, "displayName": player_name,
                    }
        print(f"  {matchup}: processed")

    return odds_by_player


def merge_into_players(odds_by_player):
    if not os.path.exists("players.json"):
        print("players.json not found - run fetch_hr_data.py first.")
        return
    with open("players.json") as f:
        players = json.load(f)

    matched = 0
    for p in players:
        key = normalize_name(p.get("player", ""))
        book_data = odds_by_player.get(key)
        if not book_data:
            p["bookOdds"] = None
            continue
        p["bookOdds"] = book_data
        matched += 1

    with open("players.json", "w") as f:
        json.dump(players, f, indent=2)

    print(f"Matched real sportsbook odds for {matched} of {len(players)} players.")


if __name__ == "__main__":
    odds = collect_odds()
    merge_into_players(odds)
