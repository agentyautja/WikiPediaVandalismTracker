"""The local dashboard: a small Flask JSON API plus the static page in /static."""

import sys
from pathlib import Path
from urllib.parse import quote

from flask import Flask, abort, jsonify, request, send_from_directory

import config
from .history import DEPTHS, SearchError
from .topics import ALL_CATEGORIES

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
            "revid", "title", "user", "timestamp", "comment", "edit_type", "badfaith", "damaging", "revert_risk",
            "content_score", "findings", "community", "status", "verdict", "confidence", "reasons", "categories",
            "check_count", "last_checked", "next_check", "stage", "source")},
        "favorite": bool(edit["favorite"]),
        "size_change": (edit["newlen"] or 0) - (edit["oldlen"] or 0),
        "diff": {"hunks": diff.get("hunks", []), "unavailable": diff.get("unavailable")},
        "links": links,
        "primary": primary,
    }


def create_app(db, monitor, wiki, profile, history):
    app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")

    @app.get("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    @app.get("/api/edits")
    def edits():
        args = request.args
        favorites = args.get("view") == "favorites"
        result = db.list_edits(
            verdicts=SHOWN_VERDICTS,
            statuses=SHOWN_STATUSES,
            source=None if favorites else "live",   # page-history finds have their own tab
            favorites=favorites,
            category=args.get("category") or None,
            query=args.get("q", "").strip(),
            sort=args.get("sort", "newest"),
            limit=min(args.get("limit", 50, type=int), 500),
            offset=args.get("offset", 0, type=int),
        )
        result["items"] = [present(e) for e in result["items"]]
        return jsonify(result)

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
        return jsonify({"search": search, "items": [present(e) for e in result["items"]], "total": result["total"]})

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
