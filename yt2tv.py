#!/usr/bin/env python3
"""
yt2tv.py -- download YouTube videos/playlists as files that actually PLAY
on customers' TVs, USB media players and car screens.

Why this exists:
  yt-dlp's default "best" gives VP9 or AV1 video + Opus audio. Passing
  --merge-output-format mp4 only changes the CONTAINER -- the codecs inside
  stay VP9/Opus, and a cheap LED/LCD reads codecs, not the file extension.
  Result: "unsupported file". IDM never had this problem because it can only
  grab YouTube's progressive streams, which are always H.264+AAC (but capped
  at 720p).

  So: force H.264 + AAC, verify with ffprobe, and re-encode only when the
  downloaded stream is genuinely outside what the target device can decode.
"""

import json
import os
import re
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

try:
    import yt_dlp
except ImportError:
    sys.exit("yt-dlp is missing.  Install it with:  pip install -U yt-dlp")

try:
    import setup as bootstrap            # ffmpeg / Deno installers, reused
except ImportError:                      # pragma: no cover
    bootstrap = None

try:
    # Optional -- gives readable filenames for non-Latin titles. The
    # "type: ignore" is for editors that indexed this folder before the
    # package was installed and keep reporting it as missing; the fallback
    # below means a genuinely absent package is harmless anyway.
    from unidecode import unidecode  # type: ignore
except ImportError:
    unidecode = None

def _base_dir():
    """
    Where bin\\, cookies.txt and Downloads\\ live.

    Frozen into an exe, __file__ points inside PyInstaller's temporary
    extraction folder, which is wiped on exit. The folder holding the exe is
    what the user actually sees, so anchor everything there instead.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


HERE = _base_dir()
OUT_DIR = HERE / "Downloads"
RAW_DIR = HERE / "_raw"

# FAT32 refuses single files larger than this, and most USB sticks are FAT32.
FAT32_LIMIT = 4 * 1024 ** 3

# H.264 profile ranking -- a device that decodes one profile also decodes
# everything below it.
PROFILE_RANK = {
    "constrained baseline": 0,
    "baseline": 0,
    "main": 1,
    "high": 2,
    "high 10": 3,
    "high 4:2:2": 3,
    "high 4:4:4 predictive": 3,
}

DEVICE_PROFILES = {
    "1": {
        "name": "TV-Safe 720p",
        "blurb": "H.264 Main + AAC. Plays on practically every LED/LCD TV and USB player.",
        "height": 720,
        "x264_profile": "main",
        "level": 31,
        "max_fps": 30,
        "abr": "128k",
        "crf": "20",
    },
    "2": {
        "name": "Smart TV 1080p",
        "blurb": "H.264 High + AAC. For TVs from roughly 2014 onward.",
        "height": 1080,
        "x264_profile": "high",
        "level": 41,
        "max_fps": 60,
        "abr": "192k",
        "crf": "21",
    },
    "3": {
        "name": "Basic 480p",
        "blurb": "H.264 Baseline + AAC. Last resort for very old players and car screens.",
        "height": 480,
        "x264_profile": "baseline",
        # YouTube only ever serves Main at 480p, never Baseline. Demanding
        # Baseline here would force a full re-encode on every single download
        # for a compatibility gain that barely exists any more -- Main has
        # been decodable by essentially every chip since the mid-2000s. So
        # accept Main as-is (instant remux), and only fall back to encoding
        # Baseline when the source is VP9/AV1 and we must re-encode anyway.
        "accept_profile": "main",
        "level": 30,
        # Same reasoning for the level: YouTube tags 480p as L3.1, and L3.1 is
        # universally supported (it is what plain 720p needs). Accept it rather
        # than re-encoding just to relabel it as L3.0.
        "accept_level": 31,
        "max_fps": 30,
        "abr": "128k",
        "crf": "21",
        # Round the width to a multiple of 16 so it lands on whole H.264
        # macroblocks. A 16:9 source would otherwise scale to 854 wide, which
        # forces the encoder to pad and signal a crop -- something the cheap
        # decoders this profile targets sometimes render with green edges.
        # 848 instead of 854 is a 0.6% aspect change, invisible in practice.
        "align": 16,
    },
    "4": {
        "name": "Original (no conversion)",
        "blurb": "Whatever YouTube has, untouched. Best quality, PC playback only.",
        "height": None,
        "x264_profile": None,
        "level": None,
        "max_fps": None,
        "abr": None,
        "crf": None,
    },
}

# Audio-only jobs. MP3 rather than the m4a YouTube actually serves, because
# car stereos, cheap USB players and old phones all read MP3 and a fair
# number still refuse m4a. 192 kbps CBR at 44.1 kHz is the combination those
# players are happiest with.
AUDIO_PROFILE = {
    "name": "Audio only (MP3)",
    "audio": True,
    "height": None,
    "abr": "192k",
}

FFMPEG = None
FFPROBE = None

# YouTube increasingly answers plain requests with "Sign in to confirm you're
# not a bot", especially once you download in volume. Borrowing cookies from
# an installed browser is the fix; these are tried in order.
COOKIE_BROWSERS = ["firefox", "chrome", "edge", "brave", "chromium", "opera", "vivaldi"]
COOKIE_BROWSER = None

# Chrome/Edge 127+ on Windows encrypt their cookie store with App-Bound
# Encryption, which yt-dlp cannot read no matter whether the browser is
# running. An exported cookies.txt is the way round that, so if one is sitting
# beside this script we use it in preference to everything else.
COOKIE_FILE = HERE / "cookies.txt"

BOT_WALL = ("sign in to confirm", "not a bot", "confirm your age",
            "please sign in", "unable to download api page")

# A Windows console is usually cp1252, which cannot encode Hindi, Urdu or
# Arabic titles -- printing one would raise UnicodeEncodeError and kill the
# whole run. Replace unencodable characters instead of dying.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass


# ---------------------------------------------------------------------------
# tool discovery
# ---------------------------------------------------------------------------

def find_tool(name):
    """Prefer the copy bundled in ./bin so this folder stays portable."""
    local = HERE / "bin" / (name + ".exe")
    if local.exists():
        return str(local)
    from shutil import which
    found = which(name)
    if not found:
        sys.exit("%s not found. Expected it at %s or on your PATH." % (name, local))
    return found


def ensure_js_runtime():
    """
    YouTube guards its stream URLs with a JavaScript challenge, and yt-dlp
    needs a JS runtime (Deno) to solve it. Without one the signature solving
    fails, the formats come back with no URLs, and YouTube reports it as
    "The page needs to be reloaded." -- which looks nothing like the real
    cause, so we check up front.

    winget drops deno in a versioned Packages folder rather than on PATH, so
    look there too and splice it in for this process.
    """
    from shutil import which
    if which("deno"):
        return True

    candidates = [HERE / "bin" / "deno.exe"]
    local_app = os.environ.get("LOCALAPPDATA")
    if local_app:
        winget = Path(local_app) / "Microsoft" / "WinGet"
        candidates.append(winget / "Links" / "deno.exe")
        candidates.extend(sorted((winget / "Packages").glob("DenoLand.Deno*/deno.exe")))
    home = os.environ.get("USERPROFILE")
    if home:
        candidates.append(Path(home) / ".deno" / "bin" / "deno.exe")

    for deno in candidates:
        if deno.exists():
            os.environ["PATH"] = str(deno.parent) + os.pathsep + os.environ.get("PATH", "")
            return True
    return False


def ensure_dependencies():
    """
    Fetch anything missing before we need it.

    Running from the exe there is no Download.bat to run setup first, so the
    first launch on a new PC has to fetch ffmpeg and Deno itself. Costs
    nothing once they are present.
    """
    from shutil import which

    need_ffmpeg = not ((HERE / "bin" / "ffmpeg.exe").exists()
                       and (HERE / "bin" / "ffprobe.exe").exists()) \
        and not (which("ffmpeg") and which("ffprobe"))
    need_deno = not ensure_js_runtime()

    if not (need_ffmpeg or need_deno):
        return

    if bootstrap is None:
        return                              # find_tool will report it plainly

    print("\n  First run on this PC -- setting things up.\n")
    if need_ffmpeg:
        bootstrap.install_ffmpeg()
    if need_deno:
        bootstrap.install_deno()
        ensure_js_runtime()                 # pick up the freshly installed one
    print()


# ---------------------------------------------------------------------------
# filenames
# ---------------------------------------------------------------------------

def ascii_name(title, fallback):
    """
    Cheap TV firmware reading a FAT32 stick chokes on non-ASCII filenames,
    and plenty of YouTube titles are full of them. Strip down to plain ASCII.
    """
    if unidecode is not None:
        # Transliterates Hindi, Urdu, Arabic, Chinese etc. into readable Latin
        # instead of deleting them, so the filename still means something to
        # whoever is looking at the stick.
        flat = unidecode(title)
    else:
        flat = unicodedata.normalize("NFKD", title)
        flat = flat.encode("ascii", "ignore").decode("ascii")
    flat = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", flat)
    flat = re.sub(r"[^\w\s.\-()&,']", " ", flat)
    flat = re.sub(r"\s+", " ", flat).strip(" .")
    if len(re.sub(r"[^A-Za-z0-9]", "", flat)) < 3:
        return "video_" + str(fallback)
    return flat[:90].strip(" .")


# ---------------------------------------------------------------------------
# inspection
# ---------------------------------------------------------------------------

def probe_media(path):
    out = subprocess.run(
        [FFPROBE, "-v", "quiet", "-print_format", "json",
         "-show_streams", "-show_format", str(path)],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        return None
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError:
        return None
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    try:
        duration = float((data.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    return {"video": video, "audio": audio, "duration": duration}


def frame_rate(stream):
    raw = stream.get("avg_frame_rate") or stream.get("r_frame_rate") or "0/1"
    try:
        num, den = raw.split("/")
        return float(num) / float(den) if float(den) else 0.0
    except (ValueError, ZeroDivisionError):
        return 0.0


def video_is_ok(v, target):
    """True when the video stream already plays on the target device as-is."""
    if v is None:
        return False
    if v.get("codec_name") != "h264":
        return False
    if v.get("pix_fmt") != "yuv420p":                  # 10-bit / 4:2:2 breaks TVs
        return False
    # What we tolerate without re-encoding can be looser than what we encode
    # to when a re-encode is unavoidable -- see "accept_profile" below.
    accept = target.get("accept_profile") or target["x264_profile"]
    rank = PROFILE_RANK.get(str(v.get("profile") or "").lower())
    if rank is None or rank > PROFILE_RANK[accept]:
        return False
    try:
        level = int(v.get("level") or 99)
    except (TypeError, ValueError):
        return False
    if level > (target.get("accept_level") or target["level"]):
        return False
    if int(v.get("height") or 0) > target["height"]:
        return False
    if frame_rate(v) > target["max_fps"] + 1:
        return False
    return True


def audio_is_ok(a, target):
    if a is None:
        return False
    if a.get("codec_name") != "aac":                   # Opus is the usual culprit
        return False
    if int(a.get("channels") or 0) > 2:                # 5.1 confuses cheap players
        return False
    return True


# ---------------------------------------------------------------------------
# conversion
# ---------------------------------------------------------------------------

def run_ffmpeg(cmd, duration, label):
    """
    Run ffmpeg while reporting how far along it is.

    Re-encoding a 45-minute episode takes minutes. Without this the screen
    sits on a single frozen line and looks like it has hung, which is the
    single most common reason to think the tool has crashed.

    stderr goes to a temporary file rather than a pipe: reading only stdout
    while stderr fills its OS buffer would deadlock.
    """
    import tempfile

    with tempfile.TemporaryFile(mode="w+", encoding="utf-8",
                                errors="replace") as errors:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errors,
                                text=True, bufsize=1)
        speed, micros = "", 0
        for line in proc.stdout:
            key, _, value = line.strip().partition("=")
            value = value.strip()
            if key == "speed":
                speed = value
            elif key in ("out_time_us", "out_time_ms"):
                # Both carry microseconds; out_time_ms is a historical
                # misnomer. ffmpeg emits both, so only record here.
                if value.lstrip("-").isdigit():
                    micros = int(value)
            elif key == "progress" and duration > 0:
                # One "progress=" line closes each block, so printing here
                # gives exactly one update per block rather than one per key.
                pct = min(100.0, max(0.0, micros / 1e6 / duration * 100))
                print("\r    %s %5.1f%%   %-8s      " % (label, pct, speed),
                      end="", flush=True)
        proc.stdout.close()
        proc.wait()
        errors.seek(0)
        return proc.returncode, errors.read()


def make_compatible(src, dst, target, title=None, artist=None):
    """
    Remux when the streams are already fine (instant, zero quality loss),
    and re-encode only the stream that actually needs it.
    """
    info = probe_media(src)
    if info is None:
        return None, "ffprobe could not read the downloaded file"

    v, a = info["video"], info["audio"]
    v_ok, a_ok = video_is_ok(v, target), audio_is_ok(a, target)

    # -progress writes machine-readable status to stdout so we can show how
    # far along a long re-encode is.
    cmd = [FFMPEG, "-y", "-v", "error", "-nostats",
           "-progress", "pipe:1", "-i", str(src)]

    if v_ok:
        cmd += ["-c:v", "copy"]
    else:
        cmd += [
            "-c:v", "libx264",
            "-profile:v", target["x264_profile"],
            "-level", "%.1f" % (target["level"] / 10.0),
            "-pix_fmt", "yuv420p",
            "-crf", target["crf"],
            # "medium" spends minutes shaving a few MB off a file headed for a
            # USB stick. At the counter, time matters far more than size.
            "-preset", "veryfast",
            "-g", "60",                                # helps seeking on dumb players
        ]
        height = int((v or {}).get("height") or 0)
        if height > target["height"]:
            # A negative width tells ffmpeg to keep the aspect ratio and round
            # to a multiple of that number -- 2 normally, 16 where macroblock
            # alignment buys extra decoder compatibility.
            cmd += ["-vf", "scale=-%d:%d" % (target.get("align", 2),
                                             target["height"])]
        if frame_rate(v or {}) > target["max_fps"] + 1:
            cmd += ["-r", str(target["max_fps"])]

    if a_ok:
        cmd += ["-c:a", "copy"]
    else:
        cmd += ["-c:a", "aac", "-b:a", target["abr"], "-ar", "48000", "-ac", "2"]

    # Carry the original title in the file's own metadata. Explorer and most
    # players show it, so a heavily transliterated filename no longer loses
    # what the video was actually called -- which is what the old titles.txt
    # index existed to preserve.
    if title:
        cmd += ["-metadata", "title=%s" % title]
    if artist:
        cmd += ["-metadata", "artist=%s" % artist]

    # Keep the container headers 32-bit.
    #
    # Left alone, ffmpeg picks a movie timescale from the audio rate -- 5644800
    # here. The duration in those units passes 2^32 after about 760 seconds, so
    # anything over ~12.7 minutes gets version 1 (64-bit) mvhd and elst boxes.
    # Basic USB players on TVs only parse version 0 and sit on "loading"
    # forever. A timescale of 1000 keeps a 24-hour file inside 32 bits.
    #
    # Verified against the same episode fetched by IDM, which plays on the TV:
    # it uses exactly these -- mvhd v0, timescale 1000, no btrt.
    cmd += ["-movie_timescale", "1000",   # 32-bit headers, matches IDM
            "-use_editlist", "0",         # drop the edit list rather than emit v1
            "-write_btrt", "0"]           # extra box IDM's file does not carry

    cmd += ["-movflags", "+faststart", str(dst)]       # moov atom to the front

    if v_ok and a_ok:
        action = "remuxed (already compatible, no quality lost)"
    elif v_ok:
        action = "audio converted to AAC (video untouched)"
    elif a_ok:
        action = "video converted to H.264"
    else:
        action = "video and audio both converted"

    label = "remuxing" if (v_ok and a_ok) else "converting"
    code, errors = run_ffmpeg(cmd, info.get("duration") or 0.0, label)
    if code != 0:
        return None, (errors or "ffmpeg failed").strip()[-400:]
    return action, None


def make_mp3(src, dst, target, title=None, artist=None):
    """
    Turn the downloaded audio into an MP3.

    ID3v2.3 rather than the newer 2.4 -- plenty of car stereos and older
    players show nothing at all for 2.4 tags, and a blank display on the
    dashboard is exactly the sort of thing that comes back as a complaint.
    """
    probe = probe_media(src) or {}
    duration = probe.get("duration") or 0.0

    cmd = [FFMPEG, "-y", "-v", "error", "-nostats", "-progress", "pipe:1",
           "-i", str(src),
           "-vn",                          # drop cover art / any video stream
           "-c:a", "libmp3lame",
           "-b:a", target["abr"],
           "-ar", "44100",
           "-ac", "2",
           "-id3v2_version", "3"]
    if title:
        cmd += ["-metadata", "title=%s" % title]
    if artist:
        cmd += ["-metadata", "artist=%s" % artist]
    cmd += [str(dst)]

    code, errors = run_ffmpeg(cmd, duration, "making mp3")
    if code != 0:
        return None, (errors or "ffmpeg failed").strip()[-400:]
    return "converted to MP3 %s" % target["abr"], None


def verify(path):
    info = probe_media(path)
    if not info:
        return "could not verify"

    if not info.get("video"):                       # audio-only file
        a = info.get("audio") or {}
        if not a:
            return "could not verify"
        kbps = ""
        try:
            kbps = " %dk" % (int(a.get("bit_rate") or 0) / 1000)
        except (TypeError, ValueError):
            pass
        return "%s%s %sHz %sch" % (a.get("codec_name"), kbps,
                                   a.get("sample_rate"), a.get("channels"))

    v = info["video"]
    a = info.get("audio") or {}
    return "%s %s@L%s %sp %s / %s %sch" % (
        v.get("codec_name"), v.get("profile"), v.get("level"),
        v.get("height"), v.get("pix_fmt"),
        a.get("codec_name"), a.get("channels"),
    )


# ---------------------------------------------------------------------------
# youtube
# ---------------------------------------------------------------------------

def add_cookies(opts, browser=None):
    """
    Attach a cookie source. An exported cookies.txt beside this script wins,
    because it is the only method that survives Chrome/Edge App-Bound
    Encryption on Windows.
    """
    if browser:                                   # explicit probe during fallback
        opts["cookiesfrombrowser"] = (browser,)
    elif COOKIE_FILE.exists():
        opts["cookiefile"] = str(COOKIE_FILE)
    elif COOKIE_BROWSER:
        opts["cookiesfrombrowser"] = (COOKIE_BROWSER,)
    return opts


def looks_like_bot_wall(exc):
    text = str(exc).lower()
    return any(phrase in text for phrase in BOT_WALL)


def explain_cookie_error(exc):
    """Turn yt-dlp's cookie errors into something a person can act on."""
    text = str(exc).lower()
    if "could not find" in text or "no such file" in text:
        return "not installed, or never logged in"
    if "could not copy" in text or "cookie database" in text or "locked" in text:
        return "database locked or App-Bound encrypted -- use cookies.txt"
    if "unsupported platform" in text or "permission" in text:
        return "no permission to read its cookies"
    return str(exc).splitlines()[0][:58]


def fetch_info(url, flat=False, browser=None):
    opts = {"quiet": True, "no_warnings": True}
    if flat:
        opts["extract_flat"] = "in_playlist"
    with yt_dlp.YoutubeDL(add_cookies(opts, browser)) as ydl:
        return ydl.extract_info(url, download=False)


def resolve_cookies(url, flat=True):
    """
    YouTube hit us with the bot wall. Walk the installed browsers until one
    hands over usable cookies, then remember it for the rest of the session.
    """
    global COOKIE_BROWSER
    print("\n  YouTube asked for sign-in verification.")
    if COOKIE_FILE.exists():
        print("  (cookies.txt was used and still got refused -- it may be stale;")
        print("   export it again while logged in to YouTube.)")
    print("  Trying to borrow cookies from your browsers...\n")
    for browser in COOKIE_BROWSERS:
        try:
            info = fetch_info(url, flat=flat, browser=browser)
        except Exception as exc:
            print("    %-9s -- %s" % (browser, explain_cookie_error(exc)))
            continue
        print("    %-9s -- works\n" % browser)
        COOKIE_BROWSER = browser
        return info
    return None


def available_heights(info):
    """Heights offered for this video, noting which come as native H.264."""
    seen = {}
    for f in info.get("formats") or []:
        h = f.get("height")
        if not h or f.get("vcodec") in (None, "none"):
            continue
        native = str(f.get("vcodec") or "").startswith("avc1")
        seen[h] = seen.get(h, False) or native
    return sorted(seen.items(), reverse=True)


def format_selector(max_height):
    """
    Ask for H.264+AAC first -- when YouTube has it the file needs only a
    remux and finishes in seconds. Fall back to VP9/AV1 and let ffmpeg
    convert, rather than failing outright.
    """
    cap = "[height<=%d]" % max_height if max_height else ""
    return (
        "bestvideo[vcodec^=avc1]%(c)s+bestaudio[acodec^=mp4a]/"
        "bestvideo[vcodec^=avc1]%(c)s+bestaudio/"
        "bestvideo%(c)s+bestaudio/"
        "best%(c)s/best" % {"c": cap}
    )


def hook(d):
    if d["status"] == "downloading":
        pct = (d.get("_percent_str") or "").strip()
        spd = (d.get("_speed_str") or "").strip()
        # Video and audio arrive as two separate passes; say which, otherwise
        # it looks like the download restarted from zero.
        info = d.get("info_dict") or {}
        kind = "audio" if (info.get("vcodec") or "none") == "none" else "video"
        print("\r    %s %7s  %12s      " % (kind, pct, spd), end="", flush=True)
    elif d["status"] == "finished":
        print("\r    download complete, checking codecs...          ", end="", flush=True)


def download_one(url, target, index=None, dest=None, pad=2):
    dest = dest or OUT_DIR
    RAW_DIR.mkdir(exist_ok=True)
    dest.mkdir(parents=True, exist_ok=True)

    if target.get("audio"):
        # Prefer YouTube's AAC track as the source: it is the one served most
        # reliably, and it transcodes to MP3 cleanly.
        wanted = "bestaudio[acodec^=mp4a]/bestaudio/best"
    else:
        wanted = format_selector(target["height"])

    opts = {
        "format": wanted,
        "merge_output_format": "mp4",
        "outtmpl": str(RAW_DIR / "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,          # else yt-dlp's own bar fights our hook
        "noplaylist": True,
        "progress_hooks": [hook],
        "ffmpeg_location": str(Path(FFMPEG).parent),
        "retries": 5,
        "fragment_retries": 10,
        # Pause briefly between videos. Hammering YouTube flat out through a
        # long playlist is the surest way to trigger the bot wall, and a few
        # seconds per video costs far less than getting blocked mid-job.
        "sleep_interval": 2,
        "max_sleep_interval": 5,
    }

    with yt_dlp.YoutubeDL(add_cookies(opts)) as ydl:
        info = ydl.extract_info(url, download=True)
        raw = Path(ydl.prepare_filename(info))

    if not raw.exists():                               # extension may differ after merge
        candidates = list(RAW_DIR.glob(info["id"] + ".*"))
        if not candidates:
            return None, "downloaded file vanished"
        raw = candidates[0]

    stem = ascii_name(info.get("title") or "", info["id"])
    if index:
        # Pad to the width of the largest position, otherwise a 100+ video
        # playlist sorts 1, 10, 100, 2 on the TV's file browser.
        stem = "%0*d - %s" % (pad, index, stem)
    if target.get("audio"):
        final = dest / (stem + ".mp3")
        action, err = make_mp3(
            raw, final, target,
            title=info.get("title"),
            artist=info.get("uploader") or info.get("channel"))
        try:
            raw.unlink()
        except OSError:
            pass
        return (None, err) if err else ([final], action)

    final = dest / (stem + ".mp4")

    if target["height"] is None:                       # "Original" -- keep untouched
        kept = final.with_suffix(raw.suffix)
        raw.replace(kept)
        return [kept], "kept original (not converted)"

    action, err = make_compatible(
        raw, final, target,
        title=info.get("title"),
        artist=info.get("uploader") or info.get("channel"))
    try:
        raw.unlink()
    except OSError:
        pass
    if err:
        return None, err
    return [final], action


# ---------------------------------------------------------------------------
# interactive flow
# ---------------------------------------------------------------------------

def ask(prompt, valid, default=None):
    valid = list(valid)
    while True:
        raw = input(prompt).strip()
        if not raw and default is not None:
            return default
        if raw in valid:
            return raw
        print("    Please enter one of: %s" % ", ".join(v for v in valid if v))


def parse_selection(text, total):
    """
    Turn '1-5', '3,7,9', '10-' (10 to the end), '-5' (first five) or 'all'
    into a sorted list of 1-based positions. Returns None if it can't be read,
    so the caller can ask again.
    """
    text = (text or "").strip().lower()
    if not text or text == "all":
        return list(range(1, total + 1))

    # Tighten up spacing around dashes first, so "2 - 4" survives the split
    # below instead of becoming three separate chunks.
    text = re.sub(r"\s*-\s*", "-", text)

    picked = set()
    for chunk in re.split(r"[,\s]+", text):
        if not chunk:
            continue
        span = re.fullmatch(r"(\d*)\s*[-to]+\s*(\d*)", chunk)
        if span and (span.group(1) or span.group(2)):
            start = int(span.group(1)) if span.group(1) else 1
            end = int(span.group(2)) if span.group(2) else total
            if start > end:                       # tolerate '10-5'
                start, end = end, start
            picked.update(range(max(1, start), min(total, end) + 1))
        elif chunk.isdigit():
            n = int(chunk)
            if 1 <= n <= total:
                picked.add(n)
        else:
            return None
    return sorted(picked) or None


def describe_selection(picked, total):
    if len(picked) == total:
        return "all %d" % total
    runs, start, prev = [], picked[0], picked[0]
    for n in picked[1:] + [None]:
        if n != prev + 1:
            runs.append(str(start) if start == prev else "%d-%d" % (start, prev))
            start = n
        prev = n
    return "%d of %d (%s)" % (len(picked), total, ", ".join(runs))


def pick_range(total):
    print("\n  This playlist has %d videos." % total)
    print("  Examples:   1-5    |    3,7,9    |    10-  (10 to the end)")
    while True:
        picked = parse_selection(
            input("  Which ones? (Enter = all): "), total)
        if picked:
            print("  Selected: %s" % describe_selection(picked, total))
            return picked
        print("    Could not use that. Valid positions are 1-%d,"
              " e.g.  1-5  or  3,7,9" % total)


def pick_media_type():
    print("\n  What do you want?\n")
    print("    1. Video  (MP4 for a TV or USB player)")
    print("    2. Audio  (MP3 for a phone, car stereo or speaker)")
    print()
    return ask("  Choose 1-2 [default 1]: ", ["1", "2"], "1")


def pick_device_profile():
    print("\n  How should the file be prepared?\n")
    for key in sorted(DEVICE_PROFILES):
        p = DEVICE_PROFILES[key]
        print("    %s. %-26s %s" % (key, p["name"], p["blurb"]))
    print()
    choice = ask("  Choose 1-4 [default 1]: ", DEVICE_PROFILES.keys(), "1")
    return DEVICE_PROFILES[choice]


def show_height(info, target):
    """
    Say what the download will actually be.

    This used to ask a second question, but the profile already sets the
    resolution -- two prompts deciding the same thing is one decision too
    many at a counter. Picking a different resolution means picking a
    different profile.
    """
    heights = available_heights(info)
    if not heights:
        return
    best = heights[0][0]
    getting = min(best, target["height"])
    if best <= target["height"]:
        print("  Best available: %dp  ->  downloading %dp" % (best, getting))
    else:
        print("  Best available: %dp, capped by %s  ->  downloading %dp"
              % (best, target["name"], getting))


def report(path, note):
    size_mb = path.stat().st_size / 1024.0 ** 2
    print("\r    OK  %s" % path.name)
    if note:
        print("        %s" % note)
    print("        %s  |  %.1f MB" % (verify(path), size_mb))
    if path.stat().st_size > FAT32_LIMIT:
        print("        WARNING: over 4 GB -- will not copy onto a FAT32 USB stick.")


def main():
    global FFMPEG, FFPROBE

    print("\n" + "=" * 68)
    print("  yt2tv -- YouTube downloader with real device compatibility")
    print("=" * 68)

    ensure_dependencies()                   # first run on a new PC

    FFMPEG = find_tool("ffmpeg")
    FFPROBE = find_tool("ffprobe")

    if not ensure_js_runtime():
        print("\n  WARNING: no JavaScript runtime (Deno) found.")
        print("  YouTube needs one to unlock stream URLs. Without it downloads")
        print("  fail with the misleading 'The page needs to be reloaded.'")
        print("  Fix:  winget install DenoLand.Deno")
        print("        pip install -U yt-dlp-ejs\n")

    if COOKIE_FILE.exists():
        age_days = (time.time() - COOKIE_FILE.stat().st_mtime) / 86400
        note = "  (%d days old -- re-export if downloads start failing)" % age_days \
            if age_days > 30 else ""
        print("  Signed in via cookies.txt%s" % note)

    url = sys.argv[1] if len(sys.argv) > 1 else input("\n  Paste the YouTube link: ").strip()
    if not url:
        sys.exit("  No link given.")

    print("\n  Reading link...")
    try:
        info = fetch_info(url, flat=True)
    except Exception as exc:
        if not looks_like_bot_wall(exc):
            sys.exit("  Could not read that link: %s" % str(exc).splitlines()[0])
        info = resolve_cookies(url, flat=True)
        if info is None:
            sys.exit(
                "\n  YouTube wants a signed-in session and none was available.\n"
                "\n"
                "  BEST FIX -- export a cookies.txt (works even with Chrome's\n"
                "  App-Bound Encryption, which blocks reading cookies directly):\n"
                "\n"
                "    1. In Chrome, install the extension 'Get cookies.txt LOCALLY'.\n"
                "    2. Open youtube.com and make sure you are logged in.\n"
                "    3. Click the extension -> Export, and save the file as\n"
                "         %s\n"
                "    4. Run this tool again. It picks the file up automatically.\n"
                "\n"
                "  ALTERNATIVE -- install Firefox, log in to YouTube there, and\n"
                "  run this again. Firefox cookies can be read directly.\n"
                "\n"
                "  Chrome 127+ and Edge 127+ encrypt their cookie store, so\n"
                "  closing the browser does NOT help on current versions.\n"
                "  Use a throwaway Google account, not your main one.\n"
                % COOKIE_FILE
            )

    is_playlist = info.get("_type") == "playlist"
    entries = [e for e in (info.get("entries") or []) if e] if is_playlist else []

    if is_playlist:
        print("  Playlist: %s  (%d videos)" % (info.get("title"), len(entries)))
    else:
        print("  Video: %s" % info.get("title"))

    media = pick_media_type()

    picked = pick_range(len(entries)) if is_playlist else None

    if media == "2":
        target = AUDIO_PROFILE          # no resolution questions for audio
    else:
        target = pick_device_profile()
        if not is_playlist and target["height"] is not None:
            show_height(fetch_info(url), target)

    if is_playlist:
        # Keep each video's ORIGINAL playlist position in the filename, so a
        # 5-10 batch stays numbered 05..10 and still sorts correctly next to
        # the rest if the others get downloaded later.
        jobs = [(entries[n - 1].get("url") or entries[n - 1].get("id"),
                 entries[n - 1].get("title") or "?", n) for n in picked]
        # Each playlist gets its own folder, so several playlists on one stick
        # don't end up jumbled together.
        dest = OUT_DIR / ascii_name(info.get("title") or "", "playlist")
        pad = max(2, len(str(len(entries))))
    else:
        jobs = [(url, info.get("title") or "?", None)]
        dest, pad = OUT_DIR, 2

    print("\n  Saving to: %s" % dest)
    print("  Profile:   %s\n" % target["name"])

    ok = 0
    failed = []
    for i, (link, title, pos) in enumerate(jobs, 1):
        label = "  [%d/%d] %s" % (i, len(jobs), title[:60])
        # When only part of a playlist was picked, show the real position too.
        print(label if pos in (None, i) else "%s   (playlist #%d)" % (label, pos))
        try:
            paths, note = download_one(link, target, pos, dest, pad)
        except Exception as exc:
            # The wall can appear partway through a long playlist.
            if looks_like_bot_wall(exc) and COOKIE_BROWSER is None \
                    and resolve_cookies(link, flat=False) is not None:
                try:
                    paths, note = download_one(link, target, pos, dest, pad)
                except Exception as retry_exc:
                    paths, note = None, str(retry_exc).splitlines()[0][:160]
            else:
                paths, note = None, str(exc).splitlines()[0][:160]
        if paths:
            report(paths[0], note)
            for extra in paths[1:]:             # the remaining split parts
                report(extra, "")
            ok += 1
        else:
            print("\r    FAILED  %s" % note)
            failed.append(title)
        print()

    try:
        if RAW_DIR.exists() and not any(RAW_DIR.iterdir()):
            RAW_DIR.rmdir()
    except OSError:
        pass

    print("=" * 68)
    print("  Done: %d of %d ready in %s" % (ok, len(jobs), dest))
    if failed:
        print("  Failed (%d):" % len(failed))
        for t in failed:
            print("    - %s" % t[:60])
    print("=" * 68 + "\n")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n  Stopped.\n")
