"""SQLite storage for tracked edits, favourites and the tracker's own state."""

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS edits (
    revid         INTEGER PRIMARY KEY,
    parentid      INTEGER,
    pageid        INTEGER,
    title         TEXT,
    user          TEXT,
    timestamp     TEXT,             -- when the edit was made (UTC, ISO 8601)
    comment       TEXT,
    edit_type     TEXT,             -- 'edit' or 'new' (page creation)
    oldlen        INTEGER,
    newlen        INTEGER,
    tags          TEXT,             -- JSON list
    badfaith      REAL,             -- check 1: ORES P(bad faith)
    damaging      REAL,             -- ORES P(damaging)
    draft_scores  TEXT,             -- JSON, ORES new-page model (page creations only)
    revert_risk   REAL,             -- Lift Wing revert-risk model
    parent_sha1   TEXT,
    diff          TEXT,             -- JSON: hunks + samples for display / live checks
    findings      TEXT,             -- JSON list of content findings
    content_score REAL,
    community     TEXT,             -- JSON: revert / block / hidden / deleted info
    status        TEXT DEFAULT 'unknown',   -- live, reverted, edited, hidden, deleted, unknown
    verdict       TEXT DEFAULT 'pending',   -- pending, likely, confirmed, rejected
    confidence    REAL DEFAULT 0,
    reasons       TEXT,             -- JSON list of human-readable reasons
    categories    TEXT,             -- JSON list of broad categories
    favorite      INTEGER DEFAULT 0,
    favorited_at  TEXT,
    stage         TEXT DEFAULT 'new',       -- new (needs analysis), tracking, final
    next_check    TEXT,
    last_checked  TEXT,
    check_count   INTEGER DEFAULT 0,
    first_seen    TEXT,
    source        TEXT DEFAULT 'live'       -- live (RecentChanges) or history (found by a page-history search)
);
CREATE INDEX IF NOT EXISTS edits_by_time ON edits (timestamp);
CREATE INDEX IF NOT EXISTS edits_by_stage ON edits (stage, next_check);
CREATE INDEX IF NOT EXISTS edits_by_page ON edits (pageid);

CREATE TABLE IF NOT EXISTS reverts (          -- revert edits seen in RecentChanges, on pages we track
    revid     INTEGER PRIMARY KEY,
    pageid    INTEGER,
    user      TEXT,
    comment   TEXT,
    tags      TEXT,
    sha1      TEXT,
    timestamp TEXT
);
CREATE INDEX IF NOT EXISTS reverts_by_page ON reverts (pageid, revid);

CREATE TABLE IF NOT EXISTS page_topics (
    title      TEXT PRIMARY KEY,
    categories TEXT,
    topics     TEXT,
    fetched_at TEXT
);

CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS watchlist (         -- pages the user wants to keep an eye on
    pageid   INTEGER PRIMARY KEY,
    title    TEXT,
    added_at TEXT
);

CREATE TABLE IF NOT EXISTS history_searches (  -- "search this page's history" requests
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    query       TEXT,             -- what was typed: a title or a link
    title       TEXT,
    pageid      INTEGER,
    depth       INTEGER,          -- how many of the latest edits to look at
    status      TEXT,             -- queued, running, done, error
    phase       TEXT,
    revisions   INTEGER DEFAULT 0,
    oldest      TEXT,
    newest      TEXT,
    reverted    INTEGER DEFAULT 0,
    to_check    INTEGER DEFAULT 0,
    checked     INTEGER DEFAULT 0,
    error       TEXT,
    created_at  TEXT,
    finished_at TEXT
);
"""

JSON_FIELDS = {"tags", "draft_scores", "diff", "findings", "community", "reasons", "categories"}
# Seconds between an edit and the revert that undid it (NULL when unknown, e.g. hidden or deleted).
REVERT_SECONDS = "ROUND((julianday(json_extract(community, '$.revert.timestamp')) - julianday(timestamp)) * 86400)"


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _encode(field, value):
    return json.dumps(value, ensure_ascii=False) if field in JSON_FIELDS and value is not None else value


def _decode(row):
    edit = dict(row)
    for field in JSON_FIELDS & edit.keys():
        if edit[field] is not None:
            edit[field] = json.loads(edit[field])
    return edit


class Database:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
            columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(edits)")}
            if "source" not in columns:   # a database from before the page-history search
                self._conn.execute("ALTER TABLE edits ADD COLUMN source TEXT DEFAULT 'live'")

    def _query(self, sql, params=()):
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _run(self, sql, params=()):
        with self._lock:
            return self._conn.execute(sql, params).rowcount

    # -- tracker state ----------------------------------------------------------

    def get_state(self, key, default=None):
        rows = self._query("SELECT value FROM state WHERE key = ?", (key,))
        return rows[0]["value"] if rows else default

    def set_state(self, key, value):
        self._run("INSERT INTO state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                  (key, str(value)))

    def add_to_counter(self, key, amount):
        if amount:
            self.set_state(key, int(self.get_state(key, 0)) + amount)

    # -- edits ------------------------------------------------------------------

    def edit_exists(self, revid):
        return bool(self._query("SELECT 1 FROM edits WHERE revid = ?", (revid,)))

    def existing_revids(self, revids):
        revids = list(revids)
        found = set()
        for i in range(0, len(revids), 500):
            chunk = revids[i:i + 500]
            found |= {r["revid"] for r in self._query(
                f"SELECT revid FROM edits WHERE revid IN ({', '.join('?' for _ in chunk)})", chunk)}
        return found

    def insert_edit(self, edit):
        edit = {"first_seen": _now(), **edit}
        columns = ", ".join(edit)
        marks = ", ".join("?" for _ in edit)
        self._run(f"INSERT OR IGNORE INTO edits ({columns}) VALUES ({marks})",
                  [_encode(k, v) for k, v in edit.items()])

    def update_edit(self, revid, **fields):
        if fields:
            assignments = ", ".join(f"{k} = ?" for k in fields)
            self._run(f"UPDATE edits SET {assignments} WHERE revid = ?",
                      [_encode(k, v) for k, v in fields.items()] + [revid])

    def get_edit(self, revid):
        rows = self._query("SELECT * FROM edits WHERE revid = ?", (revid,))
        return _decode(rows[0]) if rows else None

    def edits_to_analyze(self, limit):
        return [_decode(r) for r in self._query(
            "SELECT * FROM edits WHERE stage = 'new' ORDER BY timestamp LIMIT ?", (limit,))]

    def edits_due(self, now, limit):
        return [_decode(r) for r in self._query(
            "SELECT * FROM edits WHERE stage = 'tracking' AND next_check <= ? ORDER BY next_check LIMIT ?",
            (now, limit))]

    def edits_without_categories(self, limit):
        return [_decode(r) for r in self._query(
            "SELECT * FROM edits WHERE categories IS NULL AND stage != 'new' ORDER BY verdict IN ('confirmed', 'likely') "
            "DESC, timestamp DESC LIMIT ?", (limit,))]

    def tracked_pageids(self):
        return {r["pageid"] for r in self._query("SELECT DISTINCT pageid FROM edits WHERE stage != 'final'")}

    def count_stage(self, stage):
        return self._query("SELECT COUNT(*) AS n FROM edits WHERE stage = ?", (stage,))[0]["n"]

    def set_favorite(self, revid, favorite):
        return self._run("UPDATE edits SET favorite = ?, favorited_at = ? WHERE revid = ?",
                         (1 if favorite else 0, _now() if favorite else None, revid)) > 0

    def request_recheck(self, revid):
        return self._run("UPDATE edits SET stage = CASE stage WHEN 'new' THEN 'new' ELSE 'tracking' END, "
                         "next_check = ? WHERE revid = ?", (_now(), revid)) > 0

    # -- reverts seen in RecentChanges ------------------------------------------

    def add_revert(self, rc):
        self._run("INSERT OR IGNORE INTO reverts (revid, pageid, user, comment, tags, sha1, timestamp) "
                  "VALUES (?, ?, ?, ?, ?, ?, ?)",
                  (rc["revid"], rc["pageid"], rc.get("user"), rc.get("comment", ""), json.dumps(rc.get("tags", [])),
                   rc.get("sha1"), rc["timestamp"]))

    def reverts_after(self, pageid, revid):
        rows = self._query("SELECT * FROM reverts WHERE pageid = ? AND revid > ? ORDER BY revid", (pageid, revid))
        return [{**dict(r), "tags": json.loads(r["tags"] or "[]")} for r in rows]

    # -- topic cache --------------------------------------------------------------

    def cached_categories(self, title, max_age_days=30):
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = self._query("SELECT categories FROM page_topics WHERE title = ? AND fetched_at >= ?", (title, cutoff))
        return json.loads(rows[0]["categories"]) if rows else None

    def cache_categories(self, title, categories, topics):
        self._run("INSERT INTO page_topics (title, categories, topics, fetched_at) VALUES (?, ?, ?, ?) "
                  "ON CONFLICT(title) DO UPDATE SET categories = excluded.categories, topics = excluded.topics, "
                  "fetched_at = excluded.fetched_at", (title, json.dumps(categories), json.dumps(topics), _now()))

    # -- queries for the web page -------------------------------------------------

    def list_edits(self, verdicts=(), statuses=(), category=None, favorites=False, query="", sort="newest",
                   limit=50, offset=0, source=None, pageid=None, user=None, watched=False,
                   revert_min=None, revert_max=None):
        where, params = [], []
        if revert_min is not None:
            where.append(f"{REVERT_SECONDS} >= ?")
            params.append(revert_min)
        if revert_max is not None:
            where.append(f"{REVERT_SECONDS} < ?")
            params.append(revert_max)
        if source:
            where.append("source = ?")
            params.append(source)
        if pageid:
            where.append("pageid = ?")
            params.append(pageid)
        if user:
            where.append("user = ?")
            params.append(user)
        if watched:
            where.append("pageid IN (SELECT pageid FROM watchlist)")
        if verdicts:
            where.append(f"verdict IN ({', '.join('?' for _ in verdicts)})")
            params += list(verdicts)
        if statuses:
            where.append(f"status IN ({', '.join('?' for _ in statuses)})")
            params += list(statuses)
        if favorites:
            where.append("favorite = 1")
        if query:
            where.append("(title LIKE ? OR user LIKE ? OR comment LIKE ? OR diff LIKE ?)")
            params += [f"%{query}%"] * 4
        base = " AND ".join(where) or "1"
        # Category chip counts are computed without the category filter itself.
        category_counts = {}
        rows = self._query(f"SELECT categories FROM edits WHERE {base}", params)
        for row in rows:
            for name in json.loads(row["categories"] or "[]"):
                category_counts[name] = category_counts.get(name, 0) + 1
        total_all = len(rows)
        if category:
            base += " AND categories LIKE ?"
            params.append(f'%"{category}"%')
        order = {
            "confidence": "confidence DESC, timestamp DESC",
            "slowest": f"{REVERT_SECONDS} IS NULL, {REVERT_SECONDS} DESC, timestamp DESC",
            "fastest": f"{REVERT_SECONDS} IS NULL, {REVERT_SECONDS} ASC, timestamp DESC",
        }.get(sort, "timestamp DESC")
        total = self._query(f"SELECT COUNT(*) AS n FROM edits WHERE {base}", params)[0]["n"]
        rows = self._query(f"SELECT * FROM edits WHERE {base} ORDER BY {order} LIMIT ? OFFSET ?",
                           params + [limit, offset])
        return {"items": [_decode(r) for r in rows], "total": total, "total_all": total_all,
                "category_counts": category_counts}

    def stats(self, shown_verdicts, shown_statuses):
        verdicts = {r["verdict"]: r["n"] for r in self._query(
            "SELECT verdict, COUNT(*) AS n FROM edits GROUP BY verdict")}
        shown = self._query(
            f"SELECT COUNT(*) AS n FROM edits WHERE source = 'live' AND verdict IN "
            f"({', '.join('?' for _ in shown_verdicts)}) AND status IN ({', '.join('?' for _ in shown_statuses)})",
            [*shown_verdicts, *shown_statuses])[0]["n"]
        return {
            "scanned": int(self.get_state("scanned_total", 0)),
            "candidates": int(self.get_state("candidates_total", 0)),
            "verdicts": {v: verdicts.get(v, 0) for v in ("confirmed", "likely", "pending", "rejected")},
            "shown": shown,
            "since": self.get_state("first_run"),
        }

    def shown_edits_since(self, since, shown_verdicts, shown_statuses, source="live"):
        """Lightweight rows of the shown (confirmed, cleaned-up) edits since a timestamp, for the stats page."""
        rows = self._query(
            f"SELECT revid, pageid, title, user, timestamp, community, categories FROM edits "
            f"WHERE source = ? AND timestamp >= ? AND verdict IN ({', '.join('?' for _ in shown_verdicts)}) "
            f"AND status IN ({', '.join('?' for _ in shown_statuses)})",
            [source, since, *shown_verdicts, *shown_statuses])
        return [_decode(r) for r in rows]

    def vandal_edit_counts(self, users, shown_verdicts, shown_statuses):
        """{user: number of confirmed, cleaned-up edits} (live and page-history finds)."""
        users = [u for u in set(users) if u]
        counts = {}
        for i in range(0, len(users), 500):
            chunk = users[i:i + 500]
            for r in self._query(
                    f"SELECT user, COUNT(*) AS n FROM edits WHERE user IN ({', '.join('?' for _ in chunk)}) "
                    f"AND verdict IN ({', '.join('?' for _ in shown_verdicts)}) "
                    f"AND status IN ({', '.join('?' for _ in shown_statuses)}) GROUP BY user",
                    [*chunk, *shown_verdicts, *shown_statuses]):
                counts[r["user"]] = r["n"]
        return counts

    # -- watchlist ----------------------------------------------------------------------

    def watch(self, pageid, title):
        self._run("INSERT INTO watchlist (pageid, title, added_at) VALUES (?, ?, ?) "
                  "ON CONFLICT(pageid) DO UPDATE SET title = excluded.title", (pageid, title, _now()))

    def unwatch(self, pageid):
        return self._run("DELETE FROM watchlist WHERE pageid = ?", (pageid,)) > 0

    def watchlist(self):
        return [dict(r) for r in self._query(
            "SELECT w.pageid, w.title, w.added_at, (SELECT COUNT(*) FROM edits e WHERE e.pageid = w.pageid "
            "AND e.verdict = 'confirmed' AND e.status IN ('reverted', 'hidden', 'deleted')) AS vandal_edits "
            "FROM watchlist w ORDER BY w.title COLLATE NOCASE")]

    def latest_watched_vandalism(self, limit, shown_verdicts, shown_statuses):
        """Newest live-tracked confirmed vandalism on watched pages (for notifications)."""
        return [dict(r) for r in self._query(
            f"SELECT revid, title, user, timestamp FROM edits WHERE source = 'live' "
            f"AND pageid IN (SELECT pageid FROM watchlist) AND verdict IN ({', '.join('?' for _ in shown_verdicts)}) "
            f"AND status IN ({', '.join('?' for _ in shown_statuses)}) ORDER BY revid DESC LIMIT ?",
            [*shown_verdicts, *shown_statuses, limit])]

    # -- housekeeping ---------------------------------------------------------------

    def cleanup(self, keep_days):
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        day_ago = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._run("DELETE FROM edits WHERE favorite = 0 AND source = 'live' AND timestamp < ?", (cutoff,))
        self._run("DELETE FROM edits WHERE favorite = 0 AND source = 'live' AND verdict = 'rejected' "
                  "AND timestamp < ?", (day_ago,))
        # History finds are old edits by nature: forget them by when they were found instead.
        self._run("DELETE FROM edits WHERE favorite = 0 AND source = 'history' AND first_seen < ?", (cutoff,))
        self._run("DELETE FROM history_searches WHERE created_at < ?", (cutoff,))
        self._run("DELETE FROM reverts WHERE timestamp < ?", (day_ago,))

    # -- page-history searches ---------------------------------------------------------

    def create_search(self, query, depth):
        with self._lock:
            return self._conn.execute(
                "INSERT INTO history_searches (query, depth, status, phase, created_at) VALUES (?, ?, 'queued', ?, ?)",
                (query, depth, "Waiting to start", _now())).lastrowid

    def update_search(self, search_id, **fields):
        if fields:
            assignments = ", ".join(f"{k} = ?" for k in fields)
            self._run(f"UPDATE history_searches SET {assignments} WHERE id = ?", [*fields.values(), search_id])

    def get_search(self, search_id):
        rows = self._query("SELECT * FROM history_searches WHERE id = ?", (search_id,))
        return dict(rows[0]) if rows else None

    def recent_searches(self, limit):
        return [dict(r) for r in self._query("SELECT * FROM history_searches ORDER BY id DESC LIMIT ?", (limit,))]

    def unfinished_searches(self):
        return [r["id"] for r in self._query(
            "SELECT id FROM history_searches WHERE status IN ('queued', 'running') ORDER BY id")]

    def forget_older_searches(self, pageid, keep):
        """One search per page in the 'recent searches' list: the newest one."""
        self._run("DELETE FROM history_searches WHERE pageid = ? AND id != ?", (pageid, keep))
