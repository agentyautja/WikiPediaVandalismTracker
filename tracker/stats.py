"""Numbers for the Stats tab: when vandalism happens, how fast it's reverted, by whom, and who does it."""

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from .community import ANTIVANDAL_BOTS

# Time-to-revert histogram buckets: (label, upper bound in seconds)
REVERT_BUCKETS = [("< 1 min", 60), ("1–5 min", 300), ("5–15 min", 900), ("15–60 min", 3600),
                  ("1–6 h", 6 * 3600), ("> 6 h", float("inf"))]


def _parse(timestamp):
    return datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def revert_seconds(edit):
    """Seconds between the edit and the revert that undid it, or None if unknown."""
    revert = (edit.get("community") or {}).get("revert") or {}
    if not revert.get("timestamp") or not edit.get("timestamp"):
        return None
    seconds = (_parse(revert["timestamp"]) - _parse(edit["timestamp"])).total_seconds()
    return seconds if seconds >= 0 else None


def _median(values):
    values = sorted(values)
    if not values:
        return None
    middle = len(values) // 2
    return values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) / 2


def build(rows, days, tz_minutes, vandal_counts):
    """Everything the Stats tab shows. `rows` = confirmed, cleaned-up live edits in the period."""
    offset = timedelta(minutes=tz_minutes)
    now_local = datetime.now(timezone.utc) + offset
    per_hour = [0] * 24
    per_day = Counter()
    pages, categories, reverters = Counter(), Counter(), Counter()
    who = Counter()
    timings, survivors = [], []
    offenders = defaultdict(lambda: {"edits": 0, "pages": Counter(), "last": "", "block": None})

    for edit in rows:
        local = _parse(edit["timestamp"]) + offset
        per_hour[local.hour] += 1
        per_day[local.date().isoformat()] += 1
        pages[(edit["pageid"], edit["title"])] += 1
        for name in edit.get("categories") or []:
            categories[name] += 1

        community = edit.get("community") or {}
        revert = community.get("revert") or {}
        if revert.get("user"):
            reverters[revert["user"]] += 1
            if ANTIVANDAL_BOTS.match(revert["user"]):
                who["ClueBot NG and other bots"] += 1
            elif revert.get("kind") == "antivandal":
                who["People with anti-vandalism tools"] += 1
            else:
                who["People reverting by hand"] += 1
        elif community.get("hidden") or community.get("deleted"):
            who["Admins (hidden or deleted)"] += 1
        else:
            who["Removed by a later edit"] += 1

        seconds = revert_seconds(edit)
        if seconds is not None:
            timings.append(seconds)
            survivors.append((seconds, edit))

        user = edit.get("user")
        if user and user != "(hidden)":
            info = offenders[user]
            info["edits"] += 1
            info["pages"][edit["title"]] += 1
            info["last"] = max(info["last"], edit["timestamp"])
            if community.get("block") and not info["block"]:
                info["block"] = community["block"].get("reason") or "blocked"

    day_list = [(now_local.date() - timedelta(days=i)).isoformat() for i in range(min(days, 14) - 1, -1, -1)]
    buckets, lower = [], 0
    for label, upper in REVERT_BUCKETS:
        buckets.append({"label": label, "count": sum(lower <= s < upper for s in timings),
                        "range": f"{lower:g}-{'' if upper == float('inf') else f'{upper:g}'}"})
        lower = upper
    survivors.sort(key=lambda item: -item[0])
    repeat = [{"user": user, "edits": info["edits"], "pages": [t for t, _ in info["pages"].most_common(3)],
               "page_count": len(info["pages"]), "last": info["last"], "block": info["block"],
               "all_time": vandal_counts.get(user, info["edits"])}
              for user, info in offenders.items() if info["edits"] >= 2]
    repeat.sort(key=lambda o: o["last"], reverse=True)   # most edits first, then most recent
    repeat.sort(key=lambda o: -o["edits"])
    total_reverts = sum(who.values())
    bots = who["ClueBot NG and other bots"]

    return {
        "days": days,
        "total": len(rows),
        "per_hour": per_hour,
        "per_day": [{"day": d, "count": per_day.get(d, 0)} for d in day_list],
        "top_pages": [{"pageid": p, "title": t, "count": n} for (p, t), n in pages.most_common(10)],
        "top_categories": [{"name": name, "count": n} for name, n in categories.most_common(10)],
        "top_reverters": [{"user": u, "count": n} for u, n in reverters.most_common(8)],
        "cleaned_by": [{"name": name, "count": n} for name, n in who.most_common()],
        "bot_share": bots / total_reverts if total_reverts else None,
        "revert": {
            "known": len(timings),
            "median": _median(timings),
            "fastest": min(timings) if timings else None,
            "within_minute": sum(s < 60 for s in timings) / len(timings) if timings else None,
            "buckets": buckets,
            "longest": [{"revid": e["revid"], "title": e["title"], "user": e["user"], "seconds": s}
                        for s, e in survivors[:5]],
        },
        "repeat_offenders": repeat[:25],
        "repeat_offender_count": len(repeat),
    }
