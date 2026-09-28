"""Talking to Wikipedia: the MediaWiki Action API and Wikimedia's Lift Wing ML models."""

import email.utils
import logging
import threading
import time
from collections import deque

import requests

log = logging.getLogger(__name__)

APP_NAME = "WikipediaVandalismTracker/1.0"
LIFTWING_URL = "https://api.wikimedia.org/service/lw/inference/v1/models/{model}:predict"


class ApiError(Exception):
    """The Wikipedia API returned an error or could not be reached."""


class RateLimiter:
    """Allows at most `per_minute` calls in any rolling 60-second window."""

    def __init__(self, per_minute):
        self.per_minute = per_minute
        self._calls = deque()
        self._lock = threading.Lock()
        self._paused_until = 0.0

    def wait(self):
        while True:
            with self._lock:
                now = time.monotonic()
                while self._calls and now - self._calls[0] >= 60:
                    self._calls.popleft()
                delay = self._paused_until - now
                if delay <= 0 and len(self._calls) >= self.per_minute:
                    delay = 60 - (now - self._calls[0])
                if delay <= 0:
                    self._calls.append(now)
                    return
            time.sleep(min(delay, 60) + 0.05)

    def pause(self, seconds):
        with self._lock:
            self._paused_until = max(self._paused_until, time.monotonic() + seconds)

    def used_last_minute(self):
        with self._lock:
            now = time.monotonic()
            return sum(1 for t in self._calls if now - t < 60)


def _retry_after(resp, default):
    """Seconds to wait according to a Retry-After header (number or HTTP date)."""
    value = resp.headers.get("Retry-After", "")
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = email.utils.parsedate_to_datetime(value).timestamp() - time.time()
        except (TypeError, ValueError):
            seconds = default
    return min(max(seconds, 1.0), 300.0)


def _chunks(items, size):
    items = list(items)
    for i in range(0, len(items), size):
        yield items[i:i + size]


class WikiClient:
    """One client per thread. Pass `share_limits_with` so several clients stay within one request budget."""

    def __init__(self, domain, lang, contact, api_per_minute, share_limits_with=None):
        self.domain = domain
        self.lang = lang
        self.wiki_db = f"{lang}wiki"
        self.api_url = f"https://{domain}/w/api.php"
        who = contact.strip() or "no contact info configured"
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": f"{APP_NAME} ({who}) python-requests/{requests.__version__}",
            "Accept-Encoding": "gzip",
        })
        self.api_limiter = RateLimiter(api_per_minute)
        self.liftwing_limiter = RateLimiter(300)   # Lift Wing allows 15/s; we need far less
        if share_limits_with:
            self.api_limiter = share_limits_with.api_limiter
            self.liftwing_limiter = share_limits_with.liftwing_limiter
        self.last_error = None

    # -- low level ------------------------------------------------------------

    def api(self, **params):
        params.update(format="json", formatversion="2")
        for attempt in range(5):
            self.api_limiter.wait()
            try:
                resp = self.session.get(self.api_url, params=params, timeout=60)
            except requests.RequestException as exc:
                self.last_error = f"Can't reach Wikipedia ({exc.__class__.__name__}), retrying"
                log.warning(self.last_error)
                time.sleep(5 * 2 ** attempt)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = _retry_after(resp, default=10 * 2 ** attempt)
                self.api_limiter.pause(wait)
                self.last_error = f"Wikipedia asked us to slow down (HTTP {resp.status_code}), pausing {wait:.0f}s"
                log.warning(self.last_error)
                continue
            if resp.status_code >= 400:
                raise ApiError(f"HTTP {resp.status_code} from the Wikipedia API")
            data = resp.json()
            if "error" in data:
                raise ApiError(f"{data['error'].get('code')}: {data['error'].get('info')}")
            self.last_error = None
            return data
        raise ApiError(self.last_error or "Wikipedia API unreachable")

    def api_pages(self, **params):
        """Like api(), but follows 'continue' and yields every page of results."""
        cont = {}
        while True:
            data = self.api(**params, **cont)
            yield data
            if "continue" not in data:
                return
            cont = data["continue"]

    def liftwing(self, model, payload):
        """Ask a Lift Wing model. The models are extras, so give up quickly (None) when one is down."""
        url = LIFTWING_URL.format(model=model)
        for attempt in range(2):
            self.liftwing_limiter.wait()
            try:
                resp = self.session.post(url, json=payload, timeout=20)
            except requests.RequestException:
                time.sleep(2)
                continue
            if resp.status_code == 429:
                self.liftwing_limiter.pause(_retry_after(resp, default=5))
                continue
            if resp.status_code >= 500:
                time.sleep(2)
                continue
            if resp.status_code >= 400:   # e.g. the model can't score page creations
                log.debug("Lift Wing %s gave HTTP %s for %s", model, resp.status_code, payload)
                return None
            return resp.json()
        return None

    # -- MediaWiki API --------------------------------------------------------

    def recent_changes(self, start, end, namespaces):
        """Edits and page creations between two ISO timestamps, oldest first (bots included)."""
        changes = []
        for data in self.api_pages(
                action="query", list="recentchanges", rcdir="newer", rcstart=start, rcend=end,
                rcnamespace="|".join(map(str, namespaces)), rctype="edit|new", rclimit="500",
                rcprop="title|ids|sizes|flags|user|timestamp|comment|tags|oresscores|sha1"):
            changes.extend(data.get("query", {}).get("recentchanges", []))
        return changes

    def revisions(self, revids, content=False):
        """Look up specific revisions.

        Returns ({revid: info}, missing_revids). Revisions of deleted pages come back as missing.
        """
        found, missing = {}, set()
        rvprop = "ids|sha1|size|tags|user|timestamp" + ("|content" if content else "")
        for chunk in _chunks(sorted(set(revids)), 20 if content else 50):
            params = dict(action="query", prop="info|revisions", revids="|".join(map(str, chunk)), rvprop=rvprop)
            if content:
                params["rvslots"] = "main"
            for data in self.api_pages(**params):
                query = data.get("query", {})
                bad = query.get("badrevids", {})
                for item in (bad.values() if isinstance(bad, dict) else bad):
                    missing.add(int(item["revid"]))
                for page in query.get("pages", []):
                    for rev in page.get("revisions", []):
                        slot = rev.get("slots", {}).get("main", {})
                        found[rev["revid"]] = {
                            "revid": rev["revid"],
                            "parentid": rev.get("parentid"),
                            "pageid": page.get("pageid"),
                            "title": page.get("title"),
                            "lastrevid": page.get("lastrevid"),
                            "sha1": rev.get("sha1"),
                            "tags": rev.get("tags", []),
                            "hidden": bool(rev.get("sha1hidden") or slot.get("texthidden")),
                            "text": slot.get("content"),
                        }
        return found, missing

    def page_info(self, title=None, revid=None):
        """{"pageid", "title", "ns"} for a page name (redirects followed) or a revision id; None if missing."""
        params = dict(action="query", prop="info", redirects="1")
        if revid:
            params["revids"] = revid
        else:
            params["titles"] = title
        pages = self.api(**params).get("query", {}).get("pages", [])
        if not pages or pages[0].get("missing") or pages[0].get("invalid") or "pageid" not in pages[0]:
            return None
        return {"pageid": pages[0]["pageid"], "title": pages[0]["title"], "ns": pages[0].get("ns", 0)}

    def page_history(self, pageid, limit):
        """The latest `limit` revisions of a page (metadata only, newest first), 500 per request."""
        revisions = []
        for data in self.api_pages(action="query", prop="revisions", pageids=pageid, rvlimit="max",
                                   rvprop="ids|timestamp|user|comment|tags|sha1|size"):
            for page in data.get("query", {}).get("pages", []):
                revisions.extend(page.get("revisions", []))
            if len(revisions) >= limit:
                break
        return revisions[:limit]

    def revision_deletions(self, title):
        """{revid: reason} for revisions of a page that admins hid (from the revision-deletion log)."""
        reasons = {}
        for data in self.api_pages(action="query", list="logevents", letype="delete", leaction="delete/revision",
                                   letitle=title, lelimit="max", leprop="ids|comment|details"):
            for event in data.get("query", {}).get("logevents", []):
                for revid in (event.get("params") or {}).get("ids") or []:
                    if str(revid).isdigit():
                        reasons.setdefault(int(revid), event.get("comment", ""))   # newest reason wins
        return reasons

    def history_after(self, pageid, revid, limit=25):
        """The revision `revid` and the ones that followed it on the same page."""
        data = self.api(action="query", prop="revisions", pageids=pageid, rvstartid=revid, rvdir="newer",
                        rvlimit=limit, rvprop="ids|user|comment|tags|sha1|timestamp")
        pages = data.get("query", {}).get("pages", [])
        return pages[0].get("revisions", []) if pages else []

    def latest_texts(self, pageids):
        """Current wikitext of pages: {pageid: (lastrevid, text)}."""
        texts = {}
        for chunk in _chunks(sorted(set(pageids)), 20):
            for data in self.api_pages(action="query", prop="revisions", pageids="|".join(map(str, chunk)),
                                       rvprop="ids|content", rvslots="main"):
                for page in data.get("query", {}).get("pages", []):
                    revs = page.get("revisions") or []
                    if revs:
                        text = revs[0].get("slots", {}).get("main", {}).get("content")
                        texts[page["pageid"]] = (revs[0]["revid"], text)
        return texts

    def blocks(self, users):
        """Current blocks of these users: {username: {"reason", "by", "timestamp", "expiry"}}."""
        blocked = {}
        for chunk in _chunks(sorted(set(users)), 50):
            try:
                data = self.api(action="query", list="blocks", bkusers="|".join(chunk), bklimit="max",
                                bkprop="user|by|reason|timestamp|expiry")
            except ApiError as exc:
                log.debug("Block lookup failed: %s", exc)
                continue
            for block in data.get("query", {}).get("blocks", []):
                blocked[block.get("user")] = {key: block.get(key) for key in ("reason", "by", "timestamp", "expiry")}
        return blocked

    def deletion_reason(self, title):
        data = self.api(action="query", list="logevents", letype="delete", leaction="delete/delete",
                        letitle=title, lelimit=1, leprop="comment|user|timestamp")
        events = data.get("query", {}).get("logevents", [])
        return events[0].get("comment", "") if events else None

    def categories(self, titles):
        """Visible Wikipedia categories of pages: {title: [category names]}."""
        found = {}
        for chunk in _chunks(sorted(set(titles)), 50):
            for data in self.api_pages(action="query", prop="categories", titles="|".join(chunk),
                                       clshow="!hidden", cllimit="max"):
                for page in data.get("query", {}).get("pages", []):
                    names = [c["title"].split(":", 1)[-1] for c in page.get("categories", [])]
                    found.setdefault(page["title"], []).extend(names)
        return found

    # -- Lift Wing models -----------------------------------------------------

    def revert_risk(self, revid, model):
        """Probability (0-1) that an independent model thinks the edit will be reverted (None if unavailable)."""
        data = self.liftwing(model, {"rev_id": revid, "lang": self.lang})
        try:
            return float(data["output"]["probabilities"]["true"])
        except (TypeError, KeyError, ValueError):
            return None

    def ores_probability(self, revid, model, outcome):
        """One ORES score straight from Lift Wing, e.g. ("goodfaith", "false") = P(bad faith). None if unavailable."""
        data = self.liftwing(f"{self.wiki_db}-{model}", {"rev_id": revid})
        try:
            return float(data[self.wiki_db]["scores"][str(revid)][model]["score"]["probability"][outcome])
        except (TypeError, KeyError, ValueError):
            return None

    def ores_scores(self, revid):
        """(P(bad faith), P(damaging)), for edits RecentChanges hasn't scored."""
        return self.ores_probability(revid, "goodfaith", "false"), self.ores_probability(revid, "damaging", "true")

    def article_topics(self, title):
        """[(topic, score)] from the article-topic model, e.g. ("History_and_Society.Politics_and_government", 0.9)."""
        data = self.liftwing("outlink-topic-model", {"page_title": title.replace(" ", "_"), "lang": self.lang})
        try:
            return [(r["topic"], float(r["score"])) for r in data["prediction"]["results"]]
        except (TypeError, KeyError, ValueError):
            return None
