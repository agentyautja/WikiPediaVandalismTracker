"""The tracker loop: scan RecentChanges, run check 1 and check 2, and keep re-checking."""

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

import config
from . import community, diffing, heuristics, topics, verdict
from .wiki import ApiError

log = logging.getLogger(__name__)


def utcnow():
    return datetime.now(timezone.utc)


def iso(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(timestamp):
    return datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _ores(rc):
    """(P(bad faith), P(damaging)) from a RecentChanges entry, or (None, None) if not scored yet."""
    scores = rc.get("oresscores")
    if not isinstance(scores, dict) or "goodfaith" not in scores:
        return None, None
    return scores["goodfaith"].get("false"), (scores.get("damaging") or {}).get("true")


def _still_there(diff, text):
    """Is the vandalism still in the page text? True, False or None (can't tell)."""
    if text is None:
        return None
    if diff.get("added_sample"):
        return diff["added_sample"] in text
    if diff.get("removed_sample"):   # pure removal: fixed once the removed text is back
        return diff["removed_sample"] not in text
    return None


class Monitor(threading.Thread):
    def __init__(self, db, wiki, profile):
        super().__init__(name="vandalism-tracker", daemon=True)
        self.db = db
        self.wiki = wiki
        self.profile = profile
        self.wake = threading.Event()
        self.status = {"state": "starting", "last_scan": None, "last_error": None}
        self._seen = {}   # rcid -> timestamp, so overlapping scans don't count an edit twice
        self._min_rcid = int(db.get_state("rc_max_rcid", 0))
        self._last_recheck = 0.0
        self._last_cleanup = 0.0
        self._recheck_requested = False
        if db.get_state("first_run") is None:
            db.set_state("first_run", iso(utcnow()))

    def request_recheck(self):
        self._recheck_requested = True
        self.wake.set()

    def run(self):
        log.info("Tracker started (%s mode)", "fast" if config.CONTACT.strip() else "slow")
        while True:
            started = time.monotonic()
            try:
                self.scan_recent_changes()
                self.analyze_candidates()
                if self._recheck_requested or started - self._last_recheck >= self.profile["recheck_seconds"]:
                    self._recheck_requested = False
                    self._last_recheck = started
                    self.recheck_due()
                self.assign_categories()
                if started - self._last_cleanup >= 1800:
                    self._last_cleanup = started
                    self.db.cleanup(config.KEEP_DAYS)
                self.status.update(state="running", last_error=self.wiki.last_error)
            except Exception as exc:   # keep tracking whatever happens; show the problem in the UI
                log.exception("Tracker loop failed")
                self.status.update(state="error", last_error=str(exc))
            self.wake.wait(max(1.0, self.profile["poll_seconds"] - (time.monotonic() - started)))
            self.wake.clear()

    # -- scanning + check 1 ---------------------------------------------------------

    def scan_recent_changes(self):
        now = utcnow()
        end = now - timedelta(seconds=config.EDIT_MIN_AGE_SECONDS)
        earliest = now - timedelta(minutes=min(config.BACKFILL_MINUTES, 1440))
        last = self.db.get_state("rc_last_timestamp")
        start = max(parse(last) - timedelta(seconds=60), earliest) if last else earliest
        if start >= end:
            return
        if not last:
            self.status["state"] = "backfilling"
            log.info("Looking back %s minutes of recent changes...", config.BACKFILL_MINUTES)
        changes = self.wiki.recent_changes(iso(start), iso(end), config.NAMESPACES)
        tracked_pages = self.db.tracked_pageids()
        scanned = candidates = 0
        max_rcid = self._min_rcid
        for rc in changes:
            rcid = rc.get("rcid", 0)
            if rcid in self._seen or rcid <= self._min_rcid:
                continue
            self._seen[rcid] = rc["timestamp"]
            max_rcid = max(max_rcid, rcid)
            if rc.get("pageid") in tracked_pages and community.is_revert(rc.get("user"), rc.get("tags")):
                self.db.add_revert(rc)
            if rc.get("bot"):   # like hidebots=1: bot edits are only used to notice reverts
                continue
            scanned += 1
            badfaith, damaging = _ores(rc)
            if badfaith is None:   # RecentChanges hasn't scored it yet: ask the ORES models directly
                badfaith, damaging = self.wiki.ores_scores(rc["revid"])
            if badfaith is None or badfaith < config.BADFAITH_THRESHOLD or self.db.edit_exists(rc["revid"]):
                continue
            candidates += 1
            tracked_pages.add(rc["pageid"])
            scores = rc.get("oresscores") if isinstance(rc.get("oresscores"), dict) else {}
            self.db.insert_edit({
                "revid": rc["revid"], "parentid": rc.get("old_revid") or 0, "pageid": rc["pageid"],
                "title": rc["title"], "user": rc.get("user", ""), "timestamp": rc["timestamp"],
                "comment": rc.get("comment", ""), "edit_type": rc["type"], "oldlen": rc.get("oldlen"),
                "newlen": rc.get("newlen"), "tags": rc.get("tags", []), "badfaith": badfaith, "damaging": damaging,
                "draft_scores": scores.get("draftquality"), "stage": "new",
            })
            log.info("Check 1: %.0f%% bad faith - %s (by %s)", badfaith * 100, rc["title"], rc.get("user"))
        self.db.add_to_counter("scanned_total", scanned)
        self.db.add_to_counter("candidates_total", candidates)
        self.db.set_state("rc_last_timestamp", iso(end))
        self.db.set_state("rc_max_rcid", max_rcid)
        cutoff = iso(now - timedelta(minutes=10))
        self._seen = {k: v for k, v in self._seen.items() if v >= cutoff}
        self.status.update(state="running", last_scan=iso(now))
        if scanned:
            log.info("Scanned %d new edits, %d passed check 1", scanned, candidates)

    # -- check 2, part 1: what does the edit contain? --------------------------------

    def analyze_candidates(self):
        for _ in range(3):   # a few batches per loop, so a backlog clears quickly
            batch = self.db.edits_to_analyze(limit=10)
            if not batch:
                return
            revids = [e["revid"] for e in batch] + [e["parentid"] for e in batch if e["parentid"]]
            revisions, missing = self.wiki.revisions(revids, content=True)
            for edit in batch:
                self._analyze(edit, revisions, missing)

    def _analyze(self, edit, revisions, missing):
        new = revisions.get(edit["revid"])
        old = revisions.get(edit["parentid"]) if edit["parentid"] else None
        if new is None or new["text"] is None:
            diff = {"hunks": [], "unavailable": "deleted" if edit["revid"] in missing else "hidden"}
            added = removed = old_text = context = ""
        else:
            old_text = (old or {}).get("text") or ""
            result = diffing.diff_texts(old_text, new["text"])
            diff = {"hunks": result.hunks, "added_sample": result.added_sample,
                    "removed_sample": result.removed_sample, "added_chars": len(result.added),
                    "removed_chars": len(result.removed)}
            added, removed, context = result.added, result.removed, result.new_context
        findings = heuristics.analyze(added, removed, old_text, edit, context)
        fields = {"diff": diff, "findings": findings, "content_score": heuristics.score(findings),
                  "revert_risk": self.wiki.revert_risk(edit["revid"], config.REVERT_RISK_MODEL)}
        # A first verdict right away (content + model); how Wikipedia reacts is added at the re-checks.
        result, confidence, reasons = verdict.decide({**edit, **fields}, final=False)
        if result in ("confirmed", "likely"):
            log.info("Check 2: %s vandalism - %s (%s)", result.upper(), edit["title"], reasons[0])
        self.db.update_edit(edit["revid"], **fields, verdict=result, confidence=confidence, reasons=reasons,
                            parent_sha1=(old or {}).get("sha1"), stage="tracking", next_check=iso(utcnow()))

    # -- check 2, part 2: how did Wikipedia react? (re-checked on a schedule) ------

    def recheck_due(self):
        due = self.db.edits_due(iso(utcnow()), limit=50)
        if not due:
            return
        revisions, missing = self.wiki.revisions([e["revid"] for e in due])
        unblocked = [e["user"] for e in due if e["user"] and not (e["community"] or {}).get("block")]
        blocks = self.wiki.blocks(unblocked) if unblocked else {}
        now = utcnow()
        results, unclear = [], []
        for edit in due:
            info = dict(edit["community"] or {})
            rev = revisions.get(edit["revid"])
            if edit["user"] in blocks and not info.get("block"):
                block = blocks[edit["user"]]
                info["block"] = {**block, "kind": community.classify_block(block.get("reason"))}
            if edit["revid"] in missing or rev is None:
                status = "deleted"
                if "deleted" not in info:
                    reason = self._safe(lambda: self.wiki.deletion_reason(edit["title"])) or ""
                    info["deleted"] = {"reason": reason, "kind": community.classify_deletion(reason)}
            elif rev["hidden"]:
                status, info["hidden"] = "hidden", True
            elif rev["lastrevid"] == edit["revid"]:
                status = "live"
            elif "mw-reverted" in rev["tags"]:
                status = "reverted"
                if not (info.get("revert") or {}).get("user"):   # not identified yet: look (again)
                    info["revert"] = self._find_revert(edit)
            else:
                status = "edited"   # the page changed since: is the vandalism still in it?
                unclear.append(edit)
            results.append([edit, status, info, rev])

        if unclear:
            texts = self._safe(lambda: self.wiki.latest_texts([e["pageid"] for e in unclear])) or {}
            for item in results:
                edit, status, info, _ = item
                if status == "edited" and edit["pageid"] in texts:
                    there = _still_there(edit["diff"] or {}, texts[edit["pageid"]][1])
                    if there is True:
                        item[1] = "live"
                    elif there is False:
                        item[1], info["cleaned"] = "reverted", True

        for edit, status, info, rev in results:
            self._record_check(edit, status, info, rev, now)

    def _record_check(self, edit, status, info, rev, now):
        age = (now - parse(edit["timestamp"])).total_seconds() / 60
        info["survived"] = (status == "live" and age >= config.SURVIVAL_MINUTES
                            and rev is not None and rev["lastrevid"] != edit["revid"])
        extra = {}
        if edit["revert_risk"] is None and edit["edit_type"] == "edit" and (edit["check_count"] or 0) < 3:
            extra["revert_risk"] = self.wiki.revert_risk(edit["revid"], config.REVERT_RISK_MODEL)   # model was down
        updated = {**edit, **extra, "community": info, "status": status}
        prelim, _, _ = verdict.decide(updated, final=False)
        next_check = self._next_check(edit, now, status, prelim, info)
        result, confidence, reasons = verdict.decide(updated, final=next_check is None)
        if result != edit["verdict"] and result in ("confirmed", "likely"):
            log.info("Check 2: %s vandalism - %s (%s)", result.upper(), edit["title"], reasons[0])
        self.db.update_edit(
            edit["revid"], **extra, status=status, community=info, verdict=result, confidence=confidence,
            reasons=reasons, last_checked=iso(now), check_count=(edit["check_count"] or 0) + 1,
            next_check=iso(next_check) if next_check else None, stage="tracking" if next_check else "final")

    def _next_check(self, edit, now, status, current_verdict, info):
        if status in ("deleted", "hidden"):
            return None   # nothing more will happen to it
        if status == "reverted" and (current_verdict == "confirmed" or info.get("revert", {}).get("kind") == "goodfaith"):
            return None   # settled
        made = parse(edit["timestamp"])
        age = (now - made).total_seconds() / 60
        for minutes in config.RECHECK_AT_MINUTES:
            if minutes > age + 0.5:
                return made + timedelta(minutes=minutes)
        if status == "live" and current_verdict in ("confirmed", "likely") and age < config.LIVE_RECHECK_HOURS * 60:
            return now + timedelta(hours=1)   # still live: keep an eye on it
        return None

    def _find_revert(self, edit):
        """Who reverted the edit, and what they said about it."""
        parent_sha1 = edit.get("parent_sha1")
        seen = self.db.reverts_after(edit["pageid"], edit["revid"])
        chosen = next((r for r in seen if parent_sha1 and r["sha1"] == parent_sha1), None) or (seen[0] if seen else None)
        if chosen is None:   # not seen in our scans (e.g. the app wasn't running): ask Wikipedia
            later = [r for r in self._safe(lambda: self.wiki.history_after(edit["pageid"], edit["revid"])) or []
                     if r["revid"] > edit["revid"]]
            chosen = (next((r for r in later if parent_sha1 and r.get("sha1") == parent_sha1), None)
                      or next((r for r in later if community.is_revert(r.get("user"), r.get("tags"))), None))
        if chosen is None:
            return {"kind": "revert", "user": None, "comment": "", "tags": []}
        kind = community.classify_revert(chosen.get("user"), chosen.get("comment"), chosen.get("tags"), edit["user"])
        return {"kind": kind, "user": chosen.get("user"), "comment": chosen.get("comment", ""),
                "tags": chosen.get("tags", []), "revid": chosen["revid"], "timestamp": chosen.get("timestamp")}

    # -- categories ---------------------------------------------------------------------

    def assign_categories(self):
        waiting = []
        for edit in self.db.edits_without_categories(limit=20):
            cached = self.db.cached_categories(edit["title"])
            if cached is not None:
                self.db.update_edit(edit["revid"], categories=cached)
                continue
            found = self.wiki.article_topics(edit["title"])
            names = topics.from_topics(found) if found else []
            if names:
                self.db.cache_categories(edit["title"], names, found)
                self.db.update_edit(edit["revid"], categories=names)
            elif edit["verdict"] == "rejected":
                self.db.update_edit(edit["revid"], categories=["Other"])
            else:
                waiting.append(edit)
        if waiting:   # topic model had nothing: fall back to the page's own Wikipedia categories
            wiki_categories = self._safe(lambda: self.wiki.categories([e["title"] for e in waiting])) or {}
            for edit in waiting:
                names = topics.from_wiki_categories(wiki_categories.get(edit["title"], [])) or ["Other"]
                self.db.cache_categories(edit["title"], names, None)
                self.db.update_edit(edit["revid"], categories=names)

    @staticmethod
    def _safe(call):
        try:
            return call()
        except ApiError as exc:
            log.warning("Wikipedia API: %s", exc)
            return None
