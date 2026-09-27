"""Wikipedia Vandalism Tracker.

Run this file (the green ▶ in PyCharm), or the .exe that build_exe.py makes. It starts the tracker
in the background and opens the dashboard at http://127.0.0.1:5000 in your browser.
Settings are in config.py (or settings.json next to the .exe).
Add `--no-browser` to skip opening the browser.
"""

import json
import logging
import sys
import threading
import urllib.request
import webbrowser

import config
from tracker.db import Database
from tracker.monitor import Monitor
from tracker.web import create_app
from tracker.wiki import WikiClient


def already_running(url):
    """Is a tracker already serving the dashboard at this address?"""
    try:
        with urllib.request.urlopen(f"{url}/api/status", timeout=2) as resp:
            return "monitor" in json.load(resp)
    except Exception:
        return False


def main():
    sys.stdout.reconfigure(errors="replace")   # article titles in any script shouldn't crash the console
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    url = f"http://{config.HOST}:{config.PORT}"
    open_browser = config.OPEN_BROWSER and "--no-browser" not in sys.argv
    if already_running(url):   # e.g. the .exe was double-clicked twice
        logging.info("The tracker is already running: %s", url)
        if open_browser:
            webbrowser.open(url)
        return

    profile = config.FAST_MODE if config.CONTACT.strip() else config.SLOW_MODE
    if not config.CONTACT.strip():
        logging.info("Slow mode: Wikipedia allows anonymous tools 10 requests/minute. "
                     "Put your email or website in CONTACT in %s for 200/minute.", config.SETTINGS_WHERE)

    db = Database(config.DB_PATH)
    wiki = WikiClient(config.WIKI_DOMAIN, config.WIKI_LANG, config.CONTACT, profile["api_per_minute"])
    monitor = Monitor(db, wiki, profile)
    monitor.start()

    logging.info("Dashboard: %s  (close this window or stop the program to stop tracking)", url)
    if open_browser:
        threading.Timer(1.5, webbrowser.open, [url]).start()
    create_app(db, monitor, wiki, profile).run(host=config.HOST, port=config.PORT, debug=False,
                                               use_reloader=False, threaded=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        if not config.FROZEN:
            raise
        # A double-clicked .exe closes its window instantly on a crash; keep it open so the error can be read.
        logging.exception("The tracker stopped because of an error")
        input("Press Enter to close this window...")
