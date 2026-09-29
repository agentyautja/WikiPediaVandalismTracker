"""The local dashboard: a small Flask JSON API plus the static page in /static."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from flask import Flask, abort, jsonify, request, send_from_directory

import config
from . import stats
from .history import DEPTHS, SearchError, parse_page
from .topics import ALL_CATEGORIES
from .wiki import ApiError, WikiClient

# Inside the .exe, bundled files are unpacked to sys._MEIPASS.
STATIC_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent)) / "static"
# The dashboard only shows confirmed vandalism that Wikipedia has already cleaned up. The other
# candidates (likely, under review, still live) keep being re-checked in the background and show
# up here once they are confirmed and cleaned up.
SHOWN_VERDICTS = ["confirmed"]
SHOWN_STATUSES = ["reverted", "hidden", "deleted"]


def _links(edit):
    base = f"https://{config.WIKI_DOMAIN}"
    title = quote(edit["title"].replace(" ", "_"), safe="/:(),'!*")
    user = quote((edit["user"] or "").replace(" ", "_"), safe="")
    revid, parent = edit["revid"], edit["parentid"]
    return {
        "page": f"{base}/wiki/{title}",
        "vandalized": f"{base}/w/index.php?oldid={revid}",   # the page exactly as the vandal left it
        "diff": f"{base}/w/index.php?diff={revid}&oldid={parent}" if parent else f"{base}/w/index.php?oldid={revid}",
        "history": f"{base}/w/index.php?title={title}&action=history",
        "user": f"{base}/wiki/Special:Contributions/{user}",
        "deletion_log": f"{base}/w/index.php?title=Special:Log&type=delete&page={title}",
    }


def present(edit):
    """An edit as the web page wants it, including the right links for its status."""
    links = _links(edit)
    status = edit["status"]
    if status == "deleted":
        primary = {"label": "Page was deleted — see the log", "url": links["deletion_log"]}
    elif status == "hidden":
        primary = {"label": "Vandalized version (hidden by admins)", "url": links["vandalized"]}
    else:
        primary = {"label": "See the vandalized version", "url": links["vandalized"]}
    diff = edit["diff"] or {}
    return {
        **{key: edit[key] for key in (
            "revid", "pageid", "title", "user", "timestamp", "comment", "edit_type", "badfaith", "damaging", "revert_risk",
            "content_score", "findings", "community", "status", "verdict", "confidence", "reasons", "categories",
            "check_count", "last_checked", "next_check", "stage", "source")},
        "favorite": bool(edit["favorite"]),
        "revert_seconds": stats.revert_seconds(edit),
        "size_change": (edit["newlen"] or 0) - (edit["oldlen"] or 0),
        "diff": {"hunks": diff.get("hunks", []), "unavailable": diff.get("unavailable")},
        "links": links,
        "primary": primary,
    }


def create_app(db, monitor, wiki, profile, history):
    app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")
    # For looking up pages added to the watchlist (web requests run in their own threads).
    lookup = WikiClient(config.WIKI_DOMAIN, config.WIKI_LANG, config.CONTACT, profile["api_per_minute"],
                        share_limits_with=wiki)

    def with_items(result):
        """Present the edits, plus how many vandal edits each of their editors has (repeat offenders)."""
        result["items"] = [present(e) for e in result["items"]]
        counts = db.vandal_edit_counts([e["user"] for e in result["items"]], SHOWN_VERDICTS, SHOWN_STATUSES)
        result["offenders"] = {user: n for user, n in counts.items() if n >= 2}
        return result

    @app.get("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    def revert_range(text):
        """ "60-300" -> (60.0, 300.0), "21600-" -> (21600.0, None): a time-to-revert range in seconds."""
        low, _, high = (text or "").partition("-")
        try:
            return (float(low) if low else None), (float(high) if high else None)
        except ValueError:
            return None, None

    @app.get("/api/edits")
    def edits():
        args = request.args
        view = args.get("view", "all")
        user = args.get("user") or None
        revert_min, revert_max = revert_range(args.get("revert"))
        result = db.list_edits(
            verdicts=SHOWN_VERDICTS,
            statuses=SHOWN_STATUSES,
            # The live feed leaves out page-history finds (they have their own tab), except when
            # looking at one editor or at favourites / the watchlist.
            source="live" if view == "all" and not user else None,
            favorites=view == "favorites",
            watched=view == "watchlist",
            user=user,
            revert_min=revert_min,
            revert_max=revert_max,
            category=args.get("category") or None,
            query=args.get("q", "").strip(),
            sort=args.get("sort", "newest"),
            limit=min(args.get("limit", 50, type=int), 500),
            offset=args.get("offset", 0, type=int),
        )
        return jsonify(with_items(result))

    @app.get("/api/stats")
    def stats_page():
        days = min(max(request.args.get("days", 7, type=int), 1), config.KEEP_DAYS)
        tz_minutes = request.args.get("tz", 0, type=int)   # the browser's offset from UTC
        since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = db.shown_edits_since(since, SHOWN_VERDICTS, SHOWN_STATUSES)
        counts = db.vandal_edit_counts([r["user"] for r in rows], SHOWN_VERDICTS, SHOWN_STATUSES)
        return jsonify(stats.build(rows, days, tz_minutes, counts))

    @app.get("/api/watchlist")
    def watchlist():
        return jsonify({"pages": db.watchlist()})

    @app.post("/api/watchlist")
    def watch_page():
        body = request.get_json(silent=True) or {}
        try:
            if body.get("pageid") and body.get("title"):   # from a card: already known
                page = {"pageid": int(body["pageid"]), "title": str(body["title"])}
            else:
                kind, value = parse_page(str(body.get("page", "")))
                page = lookup.page_info(**{kind: value})
                if page is None:
                    return jsonify({"error": f"There's no page called “{value}” on {config.WIKI_DOMAIN}."}), 400
        except SearchError as exc:
            return jsonify({"error": str(exc)}), 400
        except ApiError as exc:
            return jsonify({"error": f"Couldn't reach Wikipedia: {exc}"}), 502
        db.watch(page["pageid"], page["title"])
        return jsonify({"pageid": page["pageid"], "title": page["title"]})

    @app.delete("/api/watchlist/<int:pageid>")
    def unwatch_page(pageid):
        if not db.unwatch(pageid):
            abort(404)
        return jsonify({"pageid": pageid, "watched": False})

    @app.get("/api/status")
    def status():
        return jsonify({
            "monitor": monitor.status,
            "stats": db.stats(SHOWN_VERDICTS, SHOWN_STATUSES),
            "queue": {"analysing": db.count_stage("new"), "rechecking": db.count_stage("tracking")},
            "slow_mode": not config.CONTACT.strip(),
            "settings_where": config.SETTINGS_WHERE,
            "api_budget": profile["api_per_minute"],
            "api_used": wiki.api_limiter.used_last_minute(),
            "threshold": config.BADFAITH_THRESHOLD,
            "recheck_minutes": config.RECHECK_AT_MINUTES,
            "categories": ALL_CATEGORIES,
            "wiki": config.WIKI_DOMAIN,
            "watched_pageids": [p["pageid"] for p in db.watchlist()],
            # newest live finds on watched pages, so the page can notify about new ones
            "watch_latest": db.latest_watched_vandalism(10, SHOWN_VERDICTS, SHOWN_STATUSES),
        })

    @app.post("/api/history")
    def start_history_search():
        body = request.get_json(silent=True) or {}
        try:
            search_id = history.request(str(body.get("page", "")), int(body.get("depth") or DEPTHS[0]))
        except SearchError as exc:
            return jsonify({"error": str(exc)}), 400
        except (TypeError, ValueError):
            return jsonify({"error": "That's not a valid number of edits."}), 400
        return jsonify({"id": search_id})

    @app.get("/api/history")
    def history_searches():
        return jsonify({"searches": db.recent_searches(10), "depths": DEPTHS})

    @app.get("/api/history/<int:search_id>")
    def history_search(search_id):
        search = db.get_search(search_id)
        if search is None:
            abort(404)
        result = {"items": [], "total": 0}
        if search["pageid"]:   # every confirmed, cleaned-up vandal edit we know on this page
            result = db.list_edits(verdicts=SHOWN_VERDICTS, statuses=SHOWN_STATUSES, pageid=search["pageid"],
                                   limit=min(request.args.get("limit", 50, type=int), 500))
        return jsonify({"search": search, **with_items(result)})

    @app.post("/api/edits/<int:revid>/favorite")
    def favorite(revid):
        wanted = bool((request.get_json(silent=True) or {}).get("favorite", True))
        if not db.set_favorite(revid, wanted):
            abort(404)
        return jsonify({"revid": revid, "favorite": wanted})

    @app.post("/api/edits/<int:revid>/recheck")
    def recheck(revid):
        if not db.request_recheck(revid):
            abort(404)
        monitor.request_recheck()
        return jsonify({"revid": revid, "queued": True})

    return app
