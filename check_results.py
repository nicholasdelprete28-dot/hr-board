"""check_results.py - the second half of the historical accuracy tracker.

fetch_hr_data.py writes a small daily snapshot (history/{date}.json) of
each player's projected score/line every time it runs. This script reads
one of those snapshots after that day's games are final, looks up what
actually happened, and tallies how often each favorability tier (PRIME/
STRONG/IN PLAY/LONG SHOT) actually hit its line - a real, honest track
record instead of just trusting the model blindly.

Meant to run once daily, well after all games nationwide are final (e.g.
overnight) - checking a date whose games are still in progress will just
show a lot of players with no result yet, since get_actual_*() only finds
a game log entry once MLB has posted final stats for it.

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

    for p in snapshot:
        if p.get("playerType") == "pitcher":
            actual_k = get_actual_pitcher_k(p["playerId"], date_str)
            if actual_k is None:
                skipped_no_result += 1
                continue
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

    os.makedirs("history/results", exist_ok=True)
    with open(f"history/results/{date_str}.json", "w") as f:
        json.dump(results, f, indent=2)

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
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)

    return results


if __name__ == "__main__":
    if len(sys.argv) > 1:
        target_date = sys.argv[1]
    else:
        target_date = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    check_date(target_date)
