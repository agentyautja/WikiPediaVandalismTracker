# Wikipedia Vandalism Tracker

A small app that watches English Wikipedia's
[RecentChanges](https://en.wikipedia.org/wiki/Special:RecentChanges?hidebots=1&hidecategorization=1&hideWikibase=1&limit=50&days=1&urlversion=2)
feed live, picks out **bad-faith edits**, **double-checks** that they really are vandalism, and shows the
confirmed ones that Wikipedia has cleaned up in a dashboard in your browser, with links to the vandalized
versions, favourites and category filters.

Just for fun: it only reads Wikipedia, it never edits anything.

## Download (Windows)

**[⬇ Download WikipediaVandalismTracker.exe](https://github.com/agentyautja/WikiPediaVandalismTracker/releases/latest/download/WikipediaVandalismTracker.exe)**
(no Python needed), or see all versions on the [Releases page](https://github.com/agentyautja/WikiPediaVandalismTracker/releases).

Put it in a folder of your choice and double-click it. The dashboard opens in your browser. On first run it creates
`settings.json` next to itself: add your email address there (`{"CONTACT": "you@example.com"}`) for
[fast mode](#speed-slow-mode-vs-fast-mode). Windows may say it protected your PC, because the .exe isn't signed:
click *More info → Run anyway*.

## Running from source

1. Open the project in PyCharm (it already uses the `.venv` interpreter).
2. Install the two dependencies, either through PyCharm's *"Install requirements"* prompt or with
   `pip install -r requirements.txt`.
3. **Recommended:** open `config.py` and put your email address or website in `CONTACT`
   (see [Speed](#speed-slow-mode-vs-fast-mode)).
4. Run `main.py`. The dashboard opens at <http://127.0.0.1:5000>.

The tracker keeps running as long as `main.py` does. On start-up it also looks back 30 minutes, so you get
results within a few minutes.

## Making a .exe

Run `build_exe.py` (▶ in PyCharm). It installs PyInstaller if needed and builds
`dist/WikipediaVandalismTracker.exe`: one file (about 14 MB) that runs on any Windows PC without Python.

- Put the .exe in a normal folder (not *Program Files*) and double-click it. A console window shows the log and the
  dashboard opens in your browser. **Closing the window stops the tracker.**
- It keeps its database in a `data` folder and its settings in `settings.json`, both next to the .exe. Put your
  contact in `settings.json` (`{"CONTACT": "you@example.com"}`); any other setting from `config.py` can go there too.
- Double-clicking it again while it's running just opens the dashboard.
- Its icon is `icon.ico` (the red "W" shield from the dashboard). Replace that file with any other `.ico` and rebuild
  to change it.
- Rebuild after changing the code. Windows SmartScreen may warn about an unknown app the first time, because it isn't
  signed: click *More info → Run anyway*.

## How it decides what's vandalism

| Step | What happens |
|---|---|
| **Scan** | Every 15–30 s it reads the new entries in RecentChanges, with the same filters as the link above (no bots, no category changes, no Wikidata), for articles. It reads *all* of them, not just the latest 50. |
| **Check 1: bad faith** | Wikipedia's own ORES *good-faith* model scores every edit. Edits with ≥ 50 % chance of bad faith become candidates (`BADFAITH_THRESHOLD`). |
| **Check 2: is it really vandalism?** | Runs on **every** candidate, then **re-checks it** at 1, 3, 6, 10, 15, 30, 45, 60, 90 and 120 minutes after the edit (live vandalism keeps being checked hourly for a day). |

Check 2 combines independent evidence:

- **The text itself.** A word-level diff of what was added or removed: swearing, slurs, insults, "hi mom / was here",
  chat speak, meme words, keyboard mashing, repeated characters, SHOUTING, emoji spam, page blanking… Words that
  were already in the article don't count, so an article *about* a rude word isn't flagged.
- **Wikipedia's edit filters.** Tags such as "possible vandalism", "blanking" or "reverting anti-vandal bot".
- **A second, independent model.** Wikimedia's *revert-risk* model (via Lift Wing).
- **How Wikipedians reacted.** Reverted by ClueBot NG, by an anti-vandalism tool (Huggle, Twinkle, Interceptor…),
  or with "rvv/vandalism" in the summary? Was the vandal blocked? Did admins hide the revision or delete the page?
  A revert that calls the edit a *good-faith* mistake counts **against** vandalism.

Verdicts:

- **Confirmed**: Wikipedia treated it as vandalism, or it was reverted and our own checks agree.
- **Likely**: nobody has acted yet, but the text looks like vandalism and the revert-risk model agrees.
- **Under review**: passed check 1, and check 2 is still waiting for evidence.
- **Rejected**: check 2 says it isn't vandalism (a good-faith mistake, or it survived an hour of other people editing the page).

The dashboard only shows **Confirmed** vandalism that Wikipedia has **already cleaned up** (reverted, hidden or
deleted). Everything else is tracked in the background and appears once it's confirmed and cleaned up.

## Links

- The main button opens the *vandalized version*: a permanent link to the page exactly as the vandal left it.
- Every card also has the **diff**, the current (cleaned-up) page, and the editor's contributions.

## Dashboard

- Tabs: *All* and *★ Favourites*.
- Category chips (Politics, Science, Sports, Music, Film & TV, Geography, People…), search and sort.
- Click ☆ to favourite an edit. Favourites are kept forever; other edits are forgotten after `KEEP_DAYS`.
- *↻ Re-check now* runs check 2 again straight away.

Categories come from Wikimedia's article-topic model, with the article's own Wikipedia categories as a fallback.

## Speed: slow mode vs fast mode

Wikipedia limits API use to **10 requests per minute** for tools that don't say who they are, and
**200 per minute** when the tool's User-Agent contains contact info. So:

- `CONTACT = ""` → *slow mode*: everything works, but checks queue up and results take longer.
- `CONTACT = "you@example.com"` (or a URL) → *fast mode*.

The tracker always stays below the limit and waits when Wikipedia asks it to (HTTP 429 / `Retry-After`).

## Files

```
main.py            start here
config.py          all settings
build_exe.py       builds dist/WikipediaVandalismTracker.exe
icon.ico           the .exe's icon
tracker/
  wiki.py          Wikipedia API + Lift Wing client (rate limiting, retries)
  monitor.py       the background loop: scan → check 1 → check 2 → re-checks
  diffing.py       word-level diff of two revisions
  heuristics.py    "does the text look like vandalism?" rules
  community.py     reading reverts, blocks and deletions
  verdict.py       combines all evidence into a verdict
  topics.py        article → broad categories
  db.py            SQLite storage (data/vandalism.db)
  web.py           Flask JSON API for the dashboard
static/            the dashboard (HTML, CSS, JS)
```

Heads-up: vandalism is often rude, and the dashboard shows exactly what the vandal wrote.
