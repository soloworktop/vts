# VTS

**Video URL → structured Markdown notes.** Paste a video link, and VTS fetches the
transcript (or subtitles), runs it through an LLM you configure, and lands an editable,
searchable, exportable Markdown note. Ships with a web console and a CLI.

> **Headline feature: subtitle-first.** When a video has subtitles (human or
> platform-auto-generated), VTS uses the subtitle text directly — **no audio download,
> no transcription API call, zero API cost**, and no API key required at all.
> Transcription is only needed when the video truly has no subtitles.

- **BYOK (Bring Your Own Key)** — not tied to any model vendor. Any **OpenAI-compatible**
  endpoint works: OpenAI, DeepSeek, Moonshot, a self-hosted vLLM/Ollama gateway… just set
  `base_url` / `api_key` / `model`.
- **Web console** — job list, live progress, artifact browsing & in-place editing, labels,
  full-text search, template management.
- **MIT licensed** — core features fully open source. No gating, no trial limits, no
  feature castration.
- Status: **M0/M1 complete** (server open-sourced + React frontend rewrite). Currently in
  release preparation (Docker one-click deploy / bilingual docs).

> 中文文档见 [`README.md`](README.md)。

---

## Quick start

### Option A: Docker (recommended)

No local Python/Node needed. Multi-stage build; the image already ships `ffmpeg` and runs
as a non-root user:

```bash
docker compose -f docker/docker-compose.yml up -d --build
# or
bash scripts/docker.sh up
```

- Web console: <http://127.0.0.1:8080> · health check: <http://127.0.0.1:8080/api/v1/health>
- Persistence: SQLite (incl. encrypted keys) in the `vts_data` volume (`/data`); job
  artifacts in `vts_output` (`/output`). `down` does **not** delete data.
- Env config (`LLM_API_KEY` etc., optional `VIDEO_TO_SUMMARY_TOKEN` auth, `TZ` timezone)
  and upgrade steps: see [`docker/README.md`](docker/README.md).

### Option B: local venv

Requires **Python 3.10+** and `ffmpeg` on your system
(`bash scripts/fetch_ffmpeg.sh` can fetch it automatically):

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
bash scripts/web.sh                      # open http://127.0.0.1:8080
```

CLI usage:

```bash
python -m video_to_summary.main "<video-url>"                        # subtitle-first, zero API cost
python -m video_to_summary.main "<video-url>" --summary-template 学术笔记
python -m video_to_summary.main "<video-url>" \
    --llm-key sk-xxx --llm-base-url https://api.deepseek.com/v1 --llm-model deepseek-chat
```

Once installed, the `vts` command is also available.

### Configuring an LLM (optional — you get transcripts even without one)

Put defaults in a root `.env` (copy from `.env.example`):

```ini
LLM_API_KEY=sk-xxx
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat
```

Or via the HTTP API (keys are Fernet-encrypted at rest; the API only returns masked values).
LLM config has exactly two slots: a **reasoning model** (used for summaries and polishing)
and a **speech recognition model** (transcribes audio when a video has no subtitles):

```bash
curl -X PUT localhost:8080/api/v1/llm -H 'Content-Type: application/json' \
  -d '{"summary":{"base_url":"https://api.deepseek.com/v1","api_key":"sk-xxx","model":"deepseek-chat"},
       "asr":{"base_url":"https://api.openai.com/v1","api_key":"sk-xxx","model":"whisper-1"}}'
```

**Without a key the job does not fail:** it still produces the raw transcript
(`.txt` / `.srt`) and skips the summarization phase with a `summarize_skipped` event;
add a key and hit "Regenerate" for the full note.

### Videos without subtitles

A Whisper-compatible transcription endpoint, either of:

```ini
OPENAI_API_KEY=sk-xxx          # directly OpenAI's whisper-1
ASR_BASE_URL=https://your-gateway/v1   # or any endpoint implementing /audio/transcriptions
ASR_MODEL=whisper-1
```

### Content behind login

No in-app QR-code sign-in is built in. Use `--cookies cookies.txt`, or pick
"browser cookies" in Settings → Network & Access (reads your local browser's login
state). The two are mutually exclusive; an explicit cookies file wins.

---

## Data location

The SQLite database (`app.db`: job history, labels, templates, Fernet-encrypted LLM keys)
defaults to:

- **Source checkout / editable install** (`pip install -e .`): `<checkout-root>/data/app.db`
  (unchanged — existing self-hosted installs keep their data where it is)
- **Installed as a package** (non-editable `pip install vts`): platform user data dir —
  - macOS: `~/Library/Application Support/VTS/app.db`
  - Windows: `%LOCALAPPDATA%\VTS\app.db`
  - Linux etc.: `$XDG_DATA_HOME/vts/app.db` (`~/.local/share/vts/app.db` when `XDG_DATA_HOME`
    is unset)

A checkout is detected when the directory two levels above `db.py` contains both
`pyproject.toml` and `src/video_to_summary/`; otherwise the package form is assumed and the
DB goes to the platform user data dir — it is never written into site-packages (which is
wiped on venv recreation/upgrade, and can fail to start under a system Python without write
access to site-packages).

Every form can be overridden with `VIDEO_TO_SUMMARY_DB` (Docker already sets it to
`/data/app.db`). Missing directories are created automatically; on failure an actionable
error tells you to set `VIDEO_TO_SUMMARY_DB` rather than silently crashing on permissions.
The resolved path is visible in the `sqlite database ready at ...` startup log line.

**Version override (external-build friendly)**: `get_version()` reads the
`VIDEO_TO_SUMMARY_VERSION` environment variable first (non-empty → returned as-is, e.g.
`VIDEO_TO_SUMMARY_VERSION=1.2.3 python -m video_to_summary.main ...`), so external
builds can report their own version (shown in `/api/v1/health` `version` and CLI
`--version`) without writing into the package's `_version.py`.

---

## Features

| Capability | Description |
|---|---|
| Subtitle-first | `auto` (default) / `manual_only` / `off`, language selectable (`auto` prefers Chinese) |
| Transcription | OpenAI-compatible Whisper API |
| LLM summarization | OpenAI-compatible + 10 built-in templates (General / Concise / Detailed / Tutorial / Academic / Meeting minutes / Business analysis / Xiaohongshu-style / Life notes / Task list) + custom template CRUD |
| Job management | create / list / detail / live progress / cancel / retry (with template) / delete / resume on restart / event replay |
| History & search | label system (rename/merge/delete) + SQLite FTS5 full-text search (body + metadata, highlighted hits) |
| Artifacts | `.summary.md` (editable in place, atomic write-back) + `.txt` / `.srt` / `.segments.json` |
| Export & migration | per-job Markdown export (real attachment); full history export/import as zip |
| Diagnostics | `GET /api/v1/logs/export` sanitized diagnostic log export (API keys / Bearer / cookies / home-dir pseudonymized) |
| Security | API keys Fernet-encrypted at rest; artifact path-traversal guard; optional Bearer-token auth; no-cache static assets |

### Capability negotiation

`GET /api/v1/capabilities` returns the set of optional capabilities declared by plugins;
this build ships no plugins, so the response is `{}`. In deployments with plugins
installed, the frontend renders the optional features those plugins provide.

---

## HTTP API

The canonical prefix is **`/api/v1`**; the transitional `/api/*` aliases were removed
alongside the M1 React-frontend switch, and all clients use `/api/v1`. See
[`README.md`](README.md) for the full endpoint table.

---

## Naming

The distribution and project name is **VTS**; the Python import package stays
**`video_to_summary`**:

```python
from video_to_summary import Settings, run
```

Install via the distribution name: `pip install vts` (or `pip install -e .` for dev).

---

## Plugin mount point

The core only defines a mount point; third-party plugins hook in through a standard
entry point, and **the core never references any concrete plugin package name**:

```toml
[project.entry-points."vts.plugins"]
my-plugin = "my_plugin:plugin"
```

A plugin implements `register(hooks)` and may register three kinds of hooks:
`RouteProvider` (extra routes), `JobSideEffect` (post-job side effects),
`SettingsProvider` (extra settings), and can declare optional capabilities via
`hooks.declare_capabilities({...})` (`/api/v1/capabilities` returns the union of what
plugins declare).

The check is **fail-closed**: 0 plugins = the expected state for this build, and the
product runs in its full BYOK form; entries exist but fail to load → refuse to start
with a fatal error, never a silent downgrade.

---

## What VTS deliberately does not include

VTS is intentionally single-user and BYOK-only:

- **No local Whisper** — transcription goes through an OpenAI-compatible Whisper API you
  configure. (No local model downloads, no model-management UI.)
- **No Bilibili QR-code sign-in** — use cookies (explicit file or browser cookies).
- **No PDF export** — artifacts export as Markdown; full-history export/import is zip.
- **Single-user** — no accounts, no multi-tenancy, no usage dashboards.
- **No gating** — no license checks, no trials, no watermarks, no feature paywalls.

Everything above lives behind the plugin mount point, so extensions can add it without
touching the core.

---

## Development

```bash
pip install -e ".[dev]"
python -m pytest -q                    # offline by default; network cases need VTS_NETWORK_TESTS=1
pip install -e ".[e2e]" && playwright install chromium
bash scripts/e2e.sh                    # end-to-end (real uvicorn + in-process fakes)
```

Repo conventions: [`AGENTS.md`](AGENTS.md). Contribution scope (incl. the "features that
will not be accepted" list): [`CONTRIBUTING.md`](CONTRIBUTING.md).

---

## FAQ (self-hosting)

### Bilibili videos fail with HTTP 412 / no subtitles?

**HTTP 412** is a challenge from Bilibili's edge WAF on `/video/` pages, keyed on
**UA × IP reputation**: yt-dlp's default (browser-like) UA gets blocked, a non-browser
UA recovers. **Missing CC/AI subtitles** usually means the video needs a logged-in
session, and the Web/Docker form has no local browser (browser cookies unavailable).

Any of these fixes works (full compose examples in
**`docker/README.md` → "B 站 412 风控"**):

1. **Custom User-Agent**: set env `VTS_USER_AGENT=Wget/1.21.3` and retry (applies to
   CLI / Web / Docker; unset keeps yt-dlp's default UA for other sites).
2. **Login cookies**: set env `VTS_COOKIES_FILE` to a cookies.txt path (equivalent to
   CLI `--cookies`; an explicit file wins over browser cookies; also unlocks Bilibili
   CC/AI subtitles).
3. **Proxy**: configure one in Settings → Network & Access (Web), or pass `--proxy` (CLI).

---

## Disclaimer

This tool is for personal learning and research only. Please respect the terms of
service and copyright of the target platforms: do not download or redistribute content
you are not entitled to. The project contains no DRM/paywall-bypass capability and no
bundled credentials.

## License

[MIT](LICENSE) © 2026 VTS contributors
