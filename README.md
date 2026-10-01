<div align="center">

# 🎬 BlackOut Internet Downloader

**A [Rubika](https://rubika.ir) bot that downloads media from 1500+ websites.**

Built on [yt-dlp](https://github.com/yt-dlp/yt-dlp) · runs for free on GitHub Actions · also runs on a VPS or Docker.

</div>

---

## What it does

Send the bot a link and it downloads the media and sends it back to you — as a
video, an MP3, an image, or a file. It supports everything yt-dlp does
(YouTube, Instagram, Twitter/X, TikTok, Reddit, Pinterest, Bilibili, and
[hundreds more](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md)).

- 🎬 **Video** — best quality, or pick a resolution (144p → 4K)
- 🎵 **Audio** — extract to MP3
- 🖼️ **Images** — single images and galleries (via gallery-dl)
- 📃 **Playlists** — up to `MAX_PLAYLIST_ITEMS` items
- ✂️ **Auto-split** — files bigger than Rubika's 50 MiB ceiling are cut into parts with ffmpeg
- 📊 **Live progress** — the status message updates with a progress bar, speed and ETA
- ⚙️ **Settings** — per-user default quality and an "always ask" toggle
- 🔒 **Access control** — allow-lists, block-lists, private/group modes
- 🍪 **Cookies & proxy** — for age-gated or region-locked media

## Why GitHub Actions?

GitHub Actions gives you a free Linux box with ffmpeg, but any single job is
killed after **6 hours**. This bot turns that limit into an advantage:

```
bot.yml  ──330 min──►  persist state  ──workflow_dispatch──►  bot.yml  ──►  ...
   ▲                                                                        │
   └────────────────  watchdog.yml checks every 20 min  ◄───────────────────┘
```

- Each run works for `RUN_MINUTES` (default **330**, safely under the 6h cap),
  then saves its state and **dispatches the next run of itself**.
- The offset, per-user settings and stats are committed to a `bot-state`
  branch, so the next run resumes exactly where the last one stopped — no
  duplicate downloads, no lost messages.
- **`watchdog.yml`** runs every 20 minutes. If no run is active and the
  heartbeat is stale, it starts one. That is the safety net for a crash, a
  cancelled run, or a missed hand-off.

> **Note on repo visibility.** Public repos get **unlimited** Actions minutes.
> Private repos consume your monthly quota (Linux runners: 2,000 min/month on
> Free, 3,000 on Pro) — a 330-minute run uses ~5.5 hours of it, so a private
> repo would exhaust the free quota in roughly two weeks of continuous running.
> Keep this repo public, or run on a VPS instead.

---

## Quick start (GitHub Actions — recommended)

### 1. Create the bot

1. Open [Rubika BotFather](https://rubika.ir/BotFather) and create a bot.
2. Copy the **token** it gives you (looks like `123456789:AbCdEf...`).

### 2. Fork or use this repo

Push this project to your own repository (public for unlimited minutes).

### 3. Add the token as a secret

**Settings → Secrets and variables → Actions → New repository secret**

| Name | Value |
|---|---|
| `RUBIKA_BOT_TOKEN` | the token from BotFather |

### 4. (Optional) Add a PAT so runs are fully independent

By default the bot dispatches the next run with the built-in `GITHUB_TOKEN`,
which is enough. If you want the hand-off to be independent of the current
run's token (recommended), create a **fine-grained PAT** with
`Actions: read and write` + `Contents: read and write` on this repo and save it
as the secret **`GH_PAT`**.

### 5. Enable Actions and start the bot

Go to **Actions → bot → Run workflow**. That's it — it will keep itself alive.

You can also add these **repository variables** (Settings → Variables) to tune
it:

| Variable | Default | Meaning |
|---|---|---|
| `RUN_MINUTES` | `330` | Minutes per run before handing off |
| `ADMIN_IDS` | *(empty)* | Comma-separated Rubika user ids with full access |
| `ALLOWED_USERS` | *(empty)* | If set, only these users may use the bot |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `RUBIKA_API_BASE` | `https://botapi.rubika.ir/v3` | API base URL |

---

## Running on a VPS / Docker

```bash
git clone https://github.com/Parsa-Nasiri/blackout-internet-downloader.git
cd blackout-internet-downloader
cp .env.example .env
# edit .env and set RUBIKA_BOT_TOKEN

docker compose up -d --build
docker compose logs -f
```

Or without Docker:

```bash
sudo apt install -y ffmpeg
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # then edit it
python -m rubika_dl run
```

### systemd service

```ini
# /etc/systemd/system/blackout-downloader.service
[Unit]
Description=BlackOut Internet Downloader
After=network-online.target

[Service]
WorkingDirectory=/opt/blackout-downloader
ExecStart=/opt/blackout-downloader/.venv/bin/python -m rubika_dl run
Restart=always
RestartSec=5
User=blackout

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now blackout-downloader
```

---

## Commands

| Command | Description |
|---|---|
| `/start` | Welcome + inline buttons |
| `/help` | Full help |
| `/vid <url>` | Download as video (asks for quality) |
| `/audio <url>` | Extract audio as MP3 |
| `/img <url>` | Download images |
| `/link <url>` | Show media info without downloading |
| `/settings` | Default quality and "ask each time" |
| `/cancel` | Cancel your running downloads |
| `/stats` | Usage statistics |
| `/id` | Show chat/user ids (for allow-lists) |
| `/about` | About the bot |

You can also just **paste a link** — the bot detects URLs automatically.

---

## Configuration reference

All settings are environment variables (or `.env`). See
[`.env.example`](.env.example) for the annotated list. The most useful ones:

| Variable | Default | Meaning |
|---|---|---|
| `RUBIKA_BOT_TOKEN` | — | **Required.** From BotFather |
| `ADMIN_IDS` / `ALLOWED_USERS` / `BLOCKED_USERS` | empty | Comma-separated user ids |
| `ALLOW_GROUPS` | `true` | Whether the bot works in groups |
| `PROXY` | — | e.g. `socks5://127.0.0.1:1080` |
| `COOKIES_FILE` / `COOKIES_URL` | — | For age-gated / private media |
| `MAX_UPLOAD_SIZE` | `52428800` (50 MiB) | Rubika's per-file ceiling |
| `MAX_IMAGE_SIZE` | `10485760` (10 MiB) | Rubika's image ceiling |
| `SPLIT_ENABLED` | `true` | Split files over the ceiling |
| `MAX_CONCURRENT_DOWNLOADS` | `2` | Worker threads |
| `DELETE_AFTER_UPLOAD` | `true` | Delete files from disk after sending |
| `RUN_MINUTES` | `330` | Minutes per Actions run |

> ⚠️ **Rubika upload limits.** Rubika documents 50 MiB for `File`/`Video`,
> 10 MiB for `Image`, and mp3-only for `Music`. The bot auto-splits anything
> larger and falls back to a plain `File` upload when a type does not fit.
> These ceilings are far below Telegram's 2 GB — that is a platform limit, not
> a bot limit.

---

## How it works

```
rubika_dl/
├── main.py         CLI entry point (run / check / version)
├── bot.py          long-polling loop, graceful shutdown, hand-off
├── rubika_api.py   the Rubika Bot API client (v3)
├── updates.py      raw Update → normalised Incoming events
├── commands.py     command routing, quality menus, inline-keypad callbacks
├── jobs.py         worker pool + job queue (cancel, prune, idle-wait)
├── downloader.py   probe → download → size-check → split → upload → caption
├── engine.py       the yt-dlp/ffmpeg wrapper (the only place yt-dlp is used)
├── urls.py         URL detection and safety checks
├── store.py        atomic JSON state, users, stats
├── fmt.py          human-size / time / progress-bar formatting
├── config.py       environment configuration
└── log.py          logging setup

scripts/state_sync.py   pull/push state branch, heartbeat, dispatch, health check
.github/workflows/
├── bot.yml       the long-running bot with self hand-off
├── watchdog.yml  restarts the bot if it dies
└── test.yml      CI: pytest + compileall
```

The flow for one download:

1. **Update** arrives via `getUpdates`; `updates.py` normalises it.
2. **`commands.py`** routes it. If it is a URL, it probes metadata with yt-dlp
   and (unless disabled) shows a quality keypad.
3. A **job** is submitted to the worker pool (`jobs.py`).
4. **`downloader.py`** downloads with progress edits, checks the size, splits
   if needed, uploads via `requestSendFile` → multipart upload → `sendFile`,
   and attaches a caption.
5. The offset is persisted; state is flushed to the `bot-state` branch at the
   end of the run.

---

## Development

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest -q                 # 54 tests, no network required
python -m rubika_dl check # validate your token against the live API
```

Tests use stub transports, so they never touch the network.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `getMe failed` | Token wrong, or bot not created yet |
| Bot silent | Check the run is active in the Actions tab; check `LOG_LEVEL=DEBUG` |
| Downloads fail on YouTube | Add `COOKIES_FILE`, or set `PROXY` |
| `50 MiB` upload errors | Expected — files are split automatically; raise `MAX_UPLOAD_SIZE` only if your account allows it |
| Runs stop after 6h | That is the hard cap; the hand-off should have started the next one — check `GH_PAT` and the Actions tab |
| Nothing happens after a push | Workflows only run from the **default branch** — merge to `main` |

## License

GPL-3.0, matching the upstream project this was inspired by
([chelaxian/tg-ytdlp-bot](https://github.com/chelaxian/tg-ytdlp-bot)).

## Disclaimer

Download only content you have the right to download. Respect each site's
terms of service and copyright law. The authors are not responsible for misuse.
