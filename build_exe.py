"""Build a standalone Windows program: dist/WikipediaVandalismTracker.exe

Run this file (▶ in PyCharm). It installs PyInstaller into this project's virtual environment
if needed, then packs Python, the tracker and the dashboard into one .exe that runs on any
Windows PC without Python installed.

The .exe keeps its database (data/) and its settings (settings.json) in the folder it is in,
so move it to a normal folder of your choice (not Program Files) before running it.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAME = "WikipediaVandalismTracker"


def main():
    try:
        import PyInstaller.__main__
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])
        import PyInstaller.__main__

    work = ROOT / "build"
    PyInstaller.__main__.run([
        str(ROOT / "main.py"),
        "--name", NAME,
        "--onefile",       # a single .exe file
        "--console",       # a window with the log; closing it stops the tracker
        "--icon", str(ROOT / "icon.ico"),   # swap icon.ico for any other .ico file to change it
        "--noconfirm",
        "--clean",
        "--add-data", f"{ROOT / 'static'}{os.pathsep}static",
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(work),
        "--specpath", str(work),
    ])
    shutil.rmtree(work, ignore_errors=True)
    print(f"\nDone: {ROOT / 'dist' / (NAME + '.exe')}")


if __name__ == "__main__":
    main()
