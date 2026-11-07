# mu-dekhuchi

Local-first CCTV dashboard for Hikvision DVRs/NVRs, with **encrypted RTSP
decryption** and **vision-language events** from an OpenAI-compatible endpoint.
No cloud, no hosted inference, no accounts.

## Features

- **7-camera selector** — one live stream at a time, all channels configured
- **Decrypts Hikvision stream encryption** in real time (AES-128), supporting **H.264 and H.265/HEVC** (H.265 experimental; firmware-dependent)
- **Main stream by default** (`xx1`) for higher resolution, with sub stream (`xx2`) available
- **Reads the verification code from the DVR** with an admin account
- **Live MJPEG + camera audio**, snapshots, per-camera MP4 recording (stream copy), browser playback
- **Vision events** — motion-gated frames sent to a local VLM endpoint; people,
  vehicles, animals, descriptions, confidence, snapshots, an activity signal
  that collapses repeated observations into one incident across all cameras,
  and search across recent events
- **All-camera motion scan** — every 2 seconds, ISAPI stills from inactive
  cameras are diffed and passed through a VLM gate; laundry, trees, shadows and
  lighting are ignored, while real person or animal movement is logged as
  a motion incident with a description (vehicles stay on the active camera)
- **SQLite event store** with cooldowns/dedupe and retention
- FastAPI + uvicorn, typed Python 3.13, `uv`, `ruff`, pytest

## Requirements

- Python 3.13 (mise pins it in the repo)
- [uv](https://docs.astral.sh/uv/)
- `ffmpeg` on `PATH`
- A Hikvision / HiLook / EZVIZ DVR or NVR with an **admin** account
- An OpenAI-compatible vision endpoint (vLLM, llama.cpp server, LM Studio, …)
  for real events; `mock` works without one
- Node 24 only to rebuild the CSS (`npm install && npm run build:css`)

## Install

```bash
uv sync --extra dev
```

Copy the secrets example and set the camera password:

```bash
cp .env.example .env
```

## Configure

`.env` holds **secrets only**:

```dotenv
STREAM_PASS=your-camera-password
DASHBOARD_PASSWORD=
VISION_API_KEY=
```

Everything else lives in `config.yaml`:

```yaml
dvr:
  host: 192.168.0.103
  user: admin
  password: ${STREAM_PASS}

capture:
  mode: decrypt # decrypt | rtsp | snapshot
  stream: main # main (xx1, default) | sub (xx2)

cameras:
  - { id: "1", name: Front Door, channel: 1 }
  - { id: "2", name: Backyard, channel: 2 }

vision:
  backend: openai
  base_url: http://127.0.0.1:11234/v1
  model: LiquidAI/LFM3-VL-2B-MLX-4bit
  disable_reasoning: true
  min_confidence: 0.75
  scan_all_cameras: true
  scan_interval: 2
```

`${VAR}` values are expanded from `.env` or the process environment. Point at a
different file with `MU_DEKHUCHI_CONFIG=/path/to/config.yaml`.

Then verify the DVR, credentials, verification code and first camera:

```bash
uv run mu-dekhuchi check
```

Run the dashboard:

```bash
uv run mu-dekhuchi serve
# http://localhost:8080
```

Or with mise:

```bash
mise run start     # Python app
mise run check
mise run test
mise run lint
```

## Capture modes

| `capture.mode` | Use when | How |
| --- | --- | --- |
| `decrypt` | Hik-Connect encryption is on | RTSP + AES-128 decryption |
| `rtsp` | Encryption is off | Plain RTSP |
| `snapshot` | Last resort | ISAPI JPEG polling |

Encrypted NALs are flagged by RTP header extension `0x4000`. The verification
code (zero-padded to 16 bytes) is used as the AES-128-ECB key. The original
decryption approach is based on
[homebridge-hikvision-dvr](https://github.com/matthewdeaves/homebridge-hikvision-dvr)
(MIT).

**Codecs:** H.264 and H.265/HEVC are supported. H.265 encrypted-stream handling
is **experimental** (firmware-dependent). The pipeline auto-detects the codec
from SDP and depacketizes accordingly (H.264 FU-A/STAP-A, HEVC RFC 7798 FU/AP).
Use `stream: sub` if a specific device/firmware doesn’t decode H.265 correctly.

## Vision

The model is not loaded by this process. It runs wherever you like and exposes
an OpenAI-compatible API. The app sends a single frame as a base64 `image_url`
data URL and expects a JSON object in return.

```yaml
vision:
  backend: openai
  base_url: http://127.0.0.1:8000/v1
  model: lfm2.5-vl-3b
  api_key: ${VISION_API_KEY}
  interval: 2
  cooldown: 20
  disable_reasoning: true
  min_confidence: 0.75
```

Any endpoint that supports `POST /v1/chat/completions` with image content works:
vLLM, llama.cpp's server, LM Studio, or a gateway in front of Liquid AI's latest
`LFM2.5-VL-3B`.

Without an endpoint, set `backend: mock` to exercise the pipeline; it returns a
deterministic `person` event.

How events are produced:

1. Sample the active camera every `interval` seconds.
2. Cheap motion gate (64×48 grayscale diff) — no motion, no model call.
3. One VLM call at a time; reasoning disabled when the template supports it.
4. Results below `min_confidence` or `event_type: none` are discarded.
5. Repeats within `cooldown` update `last_seen` instead of inserting.
6. Snapshot + row stored; the UI polls `/api/events`.

Events live in SQLite (`data/events.db`), snapshots in `data/events/<camera>/`.

### Cross-camera stories

`mu_dekhuchi/stories.py` groups `person` and `animal` events on at least two
cameras when they fit inside a 90-second span with no gap larger than 45
seconds. Each story gets a camera path (`Camera 4 → Camera 5`), a link score,
and a plain narrative. When a story spans more than 60 seconds or collects
more than 8 observations it is marked **loose**: the UI says "loose link" and
"possible activity" instead of presenting a percentage. This is time-and-kind
correlation, **not** biometric identity: treat a story as a possible same
subject, never as a confirmed match. The dashboard polls `/api/stories` every
10 seconds and renders the strip above the workspace; a story opens into its
evidence snapshots and descriptions.

## API

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/api/status` | Camera, recorder, vision state |
| `GET` | `/api/cameras` | Camera list |
| `POST` | `/api/cameras/:id/activate` | Switch camera |
| `GET` | `/api/cameras/:id/still.jpg` | ISAPI still for the all-camera wall |
| `WS` | `/ws/audio?camera=1` | Live PCM audio (G.711 μ-law → 16 kHz s16le) |
| `GET` | `/api/snapshot.jpg?camera=1` | Single JPEG |
| `GET` | `/stream.mjpeg?camera=1` | Live MJPEG |
| `GET` | `/api/recordings?camera=1` | List recordings |
| `POST` | `/api/recordings/start?camera=1` | Start recording |
| `POST` | `/api/recordings/stop?camera=1` | Stop recording |
| `GET` | `/api/recordings/:name?camera=1` | Play/download |
| `DELETE` | `/api/recordings/:name?camera=1` | Delete |
| `GET` | `/api/events?camera=1&limit=24` | Vision events |
| `GET` | `/api/events/:id/snapshot` | Event JPEG |
| `GET` | `/api/stories?limit=12` | Cross-camera possible links |
| `GET` | `/healthz` | Health check |

## Tests and lint

```bash
uv run pytest
uv run ruff check mu_dekhuchi tests
uv run ruff format --check mu_dekhuchi tests
uv run ty check mu_dekhuchi tests
```

Unit tests need no DVR and no model. The live smoke test is `mu-dekhuchi check`.
Rebuild the UI after changing templates or Alpine code:

```bash
npm run build:css
```

## Layout

```
config.yaml       cameras, DVR, capture, vision, events, recording
mu_dekhuchi/
  config.py       YAML loader + ${ENV} expansion
  rtsp.py         RTSP digest, RTP, NAL reassembly
  crypto.py       AES-128 NAL decryptor
  camera.py       RTSP -> decrypt -> ffmpeg -> JPEG frames
  recorder.py     decrypted H.264 -> MP4 (-c copy)
  hub.py          camera selector + recorders
  isapi.py        verification code, channel codec
  events.py       SQLite event store + motion detector
  engine.py       motion gate -> VLM -> event
  scanner.py      ISAPI still scan for inactive cameras
  stories.py      time + kind correlation across cameras
  vision/         mock + OpenAI-compatible backends
  server.py       FastAPI routes, MJPEG, auth
  ui.py           Jinja render + initial state
  templates/      dashboard template (Jinja + Alpine)
  static/         app.js, Alpine, built styles.css
  cli.py          serve / check / codecs
assets/           Tailwind input and design tokens
```

## Privacy

Everything runs on your machine except the vision request, which goes to the
`vision.base_url` you configure. Snapshots and events stay local. Bind to
`127.0.0.1` by default; set `server.host: 0.0.0.0` and
`server.dashboard_password` only on a trusted LAN. Do not expose this to the
internet without TLS and a proxy.
