"""
Settings for the Wikipedia Vandalism Tracker.

Change a value, then restart the app (stop it and run main.py again).
When running the .exe, put changes in settings.json next to the .exe instead (see the bottom).
"""
import json
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Your contact info  (this decides how fast the tracker can work)
# ---------------------------------------------------------------------------
# Wikipedia asks tools to identify themselves with an email address or a URL.
#   - without it, Wikipedia allows only 10 API requests per minute  -> "slow mode"
#   - with it, you get 200 requests per minute                      -> "fast mode"
# Example:  CONTACT = "you@example.com"   or   CONTACT = "https://github.com/yourname"
CONTACT = ""

# ---------------------------------------------------------------------------
# What to scan: the same feed as
# https://en.wikipedia.org/wiki/Special:RecentChanges?hidebots=1&hidecategorization=1&hideWikibase=1&limit=50&days=1
# (bots, category changes and Wikidata changes are left out)
# ---------------------------------------------------------------------------
WIKI_DOMAIN = "en.wikipedia.org"
WIKI_LANG = "en"
NAMESPACES = [0]              # 0 = articles
EDIT_MIN_AGE_SECONDS = 30     # wait a moment so Wikipedia's ORES model has scored the edit
BACKFILL_MINUTES = 30         # when the app starts, also look this far back (max 1440 = 1 day)

# ---------------------------------------------------------------------------
# Check 1: bad-faith filter (Wikipedia's own ORES "goodfaith" model)
# ---------------------------------------------------------------------------
BADFAITH_THRESHOLD = 0.50     # an edit becomes a candidate when P(bad faith) >= this

# ---------------------------------------------------------------------------
# Check 2: is it really vandalism?
# ---------------------------------------------------------------------------
# Every candidate is checked again at these ages (minutes after the edit) to see whether
# Wikipedians reverted it, blocked the vandal, or hid/deleted it.
RECHECK_AT_MINUTES = [1, 3, 6, 10, 15, 30, 45, 60, 90, 120]
LIVE_RECHECK_HOURS = 24       # vandalism that is still live keeps being checked hourly for this long
SURVIVAL_MINUTES = 60         # still in the article after this long while others edited it = probably fine
REVERT_RISK_MODEL = "revertrisk-multilingual"   # independent ML model on Wikimedia's Lift Wing

# Request budget per minute for the Wikipedia API, kept well under Wikipedia's limits.
SLOW_MODE = {"api_per_minute": 8, "poll_seconds": 30, "recheck_seconds": 60}
FAST_MODE = {"api_per_minute": 60, "poll_seconds": 15, "recheck_seconds": 20}

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
HOST = "127.0.0.1"
PORT = 5000
OPEN_BROWSER = True           # open the dashboard in your browser when the app starts
KEEP_DAYS = 14                # forget non-favourite edits older than this (favourites are kept forever)

# ---------------------------------------------------------------------------
# Where the app keeps its files: next to main.py, or next to the .exe
# ---------------------------------------------------------------------------
FROZEN = getattr(sys, "frozen", False)   # True when running as a .exe built by build_exe.py
APP_DIR = Path(sys.executable).parent if FROZEN else Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
DB_PATH = DATA_DIR / "vandalism.db"

# A .exe can't be edited, so settings.json next to it can override any setting above,
# e.g. {"CONTACT": "you@example.com", "PORT": 5001}. It works next to main.py as well.
SETTINGS_FILE = APP_DIR / "settings.json"
SETTINGS_WHERE = "settings.json (next to the .exe)" if FROZEN else "config.py"
try:
    if FROZEN and not SETTINGS_FILE.exists():
        SETTINGS_FILE.write_text(json.dumps({"CONTACT": ""}, indent=2) + "\n", encoding="utf-8")
    if SETTINGS_FILE.exists():
        overrides = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        globals().update({key: value for key, value in overrides.items() if key.isupper()})
except (OSError, ValueError, AttributeError) as exc:
    print(f"Couldn't use {SETTINGS_FILE}: {exc} (using the default settings)")
