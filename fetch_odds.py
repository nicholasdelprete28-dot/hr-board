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

Note: HRR (Hits+Runs+RBI combined, standard line 1.5) IS a real, commonly-
offered prop at every major book - see MARKET_TO_FIELD below for the caveat
on its exact API market key, which hasn't been verified against a live
response yet.
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

MARKETS = ("batter_home_runs,batter_home_runs_alternate,"
           "batter_hits,batter_hits_alternate,"
           "batter_total_bases,batter_total_bases_alternate,"
           "batter_hits_runs_rbis,batter_hits_runs_rbis_alternate,"
           "pitcher_strikeouts,pitcher_strikeouts_alternate")

# Maps our board's field names to the API's market keys, so the merge
# step below can write odds onto the right spot on each player row. Both
# the standard AND "_alternate" (milestone/multi-line) markets map to the
# same field, since alternates are just more lines for the same stat -
# confirmed real via The Odds API's own docs ("Milestone (X+) markets are
# captured using _alternate player market keys, e.g.
# batter_home_runs_alternate"). batter_hits_runs_rbis (HRR) is still a
# best-guess key per the note above - unverified against a live response.
MARKET_TO_FIELD = {
    "batter_home_runs": "hr", "batter_home_runs_alternate": "hr",
    "batter_hits": "hits", "batter_hits_alternate": "hits",
    "batter_total_bases": "tb", "batter_total_bases_alternate": "tb",
    "batter_hits_runs_rbis": "hrr", "batter_hits_runs_rbis_alternate": "hrr",
    "pitcher_strikeouts": "k", "pitcher_strikeouts_alternate": "k",
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
    """Returns { normalized_player_name: { field: [ {book, line, price}, ... ] } }
    Deliberately a LIST per field, not a dict keyed by book - once
    alternate lines are in the mix, a single book can offer several
    different lines for the same player/stat simultaneously (e.g.
    DraftKings might post 2.5, 3.5, AND 4.5 K's all as separate
    alternate-line outcomes), so keying by book alone would silently
    overwrite and lose all but the last one."""
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
                market_key = market.get("key", "")
                field = MARKET_TO_FIELD.get(market_key)
                if not field:
                    continue
                is_alt = market_key.endswith("_alternate")
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
                    odds_by_player.setdefault(key, {}).setdefault(field, [])
                    entries = odds_by_player[key][field]
                    # Skip an exact duplicate (same book, same line) that
                    # can happen if the standard and _alternate markets
                    # both happen to report that book's primary line.
                    if any(e["book"] == book_key and e["line"] == line for e in entries):
                        continue
                    entries.append({
                        # "alt" marks whether this line came from the
                        # standard market or the _alternate (milestone)
                        # market - the frontend card only shows non-alt
                        # entries (one line per book, not every alternate
                        # cluttering the card); the dropdown shows all of
                        # them regardless of this flag.
                        "book": book_key, "line": line, "price": price,
                        "alt": is_alt, "displayName": player_name,
                    })
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
