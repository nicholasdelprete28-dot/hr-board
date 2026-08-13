"""check_results.py - the second half of the historical accuracy tracker.

fetch_hr_data.py writes a small daily snapshot (history/{date}.json) of
each player's projected score/line every time it runs. This script reads
one of those snapshots after that day's games are final, looks up what
actually happened, and tallies how often each favorability tier (PRIME/
STRONG/IN PLAY/LONG SHOT) actually hit its line - a real, honest track
record instead of just trusting the model blindly.

WHAT CHANGED (per-player outcomes file, for the frontend day-switcher):
Everything below the results/accuracy_summary.json logic already existed
and is unchanged. What's new is history/outcomes/{date}.json - previously
this script only ever kept AGGREGATE hit/total counts per tier, and threw
away the actual per-player result (get_actual_batter_line /
get_actual_pitcher_k) the moment it was used to grade a tier. There was
nowhere that recorded "did THIS specific player actually hit a HR that
day." The new outcomes file fixes that:

    {"batter": {"<playerId>": {"hr": 0, "hrr": 1, "tb": 1}, ...},
     "pitcher": {"<playerId>": {"k": 6}, ...}}

so the site's day-switcher can mark historical cards with real results,
not just show the model's original prediction with no way to check it.

Meant to run once daily, well after all games nationwide are final (e.g.
overnight) - checking a date whose games are still in progress will just
show a lot of players with no result yet, since get_actual_*() only finds
a game log entry once MLB has posted final stats for it. Note that West
Coast games can run past 1 AM Eastern, so "overnight" should mean genuinely
late (or just rely on the default "yesterday" behavior from a scheduled
run early the following morning) rather than right at midnight.

Usage:
    python3 check_results.py                # checks yesterday
    python3 check_results.py 2026-08-05      # checks a specific date
"""
import datetime
import json
import os
import sys

from fetch_hr_data import statsapi_get

# Tier boundaries - MUST be kept in sync with index.html's favTierClass().
# Duplicated here rather than shared since the frontend is JS and this is
# Python with no build step connecting them; if you retune the frontend
# thresholds, update these too or the accuracy stats will be graded
# against the wrong boundaries.
BATTER_TIERS = [("prime", 55), ("strong", 40), ("inplay", 35), ("longshot", 0)]
K_TIERS = [("prime", 51), ("strong", 45), ("inplay", 39), ("longshot", 0)]


def tier_for(score, tiers):
    if score is None:
        return None
    for name, cutoff in tiers:
        if score >= cutoff:
            return name
    return "longshot"


def get_actual_batter_line(player_id, date_str):
    """This player's actual HR/HRR/TB line for one specific date, or None
    if he didn't play (or hasn't had a final box score posted) that day."""
    try:
        data = statsapi_get(f"people/{player_id}/stats", {
            "stats": "gameLog", "group": "hitting",
            "season": date_str[:4], "sportId": 1,
        })
        for s in data.get("stats", [{}])[0].get("splits", []):
            if s.get("date") == date_str:
                stat = s.get("stat", {})
                hits = int(stat.get("hits", 0) or 0)
                doubles = int(stat.get("doubles", 0) or 0)
                triples = int(stat.get("triples", 0) or 0)
                hr = int(stat.get("homeRuns", 0) or 0)
                runs = int(stat.get("runs", 0) or 0)
                rbi = int(stat.get("rbi", 0) or 0)
                return {
                    "hr": hr,
                    "hrr": hits + runs + rbi,
                    "tb": hits + doubles + 2 * triples + 3 * hr,
                }
    except Exception:
        pass
    return None


def get_actual_pitcher_k(player_id, date_str):
    """This pitcher's actual strikeout total for one specific date, or
    None if he didn't pitch (or hasn't had a final line posted) that day."""
    try:
        data = statsapi_get(f"people/{player_id}/stats", {
            "stats": "gameLog", "group": "pitching",
            "season": date_str[:4], "sportId": 1,
        })
        for s in data.get("stats", [{}])[0].get("splits", []):
            if s.get("date") == date_str:
                return int(s.get("stat", {}).get("strikeOuts", 0) or 0)
    except Exception:
        pass
    return None


def bump(results, board, tier, hit):
    results.setdefault(board, {}).setdefault(tier, {"hit": 0, "total": 0})
    results[board][tier]["total"] += 1
    if hit:
        results[board][tier]["hit"] += 1


def fetch_league_wide_qualifiers(date_str):
    """The REAL, full-league answer to 'how many players actually cleared
    this line today, across all of MLB' - not just the ones we happened
    to be tracking. Pulls every completed game's full box score (both
    teams' entire batting lineup, not just our snapshot's players) and
    builds the true qualifying set for each board.

    This is what makes an honest "we flagged 29 of the 33 real home run
    hitters tonight" stat possible - a coverage number, not just a hit
    rate, and grounded in real box scores rather than an assumption that
    our tracked list already includes everyone.

    Returns a dict of sets: {"hr": {playerId, ...}, "hrr": {...}, "tb": {...}}
    Silently skips any game whose box score isn't available yet or errors
    out - a partial-day fetch is far better than the whole check crashing
    on one bad game.
    """
    qualifiers = {"hr": set(), "hrr": set(), "tb": set()}
    try:
        schedule = statsapi_get("schedule", {"sportId": 1, "date": date_str})
    except Exception as e:
        print(f"  Could not fetch league schedule for {date_str}: {e}")
        return qualifiers

    game_pks = []
    for date_entry in schedule.get("dates", []):
        for game in date_entry.get("games", []):
            status = game.get("status", {}).get("abstractGameState")
            if status == "Final":
                game_pks.append(game.get("gamePk"))

    for game_pk in game_pks:
        try:
            box = statsapi_get(f"game/{game_pk}/boxscore")
        except Exception as e:
            print(f"  Skipping game {game_pk} - boxscore fetch failed: {e}")
            continue

        for side in ("away", "home"):
            players = box.get("teams", {}).get(side, {}).get("players", {})
            for _, pdata in players.items():
                batting = pdata.get("stats", {}).get("batting") or {}
                if not batting:
                    continue
                player_id = pdata.get("person", {}).get("id")
                if player_id is None:
                    continue
                hits = int(batting.get("hits", 0) or 0)
                doubles = int(batting.get("doubles", 0) or 0)
                triples = int(batting.get("triples", 0) or 0)
                hr = int(batting.get("homeRuns", 0) or 0)
                runs = int(batting.get("runs", 0) or 0)
                rbi = int(batting.get("rbi", 0) or 0)
                total_bases = hits + doubles + 2 * triples + 3 * hr

                if hr >= 1:
                    qualifiers["hr"].add(player_id)
                if (hits + runs + rbi) >= 2:
                    qualifiers["hrr"].add(player_id)
                if total_bases >= 2:
                    qualifiers["tb"].add(player_id)

    return qualifiers


def check_date(date_str):
    snapshot_path = f"history/{date_str}.json"
    if not os.path.exists(snapshot_path):
        print(f"No snapshot found for {date_str} at {snapshot_path} - nothing to check.")
        return None

    with open(snapshot_path) as f:
        snapshot = json.load(f)

    results = {}
    checked = 0
    skipped_no_result = 0

    # NEW: per-player actual outcomes, keyed by playerId (as a string, to
    # match JSON object key requirements and the frontend's lookup) - this
    # is what history/outcomes/{date}.json gets built from below. Separate
    # from `results` (which only ever holds tier-level hit/total counts) -
    # this keeps the individual real result for every player actually
    # graded, batters and pitchers each in their own bucket.
    player_outcomes = {"batter": {}, "pitcher": {}}

    for p in snapshot:
        if p.get("playerType") == "pitcher":
            actual_k = get_actual_pitcher_k(p["playerId"], date_str)
            if actual_k is None:
                skipped_no_result += 1
                continue
            player_outcomes["pitcher"][str(p["playerId"])] = {"k": actual_k}
            if p.get("kScore") is None or p.get("kLine") is None:
                continue
            tier = tier_for(p["kScore"], K_TIERS)
            bump(results, "k", tier, actual_k > p["kLine"])
            checked += 1
        else:
            actual = get_actual_batter_line(p["playerId"], date_str)
            if actual is None:
                skipped_no_result += 1
                continue
            player_outcomes["batter"][str(p["playerId"])] = {
                "hr": actual["hr"], "hrr": actual["hrr"], "tb": actual["tb"],
            }
            for board, score_key, threshold in [
                ("hr", "score", 1), ("hrr", "hrrScore", 2), ("tb", "tbScore", 2)
            ]:
                score = p.get(score_key)
                if score is None:
                    continue
                tier = tier_for(score, BATTER_TIERS)
                bump(results, board, tier, actual[board] >= threshold)
            checked += 1

    print(f"Checked {date_str}: {checked} players graded, {skipped_no_result} skipped (no game/result found).")

    # NEW: write the per-player outcomes file. Written regardless of
    # whether any tier-grading happened above (e.g. even if a player was
    # missing score/kScore/kLine, we still record his real result here if
    # we found one) - this file is meant to be the complete real record of
    # what happened, independent of the model's own tier cutoffs.
    os.makedirs("history/outcomes", exist_ok=True)
    with open(f"history/outcomes/{date_str}.json", "w") as f:
        json.dump(player_outcomes, f, indent=2)
    print(f"Wrote per-player outcomes to history/outcomes/{date_str}.json "
          f"({len(player_outcomes['batter'])} batters, {len(player_outcomes['pitcher'])} pitchers)")

    # Real league-wide coverage: of everyone who ACTUALLY hit the mark
    # today, across all of MLB - not just our tracked list - how many
    # were players we had tracked at all that day? This is a genuinely
    # different (and more honest) stat than the tier hit-rate above: it
    # answers "did we even have the guy on our radar," not "did our
    # confidence ranking work out."
    print(f"Fetching real league-wide results for {date_str} (coverage check)...")
    league_qualifiers = fetch_league_wide_qualifiers(date_str)
    tracked_ids = {
        board: set() for board in ("hr", "hrr", "tb")
    }
    for p in snapshot:
        if p.get("playerType") == "pitcher":
            continue
        pid = p.get("playerId")
        if pid is None:
            continue
        for board, score_key in [("hr", "score"), ("hrr", "hrrScore"), ("tb", "tbScore")]:
            if p.get(score_key) is not None:
                tracked_ids[board].add(pid)

    coverage = {}
    for board in ("hr", "hrr", "tb"):
        real_set = league_qualifiers[board]
        matched = real_set & tracked_ids[board]
        coverage[board] = {"real_total": len(real_set), "matched": len(matched)}
        print(f"  {board.upper()} coverage: {len(matched)} of {len(real_set)} real qualifiers were on our board")

    os.makedirs("history/results", exist_ok=True)
    results_with_coverage = dict(results)
    results_with_coverage["_coverage"] = coverage
    with open(f"history/results/{date_str}.json", "w") as f:
        json.dump(results_with_coverage, f, indent=2)

    # Roll this date's results into a running all-time summary too, so the
    # frontend can show "PRIME tier: 61% over the last 30 days" without
    # needing to fetch and combine every individual day's file itself.
    summary_path = "history/accuracy_summary.json"
    summary = {}
    if os.path.exists(summary_path):
        with open(summary_path) as f:
            summary = json.load(f)
    for board, tiers in results.items():
        summary.setdefault(board, {})
        for tier, r in tiers.items():
            summary[board].setdefault(tier, {"hit": 0, "total": 0})
            summary[board][tier]["hit"] += r["hit"]
            summary[board][tier]["total"] += r["total"]

    # Same running-total treatment for coverage, kept in its own section
    # so it doesn't get confused with the tier hit-rate data above.
    summary.setdefault("_coverage", {})
    for board, c in coverage.items():
        summary["_coverage"].setdefault(board, {"real_total": 0, "matched": 0})
        summary["_coverage"][board]["real_total"] += c["real_total"]
        summary["_coverage"][board]["matched"] += c["matched"]
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)

    return results


if __name__ == "__main__":
    if len(sys.argv) > 1:
        target_date = sys.argv[1]
    else:
        target_date = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    check_date(target_date)
