# Universal Video Studio

A Windows desktop app (Python 3.11, PySide6, yt-dlp, FFmpeg) that lets you paste a video link, mark **several sections** of the
video on an interactive timeline, and export **each section as its own file** (`my_video_clip_01.mp4`, `_02`, ...). Everything
runs locally - no cloud service, Node.js, Docker or database.

> **Your responsibility.** Only download or cut videos you have the right to use. You are responsible for respecting copyright
> law, each platform's terms of service, and the permissions attached to the content. The app does not bypass DRM, paywalls,
> logins or other access controls, and does not promise that every site works.

## Contents
1. [Features](#features) · 2. [Install (Windows)](#install-windows) · 3. [Using the app](#using-the-app) ·
4. [Accuracy: fast vs accurate trim](#accuracy-fast-vs-accurate-trim) · 5. [Remote sections vs. full download](#remote-sections-vs-full-download) ·
6. [Settings & data locations](#settings--data-locations) · 7. [Portable build](#portable-build-pyinstaller) ·
8. [Updating yt-dlp / FFmpeg / dependencies](#updating) · 9. [Tests](#tests) · 10. [Troubleshooting](#troubleshooting) ·
11. [Verified features and limitations](#verified-features-and-limitations)

## Features
* URL analysis in a background thread (cancelable): title, uploader, duration, thumbnail, available resolutions, size estimate.
* Playlist links: choose a single video, one video from the list, or queue all.
* Preview player (Qt Multimedia) with play/pause, +/-5 s, go-to-time, slider, per-clip preview. If streaming is not possible
  you get a clear message and the option to download a small (<=480p) preview file. The full video is never downloaded just for preview.
* Timeline: ruler, thumbnail strip (once a local file exists), playhead, zoom (buttons / Ctrl+Wheel), horizontal scroll,
  click-to-seek, any number of independent coloured clips, draggable left/right trim handles, move whole clip, snapping (hold **Alt** to disable),
  overlapping clips drawn on separate lanes, context menu, minimum clip length 0.5 s.
* Clip list: rename, per-clip file name, edit start/end by typing, reorder (= export order), duplicate, delete, reset.
* Export: **Download all clips** / **Download selected** / **Download full video** (whole video, no trim or crop); one file per clip; collision-safe names (`name (2).mp4`); sanitised for Windows.
* Crop: Original, 16:9, 9:16, 1:1, custom ratio, free rectangle; drag/resize on the preview; defaults to centre crop; per-clip; even dimensions; applied by FFmpeg.
* MP4 or MP3; quality limit (360p ... 2160p / best); H.264 or H.265 re-encode codec. Missing resolutions are reported, never upscaled.
* Download manager with queue, per-job status/progress/speed/ETA (only shown when measurable), cancel, retry (failed jobs only), open folder, history.
* Dark/light theme, shortcuts, drag-and-drop of a link onto the window, persistent settings.

## Install (Windows)
1. **Python 3.11 (64-bit)** - https://www.python.org/downloads/windows/ (tick *Add python.exe to PATH*).
   Check: `py -3.11 --version`. (3.12/3.13 also work with the pinned ranges; 3.11 is the tested target.)
2. **FFmpeg** (required for trimming, merging and cropping):
   * Download a "full" or "essentials" build, e.g. from https://www.gyan.dev/ffmpeg/builds/ (or `winget install Gyan.FFmpeg`).
   * Option A - PATH: unzip to `C:\ffmpeg`, add `C:\ffmpeg\bin` to *System Properties > Environment Variables > Path*, open a **new** terminal, check `ffmpeg -version`.
   * Option D - easiest: skip all of this and use the app's built-in **Setup** window (see below).
   * Option B - portable: put `ffmpeg.exe` and `ffprobe.exe` in an `ffmpeg\` folder next to the app (the app finds `ffmpeg\`, `ffmpeg\bin\`).
   * Option C - Settings: *Settings > FFmpeg executable* -> browse to `ffmpeg.exe` (ffprobe must be in the same folder).
   * The build must include `libx264`, `libx265` (for H.265) and `libmp3lame` (for MP3).
3. **Run**: double-click **`run.bat`**. First run creates `.venv\` (nothing is installed globally) and installs `requirements.txt`
   (about 250 MB, internet needed). Later runs start immediately.

Manual equivalent:
```bat
py -3.11 -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
python main.py
```
Diagnostics: `python main.py --check` (Python, PySide6, yt-dlp, FFmpeg/FFprobe locations, data folders).

## First start: one-click setup (no command prompt)
If FFmpeg or yt-dlp is missing, a **Setup** window opens automatically and shows what is installed. Click **Download FFmpeg** (about 90 MB,
HTTPS, SHA-256 checked, stored in the app's own `tools\ffmpeg` folder) and/or **Install yt-dlp**. You can reopen it any time:
*Settings > Setup: install / check FFmpeg and yt-dlp*. The same window shows whether NVIDIA GPU encoding (NVENC) is available.
yt-dlp installs through pip into the app's own `.venv`; in a packaged build yt-dlp is already bundled.

## Tabs, sign-ins and cookies (private / age-restricted / "not a bot")

* **Tabs** - every video gets its own tab (`Ctrl+T` new, `Ctrl+W` close, or click **+ New Tab**). Each tab has its own
  preview, timeline, clips and crop. Downloads from all tabs share one **Downloads** panel on the right (job names carry `[Tab n]`).
* **Sign in once** - when a site says *sign in / private video / confirm you're not a bot / age-restricted*, the error box shows
  **Sign in to <site>...**. A browser window opens at that site's login page (YouTube/Google, Facebook, Instagram, X, TikTok...,
  or any site). Log in normally (2-step verification works), press **Save sign-in**, and the analysis/download is retried.
* **Cookies list** - **Cookies & sign-ins** (top right) shows every saved sign-in: status, cookie count, validity, source.
  You can sign in again, remove one, **import from your own browser** (Firefox is the most reliable) or import a `cookies.txt`.
* **Any website**: the browser window has an address bar and quick buttons (YouTube, Facebook, Instagram, TikTok, X, Vimeo...). Open any
  address, sign in, press **Save sign-in for <site>**. In **Cookies & sign-ins** select a site and press **View / delete cookies...**
  to inspect (values hidden until you tick *Show values*) or delete single cookies, **Remove site**, or **Remove everything**.
* While a link is analysed or a preview file downloads, the preview shows a spinner (with a percentage when known), the tab says
  *Analyzing...* and the status bar shows a progress stripe while any download runs. **Clear** now also clears the video, timeline and clips.
* Cookies are stored per site in `%APPDATA%\UniversalVideoStudio\cookies` and are sent **only to the site they belong to**
  (a YouTube download never sees Facebook cookies). Your password is never stored. Treat the folder like a browser profile.
* Google sometimes refuses logins from embedded browsers. If that happens use **Import from my browser** or a `cookies.txt`.
* YouTube also needs a JavaScript runtime: Setup offers a one-click **Deno** download (also fixes many "page needs to be reloaded" errors).
* The sign-in window needs QtWebEngine (included in `PySide6`; the portable build becomes ~150 MB larger because of it).

## GPU / CPU encoding
* Choose it once in **Settings > Hardware encoding** - the app remembers it (there is no per-tab switch): **Auto** detects your NVIDIA GPU
  and uses NVENC when it really works, **GPU** forces NVENC (CPU only if the GPU/driver cannot do it), **CPU only** never touches the GPU
  (it is not even probed).
* **Setup** (Settings > Setup, or at first start) has a *Video encoder (GPU / CPU)* row that follows this setting: it shows your GPU model
  (e.g. *NVIDIA GeForce RTX 3060: NVENC ready*) when the GPU will be used, or *CPU rendering is selected* / *Rendering will use the CPU*.
* The detector checks that FFmpeg lists `h264_nvenc`/`hevc_nvenc` and runs a tiny real encode per codec, and reads the GPU name
  (via `nvidia-smi`). Each job's *Task* column says what was used: `[GPU NVENC]`, `[CPU]`,
  `[CPU (GPU not available)]` or `[CPU - GPU failed]`.
* An encoder is only needed when a clip is re-encoded (Accurate trim, crop, H.265). **Fast stream-copy** trimming uses neither CPU nor GPU encoding.

## Timeline tips
* **Add a clip exactly where you point**: double-click or **right-click** an empty spot (also the empty row *below* an existing clip -
  the new clip gets its own row). The right-click menu offers 5 s / 10 s / 30 s / 1 min clips, *Move playhead here*, *Select all*.
* **Select several clips**: press the left button on empty space and drag - a blue rectangle selects every clip it touches.
  `Ctrl`/`Shift` adds or removes clips (also with the rectangle), `Ctrl+A` selects all, `Esc` clears.
* With several clips selected: drag one to **move them all together**, **Delete**/**Duplicate** act on all, and **Selected (n)** downloads them.

## Small screens
The layout adapts to the window: the **Downloads** panel starts hidden below ~1250 px (toggle with the *Downloads* button); when a tab gets
narrower than ~900 px it switches to two sub-tabs (**Video & Timeline** / **Clips & Export**); on low windows (<760 px high) the Video Info and
Output sections fold so the clip list stays visible; very narrow tabs use icon-only buttons (hover for the name). The export buttons stay
pinned at the bottom of the options column. If the window is smaller than the content, the page scrolls. Minimum window size: 640x480.

## App icon
*Settings > App icon*: pick any PNG/ICO/JPG/SVG (or press Reset for the built-in icon). It is used for the window and the taskbar immediately.
The icon of the **.exe file itself** comes from `assets\icon.ico` at build time: replace that file with your own `.ico`, then run `build.bat`.

## Using the app
1. Paste a link (or drag it onto the window) and press **Analyze** (Enter). **Cancel analysis** appears while it runs.
2. Press **Add clip** (Ctrl+N) or double-click an empty spot on the timeline. Add as many as you like.
3. Drag a clip's left/right edge to trim, drag its body to move it. Type exact times in the table if you prefer.
4. Choose format / quality / trim mode / output folder. Optionally pick a crop for the selected clip.
5. **Download all clips** (Ctrl+E). Watch the *Download queue* tab; finished files appear in *Export history*.

Shortcuts: Ctrl+L focus URL - Ctrl+N add clip - Ctrl+D duplicate - Del delete selected - Ctrl+E export all - Space play/pause - Left/Right -/+5 s - Ctrl+, settings.

## Accuracy: fast vs accurate trim
| Mode | How | Accuracy |
|---|---|---|
| **Fast** | FFmpeg stream copy (`-c copy`) when the source is H.264/AAC and no crop | Cuts snap to the nearest **keyframe before** the start, so a clip can start up to a few seconds early and be longer than requested. **Not frame-accurate.** |
| **Accurate** | Re-encodes video (H.264/H.265) + AAC | Cut points precise to ~1 frame. Slower, uses CPU. |

Cropping always re-encodes (so it is accurate). Incompatible source codecs (VP9/AV1/Opus...) are transcoded to H.264/AAC so the
MP4 plays everywhere. The app states which method was used for each job in the task column.

## Remote sections vs. full download
* If a selection covers **<= 50 %** of the video and *Prefer sections* is on, each clip is fetched with yt-dlp's section download
  (`--download-sections`). Whether bytes outside the range are really skipped depends on the website, stream type (DASH/HLS vs. one progressive file),
  byte-range support and keyframes; some sites transfer more than the section.
* Otherwise the source is downloaded **once** into the cache (`<temp>\sources`) and every clip is cut locally from it. A file already in the cache
  is reused for later exports, **never downloaded again per clip**. Partial downloads resume.
* Clear the cache in *Settings*. Nothing is deleted automatically (exports are never touched).

## Settings & data locations
Settings (`settings.json`), history, logs and sign-in cookies are in `%APPDATA%\UniversalVideoStudio\` (never inside the app folder).
Downloaded videos go to `C:\Users\<you>\Videos\UniversalVideoStudio\Video`; preview/low-quality cache files go to `...\Cache video`. Both can be changed in Settings.
Set a different *Temporary folder* in Settings to keep big files off the system drive. Environment override: `UVS_DATA_DIR`.
No passwords or cookies are stored. "Browser sign-in" only tells yt-dlp which installed browser to read cookies from at runtime
(for content *you* are allowed to see); logs strip URL query strings and secrets.

## Portable build (PyInstaller)
Run **`build.bat`** (runs the tests first). Result: `dist\UniversalVideoStudio\UniversalVideoStudio.exe`.
The build downloads FFmpeg and Deno once (into `.\ffmpeg`, `.\deno`) and bundles them with yt-dlp, so users need no setup.
The build contains no cookies or personal data: those are created on each user's PC in `%APPDATA%`. (Advanced: an empty `portable.flag` file next to the exe stores data in `data\` instead.)
In a packaged build yt-dlp is frozen into the exe: to update it, update it in the venv and rebuild.

## Updating
* **yt-dlp** (do this first when a site stops working): *Settings > Update yt-dlp*, or `.venv\Scripts\pip install --upgrade yt-dlp`. Restart the app.
* **FFmpeg**: download a newer build and replace the files (same folder / PATH entry).
* **Other dependencies**: `.venv\Scripts\pip install --upgrade -r requirements.txt`, then run the tests (`pytest`). The version ranges in
  `requirements.txt` are constraints, not exact pins; to freeze a known-good set run `pip freeze > requirements.lock.txt`.

## Tests
```bat
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest -q
```
200 tests: clip model, URL validation, filename safety, crop maths, export planning, FFmpeg arguments, queue state machine,
cancellation/errors, and real FFmpeg exports on a generated synthetic video (no network, no copyrighted media). Tests skip FFmpeg ones if it is missing.

## Troubleshooting
| Symptom | Fix |
|---|---|
| "FFmpeg not found" | See install step 2; confirm with `ffmpeg -version` in a **new** terminal or set the path in Settings. |
| "Unsupported URL" / extraction errors / HTTP 403 | Update yt-dlp (Settings). Check the video opens in your browser. |
| "Requires sign in" / age-restricted / members-only | Only if you may access it: click **Sign in to <site>...** in the error box (or open **Cookies & sign-ins**); the saved cookies are reused automatically. DRM content is never supported. |
| Preview is black / "could not be streamed" | Use *Download a small preview file*. Some codecs need the Windows "Media Feature Pack" on N editions. |
| Clip starts a bit early | That's fast mode (keyframes). Switch Trim to **Accurate**. |
| Crop export slow | Cropping re-encodes. Use `H.264`; consider fewer/shorter clips. |
| Disk full / no space | Set another *Temporary folder* and output folder; clear the cache. |
| Hangs on "Analyzing" | Press *Cancel analysis*; check connection / VPN / rate limiting (HTTP 429: wait). |
| App closes at start | `run.bat --check`; read `%APPDATA%\UniversalVideoStudio\logs\app.log`. |
| `pip install` fails (SSL/proxy) | Configure pip proxy or install from a network that allows PyPI. |
| Crash on a machine without audio device | Start with environment variable `UVS_NO_AUDIO=1` (preview becomes silent). |

## Project layout
`main.py` entry - `app/models` (clip, crop, job queue) - `app/services` (yt-dlp, FFmpeg, probe, thumbnails, export planning) -
`app/workers` (QThread/QRunnable workers + job controller) - `app/widgets` (timeline, preview, crop, clip list, download manager, dialogs) -
`app/utils` (paths, validation, logging) - `tests/`.
Rules kept throughout: all subprocesses use argument lists (no shell), URLs are validated, workers only emit signals (never touch widgets), partial files are written to
a hidden temp name and renamed only on success.

## Verified features and limitations
**Verified by automated tests / runs in a Linux cloud sandbox (headless Qt + real FFmpeg 6.1, PySide6 6.12, yt-dlp 2026.8):**
launching the full window; three clips on one timeline; handle drag / move / zoom / scroll / seek; export of 3 clips to 3 separate MP4 files
from one shared source job; fast vs accurate trim; 9:16 / 1:1 / 16:9 crops producing real even-sized output; MP3 extraction; name collisions;
cancel (FFmpeg killed, no partial file); partial failure + retry of only the failed clip; cached-source reuse; settings round-trip.

**NOT verified here (you should try them):** running on Windows itself (`run.bat`, `build.bat`, PyInstaller output, Explorer integration), live downloads from YouTube/Facebook
(the sandbox has no access to those services), real audio playback, GPU video rendering, and remote section downloads on real sites.

**Known limitations**
* Fast trim is keyframe-limited; frame accuracy needs Accurate mode.
* Section download savings depend on the website; some sites send the whole stream.
* Preview streaming works only when the site offers a progressive MP4/WebM; otherwise a small preview file must be downloaded. Thumbnails need a local file.
* Pausing a running download or FFmpeg step is not supported (the queue can be paused; cancel + retry resumes partial downloads). Pause/Resume per job is therefore not offered.
* Live streams and videos without a known duration are rejected. Facebook content frequently requires login; private content is unsupported.
* No automatic subject tracking, no merging of clips into one file.
* Cropping is applied with FFmpeg to the downloaded source; preview framing assumes the largest listed resolution's aspect ratio.


## Updating yt-dlp in the packaged app
Settings > "Update yt-dlp" (or Setup) downloads the official wheel from PyPI (SHA-256 checked) into
`%APPDATA%\UniversalVideoStudio\tools\ytdlp`. Restart the app to use it. The update is used only while it is newer than the
yt-dlp bundled by `build.bat`, so a rebuilt app is never held back by an old update. Nothing inside the app folder changes.

## Facebook / X / Instagram links and sign-ins
* Links are cleaned before analysis: Facebook **group** links (`/groups/<id>/videos/<id>`, `?multi_permalinks=<id>`, `/posts/`),
  mobile/`web.` hosts, `twitter.com` / `i/web/status` and Instagram `/<user>/reel/` links become forms yt-dlp understands;
  tracking parameters (`fbclid`, `mibextid`, `igsh`, ...) are removed.
* "Save sign-in" checks for the site's real login cookie (Facebook `c_user`+`xs`, Instagram `sessionid`, X `auth_token`,
  YouTube `SAPISID`/`SID`, TikTok `sessionid`, ...). If it is missing the window warns you, because such a session cannot download
  private videos. The Cookie manager shows **Signed in** / **No login found** per site.
* The sign-in window restores your saved sign-ins, so a site you already signed in to opens logged in.

## Troubleshooting a link: `run.bat --probe "<link>"`
Prints the address the site redirects to, the video ids/titles found in the page, whether the saved sign-in was accepted and what
yt-dlp returns, and saves it to `%APPDATA%\UniversalVideoStudio\logs\probe.txt`. No cookie values or page contents are written.

## Publishing an update (Google Drive or GitHub)
1. Raise `__version__` in `app/__init__.py`, run `build.bat`, zip the folder `dist\UniversalVideoStudio` and upload the zip
   (Google Drive: right-click > Share > "Anyone with the link"; copy the link - this is what users download).
2. Make a text file `latest.json`:
   `{"version": "1.1.0", "url": "<the zip's download link>", "notes": "What is new"}` (the url must start with https://).
3. Upload `latest.json` to Google Drive and share it with "Anyone with the link". To publish later updates, right-click the
   file > **Manage versions** > upload a new version, so the link and file id never change.
4. Put its share link (or `drive:<file id>`) into `UPDATE_INFO_URL` in `app/__init__.py` **once** and build. From then on every
   user sees "Update x.y.z available" in the status bar after you change `latest.json`.
GitHub works the same way: use the raw link of `latest.json` in your repo (or a Release asset). Updates are never installed
automatically; the download page opens only when the user clicks.

## Building on GitHub (Actions)
`.github/workflows/build.yml` builds the **Windows** app on GitHub (the app is a Windows program; a Linux build is not useful).
Actions tab > "Build Windows app" > Run workflow, or push a tag (`git tag v1.1.0 && git push --tags`) to also create a Release with
the zip. It uses Node-24-ready actions (checkout v5, setup-python v6, upload-artifact v5) and `build.bat` (which behaves
non-interactively when `CI` is set).

### Updates through GitHub Releases (this project: `yuppost/universal-video-downloader`)
`UPDATE_INFO_URL` is set to `github:yuppost/universal-video-downloader`, so the app asks GitHub for the repo's **latest Release**
(the repository must be public). To publish an update:
1. Change `__version__` in `app/__init__.py` (for example `1.1.0`), commit and push.
2. `git tag v1.1.0` then `git push origin v1.1.0` - the "Build Windows app" workflow builds the zip and creates the Release.
3. Users of older versions see "Update 1.1.0 available" in the status bar; the button opens the Release page to download.
The tag number must be higher than the version inside the user's app.
