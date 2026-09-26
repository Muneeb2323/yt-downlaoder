#!/usr/bin/env python3
"""
setup.py -- prepares a fresh Windows PC to run ytshop.

Download.bat calls this automatically before every run. It is cheap and safe
to repeat: anything already installed is detected and skipped, so the normal
case costs about a second.

It installs three things:
  1. Python packages  : yt-dlp, Unidecode, yt-dlp-ejs
  2. ffmpeg + ffprobe : downloaded into bin\\ (not stored in git, ~180 MB)
  3. Deno             : the JavaScript runtime yt-dlp needs to unlock
                        YouTube stream URLs
"""

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
BIN = HERE / "bin"

FFMPEG_URL = ("https://github.com/BtbN/FFmpeg-Builds/releases/download/"
              "latest/ffmpeg-master-latest-win64-gpl.zip")

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass


def step(n, total, text):
    print("\n[%d/%d] %s" % (n, total, text))


def ok(text):
    print("      OK  %s" % text)


def warn(text):
    print("      !!  %s" % text)


# ---------------------------------------------------------------------------
# 1. Python packages
# ---------------------------------------------------------------------------

def install_packages():
    missing = []
    for module, package in (("yt_dlp", "yt-dlp"),
                            ("unidecode", "Unidecode"),
                            ("yt_dlp_ejs", "yt-dlp-ejs")):
        try:
            __import__(module)
        except ImportError:
            missing.append(package)

    if not missing:
        ok("yt-dlp, Unidecode, yt-dlp-ejs already installed")
        return True

    print("      installing: %s" % ", ".join(missing))
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-q", "--upgrade"] + missing)
    if result.returncode != 0:
        warn("pip failed. Try running:  pip install -r requirements.txt")
        return False
    ok("installed " + ", ".join(missing))
    return True


# ---------------------------------------------------------------------------
# 2. ffmpeg / ffprobe
# ---------------------------------------------------------------------------

def have_ffmpeg():
    return (BIN / "ffmpeg.exe").exists() and (BIN / "ffprobe.exe").exists()


def show_progress(done, total):
    if total <= 0:
        return
    pct = done * 100.0 / total
    print("\r      downloading ffmpeg  %5.1f%%  (%d of %d MB)   "
          % (pct, done // 1048576, total // 1048576), end="", flush=True)


def install_ffmpeg():
    if have_ffmpeg():
        ok("ffmpeg and ffprobe already in bin\\")
        return True

    # Falls back to whatever is on PATH rather than downloading again.
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        ok("ffmpeg and ffprobe found on PATH")
        return True

    import urllib.request

    BIN.mkdir(exist_ok=True)
    archive = Path(os.environ.get("TEMP", ".")) / "ffmpeg-ytshop.zip"
    print("      this is a one-time ~180 MB download, please wait...")

    try:
        with urllib.request.urlopen(FFMPEG_URL, timeout=60) as response:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            with archive.open("wb") as out:
                while True:
                    chunk = response.read(1024 * 256)
                    if not chunk:
                        break
                    out.write(chunk)
                    done += len(chunk)
                    show_progress(done, total)
        print()
    except Exception as exc:
        print()
        warn("could not download ffmpeg: %s" % exc)
        warn("Download it yourself from https://www.gyan.dev/ffmpeg/builds/")
        warn("and put ffmpeg.exe and ffprobe.exe into: %s" % BIN)
        return False

    try:
        with zipfile.ZipFile(archive) as zf:
            wanted = {"ffmpeg.exe", "ffprobe.exe"}
            for member in zf.namelist():
                name = member.rsplit("/", 1)[-1]
                if name in wanted:
                    with zf.open(member) as src, (BIN / name).open("wb") as dst:
                        shutil.copyfileobj(src, dst)
                    wanted.discard(name)
                    if not wanted:
                        break
    except Exception as exc:
        warn("could not unpack ffmpeg: %s" % exc)
        return False
    finally:
        try:
            archive.unlink()
        except OSError:
            pass

    if not have_ffmpeg():
        warn("ffmpeg.exe / ffprobe.exe missing after unpacking")
        return False
    ok("ffmpeg and ffprobe installed into bin\\")
    return True


# ---------------------------------------------------------------------------
# 3. Deno (the JS runtime YouTube extraction needs)
# ---------------------------------------------------------------------------

def find_deno():
    if shutil.which("deno"):
        return True
    local_app = os.environ.get("LOCALAPPDATA")
    if local_app:
        winget = Path(local_app) / "Microsoft" / "WinGet"
        if (winget / "Links" / "deno.exe").exists():
            return True
        if any((winget / "Packages").glob("DenoLand.Deno*/deno.exe")):
            return True
    home = os.environ.get("USERPROFILE")
    if home and (Path(home) / ".deno" / "bin" / "deno.exe").exists():
        return True
    return (BIN / "deno.exe").exists()


def install_deno():
    if find_deno():
        ok("Deno already installed")
        return True

    if not shutil.which("winget"):
        warn("winget not available, so Deno can't be installed automatically.")
        warn("Install it from https://deno.com  -- without it YouTube")
        warn("downloads fail with 'The page needs to be reloaded.'")
        return False

    print("      installing Deno via winget...")
    result = subprocess.run(
        ["winget", "install", "--id", "DenoLand.Deno", "--source", "winget",
         "--accept-source-agreements", "--accept-package-agreements",
         "--disable-interactivity"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    if find_deno():
        ok("Deno installed")
        return True
    warn("Deno install did not complete (winget exit %d)." % result.returncode)
    warn("Install it from https://deno.com and run this again.")
    return False


# ---------------------------------------------------------------------------

def main():
    print("=" * 68)
    print("  ytshop setup -- checking what this PC needs")
    print("=" * 68)

    results = []
    step(1, 3, "Python packages")
    results.append(install_packages())
    step(2, 3, "ffmpeg (video conversion)")
    results.append(install_ffmpeg())
    step(3, 3, "Deno (needed to unlock YouTube streams)")
    results.append(install_deno())

    print("\n" + "=" * 68)
    if all(results):
        print("  Ready.")
        print("=" * 68)
        return 0

    print("  Setup incomplete -- see the !! lines above.")
    print("=" * 68)
    return 1


if __name__ == "__main__":
    sys.exit(main())
