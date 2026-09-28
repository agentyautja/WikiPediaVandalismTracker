"""Search one page's edit history for vandalism (the "Page history" tab).

Old edits don't come with ORES scores in RecentChanges, but they come with something better: how
Wikipedia already dealt with them. A search reads the page's latest edits, finds the ones that were
reverted (a later revision restored the exact earlier text, or MediaWiki tagged them "mw-reverted")
or hidden by admins, and runs those through the same two checks as live edits.
"""

import logging
import queue
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import parse_qs, unquote, urlparse

import config
from . import community, topics, verdict
from .monitor import analyze_content
from .wiki import ApiError

log = logging.getLogger(__name__)

DEPTHS = [500, 1000, 2500, 5000]   # how many of the latest edits a search can look at
REVERT_WINDOW = 15                 # like MediaWiki, a revert can undo at most this many edits
BOT_NAME = re.compile(r"bot\b", re.IGNORECASE)
WORKERS = 3                        # parallel Lift Wing requests (Wikimedia asks for 3 or fewer)
# Admins also hide revisions that aren't vandalism: copyright violations (RD1), private info (RD4),
# housekeeping (RD5/RD6). Those are skipped.
NOT_VANDAL_HIDING = re.compile(r"\bRD ?[1456]\b|copyright|copyvio|personal information|privacy", re.IGNORECASE)


class SearchError(Exception):
    """A problem to show to the user as-is, e.g. the page doesn't exist."""


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def parse_page(text):
    """("title", name) or ("revid", number) from a page name or any kind of Wikipedia link."""
    text = (text or "").strip()
    if not text:
        raise SearchError("Type a page name or paste a Wikipedia link.")
    if re.match(r"^(?:https?://)?[\w.-]*wiki[mp]edia\.org(?:[/?#]|$)", text, re.IGNORECASE):
        url = urlparse(text if "://" in text else "https://" + text)
        domain = config.WIKI_DOMAIN
        if url.netloc.lower() not in (domain, domain.replace(".wikipedia.", ".m.wikipedia.")):
            raise SearchError(f"Only pages on {domain} can be searched.")
        query = parse_qs(url.query)
        for key in ("oldid", "diff"):
            if query.get(key, [""])[0].isdigit():
                return "revid", int(query[key][0])
        if "title" in query:
            text = query["title"][0]
        elif url.path.startswith("/wiki/") and len(url.path) > len("/wiki/"):
            text = unquote(url.path[len("/wiki/"):])
        else:
            raise SearchError("That link doesn't point to a Wikipedia page.")
    title = " ".join(text.split("#")[0].replace("_", " ").split())
    if not title:
        raise SearchError("That link doesn't point to a Wikipedia page.")
    return "title", title


def find_reverted(revisions):
    """{revid: the revision that reverted it} for a page's revisions (oldest first)."""
    reverted = {}
    last_seen = {}   # sha1 -> index of the latest revision with that exact text
    for j, rev in enumerate(revisions):
        sha1 = rev.get("sha1")
        if not sha1 or rev.get("sha1hidden"):
            continue
        i = last_seen.get(sha1)
        if i is not None and 1 < j - i <= REVERT_WINDOW + 1:   # text restored: everything in between undone
            for k in range(i + 1, j):
                reverted.setdefault(revisions[k]["revid"], rev)
        last_seen[sha1] = j
    # Partial reverts ("undo" of an older edit) don't restore an identical text, but MediaWiki
    # tags the undone edit "mw-reverted". Credit the first revert that followed it.
    for k, rev in enumerate(revisions):
        if "mw-reverted" in rev.get("tags", []) and rev["revid"] not in reverted:
            later = revisions[k + 1:k + 1 + REVERT_WINDOW]
            reverter = next((r for r in later if community.is_revert(r.get("user"), r.get("tags"))), None)
            if reverter:
                reverted[rev["revid"]] = reverter
    return reverted


class HistorySearcher(threading.Thread):
    """Runs page-history searches one at a time in the background."""

    def __init__(self, db, wiki):
        super().__init__(name="history-search", daemon=True)
        self.db = db
        self.wiki = wiki
        self.jobs = queue.Queue()

    def request(self, text, depth):
        """Queue a search; returns its id. Raises SearchError for input that can't be a page."""
        parse_page(text)
        search_id = self.db.create_search(text.strip(), depth if depth in DEPTHS else DEPTHS[0])
        self.jobs.put(search_id)
        return search_id

    def run(self):
        for search_id in self.db.unfinished_searches():   # interrupted when the app was closed
            self.jobs.put(search_id)
        while True:
            search_id = self.jobs.get()
            try:
                self._search(search_id)
            except SearchError as exc:
                self.db.update_search(search_id, status="error", error=str(exc), finished_at=_now())
            except Exception as exc:   # e.g. Wikipedia unreachable: show it, keep the thread alive
                log.exception("History search failed")
                self.db.update_search(search_id, status="error", error=f"The search failed: {exc}",
                                      finished_at=_now())

    def _search(self, search_id):
        search = self.db.get_search(search_id)
        if search is None:
            return

        def update(**fields):
            self.db.update_search(search_id, **fields)

        update(status="running", phase="Finding the page", error=None)
        kind, value = parse_page(search["query"])
        page = self.wiki.page_info(**{kind: value})
        if page is None:
            raise SearchError(f"There's no page called “{value}” on {config.WIKI_DOMAIN}." if kind == "title"
                              else "That revision doesn't exist (any more).")
        self.db.forget_older_searches(page["pageid"], keep=search_id)
        update(title=page["title"], pageid=page["pageid"], phase=f"Reading the last {search['depth']:,} edits")
        log.info("History search: %s (last %d edits)", page["title"], search["depth"])

        history = sorted(self.wiki.page_history(page["pageid"], search["depth"]), key=lambda r: r["revid"])
        if not history:
            update(status="done", phase="Done", finished_at=_now())
            return
        reverters = find_reverted(history)
        hidden = {}
        if any(r.get("sha1hidden") for r in history):   # why did admins hide them?
            reasons = self._safe(lambda: self.wiki.revision_deletions(page["title"])) or {}
            hidden = {r["revid"]: reasons.get(r["revid"], "") for r in history if r.get("sha1hidden")}
            hidden = {revid: why for revid, why in hidden.items() if not NOT_VANDAL_HIDING.search(why)}
        update(revisions=len(history), oldest=history[0]["timestamp"], newest=history[-1]["timestamp"],
               reverted=len(reverters), phase="Looking at the reverted edits")

        candidates = self._candidates(history, reverters, hidden)
        update(to_check=len(candidates), phase=f"Check 1: bad-faith scores for {len(candidates)} reverted edits")
        with ThreadPoolExecutor(WORKERS) as pool:
            scores = list(pool.map(lambda c: self.wiki.ores_probability(c[0]["revid"], "goodfaith", "false"),
                                   candidates))

        by_id = {r["revid"]: r for r in history}
        categories = self._categories(page["title"])
        passed, checked = [], 0
        for (rev, info), badfaith in zip(candidates, scores):
            row = self._row(page, rev, by_id.get(rev.get("parentid")), info, badfaith, categories)
            if badfaith is not None and badfaith < config.BADFAITH_THRESHOLD:
                row.update(verdict="rejected", confidence=0.0, reasons=[
                    f"Check 1: ORES rates it only {badfaith:.0%} likely to be bad faith"])
                self.db.insert_edit(row)   # remembered, so searching again doesn't redo it
                checked += 1
            else:
                passed.append(row)
        update(checked=checked, phase=f"Check 2: {len(passed)} edits passed check 1")

        users = {row["user"] for row in passed if row["user"] != "(hidden)"}
        blocks = self._safe(lambda: self.wiki.blocks(users)) if users else {}
        for batch in _chunks(passed, 10):
            revids = [row["revid"] for row in batch] + [row["parentid"] for row in batch if row["parentid"]]
            revisions, missing = self.wiki.revisions(revids, content=True)
            with ThreadPoolExecutor(WORKERS) as pool:
                risks = list(pool.map(lambda row: self.wiki.revert_risk(row["revid"], config.REVERT_RISK_MODEL),
                                      batch))
            for row, risk in zip(batch, risks):
                block = (blocks or {}).get(row["user"])
                if block:
                    row["community"]["block"] = {**block, "kind": community.classify_block(block.get("reason"))}
                row.update(analyze_content(row, revisions, missing), revert_risk=risk)
                result, confidence, reasons = verdict.decide(row, final=True)
                row.update(verdict=result, confidence=confidence, reasons=reasons)
                self.db.insert_edit(row)
                checked += 1
            update(checked=checked, phase=f"Check 2: {checked} of {len(candidates)} checked")
        update(status="done", phase="Done", finished_at=_now())
        log.info("History search done: %s", page["title"])

    def _candidates(self, history, reverters, hidden):
        """(revision, community info) for every reverted or hidden edit that still needs checking.

        `hidden` maps the revids admins hid (for a vandalism-type reason) to that reason.
        """
        already = self.db.existing_revids(set(reverters) | set(hidden))
        candidates = []
        for rev in history:
            revid, user = rev["revid"], rev.get("user", "")
            if revid in already or (revid not in reverters and revid not in hidden) or BOT_NAME.search(user):
                continue
            info = {"hidden": True, "hidden_reason": hidden[revid]} if revid in hidden else {}
            reverter = reverters.get(revid)
            if reverter:
                kind = community.classify_revert(reverter.get("user"), reverter.get("comment"),
                                                 reverter.get("tags"), user)
                if kind in ("goodfaith", "self") and revid not in hidden:
                    continue   # Wikipedians already said this wasn't vandalism
                info["revert"] = {"kind": kind, "user": reverter.get("user"), "comment": reverter.get("comment", ""),
                                  "tags": reverter.get("tags", []), "revid": reverter["revid"],
                                  "timestamp": reverter.get("timestamp")}
            candidates.append((rev, info))
        return candidates

    @staticmethod
    def _row(page, rev, parent, info, badfaith, categories):
        return {
            "revid": rev["revid"], "parentid": rev.get("parentid") or 0, "pageid": page["pageid"],
            "title": page["title"], "user": rev.get("user") or "(hidden)", "timestamp": rev["timestamp"],
            "comment": rev.get("comment", ""), "edit_type": "edit" if rev.get("parentid") else "new",
            "oldlen": (parent or {}).get("size"), "newlen": rev.get("size"), "tags": rev.get("tags", []),
            "badfaith": badfaith, "damaging": None, "draft_scores": None, "revert_risk": None,
            "community": info, "status": "hidden" if info.get("hidden") else "reverted",
            "categories": categories, "stage": "final", "last_checked": _now(), "check_count": 1,
            "source": "history",
        }

    def _categories(self, title):
        cached = self.db.cached_categories(title)
        if cached is not None:
            return cached
        found = self.wiki.article_topics(title)
        names = topics.from_topics(found) if found else []
        if not names:
            wiki_categories = self._safe(lambda: self.wiki.categories([title])) or {}
            names = topics.from_wiki_categories(wiki_categories.get(title, []))
        names = names or ["Other"]
        self.db.cache_categories(title, names, found)
        return names

    @staticmethod
    def _safe(call):
        try:
            return call()
        except ApiError as exc:
            log.warning("Wikipedia API: %s", exc)
            return None
