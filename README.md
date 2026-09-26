# ytshop

A YouTube downloader that produces files which actually play on customers'
TVs, USB media players and car screens — not just on the PC that made them.

## Why your old files said "unsupported file"

IDM can only grab what the browser requests as a single file: YouTube's
**progressive** streams. There are only two of them, and both are always
**H.264 video + AAC audio in MP4** — a combination every TV, DVD player and
USB stick player has supported since about 2008. That is why IDM files always
worked. The catch is they stop at 720p.

yt-dlp defaults to *best quality*, which on YouTube means **VP9 or AV1 video
with Opus audio**. Here is the trap that caught you:

```
yt-dlp --merge-output-format mp4 <url>
```

That flag does **not** convert anything. ffmpeg simply places the VP9/Opus
streams *inside* an MP4 container. The filename ends in `.mp4`, but the codecs
inside are still VP9 and Opus. A TV reads the codecs, not the extension — so
it refuses the file. Your PC played it fine because VLC and modern browsers
decode VP9 and AV1 in software.

Four more things break playback even when you *do* get H.264:

| Problem | Why it breaks | What this tool does |
|---|---|---|
| H.264 **High** profile above Level 4.1 | Old decoder chips cap at Main@3.1 or High@4.0 | Caps profile and level per device |
| **10-bit** colour (`yuv420p10le`) | TV decoders are 8-bit only | Forces `yuv420p` |
| `moov` atom written at the end | Player needs the index before it can start | Adds `-movflags +faststart` |
| Non-ASCII filename (Hindi, Urdu, emoji) | FAT32 + cheap TV firmware can't read it | Transliterates to plain ASCII |

## What the tool does about it

1. Asks YouTube for **native H.264 + AAC first**. When YouTube has it at your
   chosen resolution, no re-encoding happens at all.
2. Inspects the result with `ffprobe` — codec, profile, level, pixel format,
   frame rate, channel count.
3. If everything is already inside the target device's limits, it **remuxes
   with `-c copy`**: a few seconds, zero quality loss.
4. If only the audio is wrong (the usual case — Opus), it converts **only the
   audio** and copies the video untouched.
5. Only re-encodes the video when it genuinely has to.

That last part matters in a shop: most jobs finish in seconds instead of
minutes, because full re-encoding is the exception, not the rule.

## Usage

Double-click **Download.bat**, or from a terminal:

```
python ytshop.py
python ytshop.py "https://www.youtube.com/watch?v=..."
python ytshop.py "https://www.youtube.com/playlist?list=..."
```

Playlists are detected automatically, and files are numbered `01 - `, `02 - `
so they stay in order on the stick.

### Picking part of a playlist

A playlist first asks which videos you want, so you don't have to take all 200
to get six:

| You type | You get |
|---|---|
| `1-5` | videos 1 to 5 |
| `5-10` | videos 5 to 10 |
| `3,7,9` | just those three |
| `1-3,8,20-22` | mix ranges and singles |
| `10-` | video 10 to the end |
| `-5` | the first five |
| *(Enter)* | all of them |

Spaces are fine (`2 - 4` works), and it confirms the selection before starting
— *"Selected: 6 of 50 (5-10)"*.

Files keep their **original playlist position** in the name. Asking for `5-10`
gives you `05 - …` through `10 - …`, not `01 - …` through `06 - …`, so a batch
downloaded later still sorts correctly alongside the rest.

### Where files end up

A **playlist gets its own folder**, named after the playlist, so several
playlists on one stick don't get jumbled together. A **single video** goes
straight into `Downloads\`.

```
Downloads\
  2026 Total Solar Eclipse\
    01 - 2026 Total Solar Eclipse (Official NASA Trailer).mp4
    02 - Chasing Solar Eclipses With NASA Pilots.mp4
    titles.txt
  Me at the zoo.mp4
  titles.txt
```

`titles.txt` only appears **when it's actually needed** — that is, when the
filename lost something on the way to ASCII:

- a non-Latin title that had to be transliterated (Hindi, Urdu, Arabic,
  Chinese), where `hiNdii gaane` on disk won't tell you much
- accented text where the accents were stripped
- a very long title cut short to fit

English titles survive intact, so no index file is written at all — the
filename already says everything, and there's no stray `.txt` cluttering the
stick. Re-running a playlist won't add duplicate lines to an existing one.

Numbering widens to fit the playlist: under 100 videos you get `01 - `, and
over 100 you get `001 - `, so the TV's file browser sorts them properly
instead of going 1, 10, 100, 2.

## The four device profiles

| # | Profile | Encoding | Use for |
|---|---|---|---|
| 1 | **TV-Safe 720p** | H.264 Main@3.1, AAC 128k | Default. Practically every LED/LCD and USB player |
| 2 | **Smart TV 1080p** | H.264 High@4.1, AAC 192k | TVs from roughly 2014 onward |
| 3 | **Basic 480p** | H.264 Baseline@3.0, AAC 128k | Very old or very cheap players, car screens |
| 4 | **Original** | none | Best quality, PC playback only |

When in doubt at the counter, use **1**. It costs a little sharpness and is
the one that does not come back as a complaint.

## USB stick advice

- Format sticks as **FAT32** for the widest compatibility. **exFAT** if you
  need files over 4 GB, but some older TVs won't mount it — NTFS even less
  often. The tool warns you when a file crosses the 4 GB line.
- Keep files in the **root folder** or one level deep. Some TVs won't recurse.
- 32 GB or smaller sticks mount more reliably on old TVs than large ones.

## "Sign in to confirm you're not a bot"

YouTube throws this at anyone downloading in volume — so in a shop you will
see it regularly. It is not a fault in the tool.

The wall is asking you to be **signed in**. There is no way round that other
than actually being signed in — so the job is to hand yt-dlp a valid login.

When it happens, ytshop automatically tries to borrow cookies from your
installed browsers and carries on with whichever works. It only asks for help
if none do.

### Why "no cookies found" happens on this PC

- **Firefox is not installed**, so there is nothing for it to read.
- **Chrome 127+ and Edge 127+ encrypt their cookie store** with App-Bound
  Encryption. yt-dlp cannot decrypt it — and *closing the browser does not
  help*, which is the usual advice and is simply out of date. This PC runs
  Chrome 153 and Edge 154, so both are affected.

### The fix: export a cookies.txt

This is the reliable route, and it uses the Chrome login you already have.

1. In Chrome, install the extension **"Get cookies.txt LOCALLY"**.
2. Open `youtube.com` and confirm you are logged in.
3. Click the extension → **Export**, and save the file as **`cookies.txt`**
   next to `ytshop.py`.
4. Run the tool again. It detects the file automatically and prints
   *"Signed in via cookies.txt"* at startup.

### How often do I have to do this?

**Once.** The file stays next to `ytshop.py` and is reused for every download
and every playlist. You only redo it when the login behind it stops working.

What kills a cookies.txt, in order of how often it actually happens:

- **Logging out of YouTube in the browser.** This is the big one. Logging out
  invalidates the session on Google's side, so the exported file dies with it
  — even though the file itself looks unchanged.
- Changing your Google password.
- Google rotating the session, which normal browsing can trigger.
- Heavy automated use getting the account flagged.

**The trick that makes it last:** export from a **private / incognito
window**.

1. Open a private window and log in to YouTube there.
2. Export `cookies.txt` from that window.
3. Close the private window **without logging out**.

That session is now detached from your normal browsing, so day-to-day use of
Chrome can't rotate or invalidate it. Exported this way a file commonly lasts
months instead of weeks.

The tool warns you once the file is over 30 days old, but age alone doesn't
mean it's dead — only re-export when downloads actually start failing.

### Alternative

Install **Firefox**, log in to YouTube there, and run the tool again. Firefox
cookies can still be read directly, so no export step is needed.

### Do I need proxies?

No — and Firefox is not a substitute for one, because the two do different
jobs:

| | What it changes | What it fixes |
|---|---|---|
| **Proxy** | your IP address | IP-based rate limiting |
| **Firefox** | nothing about your connection | makes your **login** readable to yt-dlp |

The wall says *"Sign in to confirm you're not a bot"* — it is asking for
**authentication**, not a different address. That is why a login clears it and
why proxies were never the fix here.

Firefox doesn't make you immune either. At high volume you can still hit rate
limiting (HTTP 429) whatever your IP, and a signed-in account downloading
heavily can get the **account** flagged — which is worse than an IP block,
because it follows you everywhere. Use a throwaway account.

The tool pauses 2–5 seconds between videos for this reason. Pacing avoids far
more blocks than any IP trick, and a few seconds per video costs much less
than getting cut off halfway through a playlist.

Two other things worth trying before anything else: **wait about 15 minutes**,
since the block is often temporary and tied to your IP, and run
`pip install -U yt-dlp`.

Use a **throwaway Google account**, not your main one. Downloads are tied to
whichever account the cookies belong to, and heavy automated use can get an
account limited.

## "The page needs to be reloaded."

A badly named error — it has nothing to do with reloading anything, and it is
not a cookie problem.

YouTube guards its stream URLs with a **JavaScript challenge**. yt-dlp needs a
JS runtime to solve it. With no runtime installed, signature solving fails, the
formats come back with no URLs, and YouTube reports the failure as *"The page
needs to be reloaded."*

The giveaway is in the warnings printed just above it:

```
WARNING: No supported JavaScript runtime could be found. Only deno is enabled by default
WARNING: Signature solving failed
WARNING: n challenge solving failed
ERROR:   The page needs to be reloaded.
```

Fix, once:

```
winget install DenoLand.Deno
pip install -U yt-dlp-ejs
```

The tool finds Deno automatically afterwards, including in the versioned
folder winget uses that never lands on your PATH. If it can't find one, it
warns at startup instead of letting you hit the confusing error.

### SABR and missing resolutions

Separately, YouTube is rolling out **SABR** streaming. On affected videos most
formats come back without usable URLs, leaving only the 360p progressive
stream. When that happens you'll see far fewer resolutions than expected, and
a TV-Safe 720p request quietly produces 360p — the tool downloads the best it
is actually offered. That is a YouTube-side restriction, not something the
tool or a different setting can raise. Tracked upstream at
`yt-dlp/yt-dlp#12482`.

## Requirements

Install **Python 3.8+** from [python.org](https://www.python.org/downloads/),
ticking **"Add python.exe to PATH"** on the first screen. That is the only
manual step.

Then clone the repo and double-click **Download.bat**. It runs `setup.py`
first, which installs everything else:

| | What | Size |
|---|---|---|
| 1 | `yt-dlp`, `Unidecode`, `yt-dlp-ejs` | small |
| 2 | `ffmpeg` + `ffprobe` into `bin\` | ~180 MB, once |
| 3 | **Deno** (unlocks YouTube streams) | via winget |

```
git clone https://github.com/Muneeb2323/yt-downlaoder.git
cd yt-downlaoder
Download.bat
```

Setup runs before every launch but only *does* anything the first time — on a
ready PC it finishes in about a quarter of a second, so it costs nothing.

### What is deliberately kept out of git

- **`cookies.txt`** — holds live Google session tokens. Anyone who reads it
  could sign in as you, so it must never be committed. Each PC exports its own.
- **`bin/`** — 330 MB of binaries; `setup.py` fetches them per machine.
- **`Downloads/`** — your videos, not source code.

### Keeping it working

Run this every few weeks. YouTube changes often, and an out-of-date yt-dlp is
the most common cause of downloads suddenly failing:

```
pip install -U yt-dlp yt-dlp-ejs
```

## A note on what you download

This tool doesn't care what you point it at, but YouTube's Terms of Service
prohibit downloading without permission, and redistributing commercial music
or films to customers is copyright infringement in most countries regardless
of whether you charge for it. Content you own, content licensed under Creative
Commons, and material with the uploader's permission are all fine. How you use
it is your call.
