# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

A single technical owner of a small Hikvision DVR installation — likely the
person who set up the LAN, the cameras, and the local vision endpoint. They
check the dashboard from a laptop or desktop most of the day, and occasionally
from a phone while away from the desk. Their job is to confirm the property is
being watched, see what just happened, and capture or keep evidence without
leaving the browser.

## Product Purpose

mu-dekhuchi is a local-first CCTV console for Hikvision DVRs/NVRs. It turns an
encrypted RTSP feed into a live browser view, records clips without
re-encoding, and uses a local vision-language model to turn motion into
described, searchable events. Success means the owner trusts the feed at a
glance, can switch cameras without thinking, and can act on an event in one or
two clicks.

## Positioning

It does the parts consumer camera apps hide: it decrypts Hikvision's encrypted
RTSP stream in-process (H.264/H.265), keeps every frame and event on the LAN, and pairs the
live feed with a local OpenAI-compatible vision model instead of a cloud
subscription. No account, no vendor app, no hosted inference.

## Operating Context

- Runs on the owner's machine or a small always-on box on the same LAN as the
  DVR; opened as `http://localhost:8080` or a LAN address.
- Seven cameras on one DVR, one active live stream at a time.
- Sub streams are 352×288; main streams reach 704×576 or 960×1080.
- The vision model runs separately on a local endpoint and may be idle, slow,
  or erroring; the UI must stay honest about that.
- Recording is an explicit, short-lived action, not a 24/7 archive.
- The owner also uses this on a phone for a quick check, not for long review.

## Capabilities and Constraints

- Live MJPEG playback, snapshots, per-camera MP4 recording, playback and delete.
- Motion-gated vision events with people/vehicle counts, description,
  confidence, snapshot, and model name.
- H.264 and H.265/HEVC supported for encrypted Hikvision streams (H.265 experimental, firmware-dependent); main stream (`xx1`) is the default.
- One active camera is analysed at a time; switching resets motion detection.
- Configuration is `config.yaml` plus three secrets in `.env`.
- FastAPI + Jinja + Alpine.js + Tailwind; no SPA build step at runtime.
- Must remain usable when the feed is connecting, the model is down, or there
  are zero events and zero recordings.

## Brand Commitments

The name is mu-dekhuchi (Odia for "I am seeing"). The existing mark is an eye/lens glyph.
The product is private, local, and technical; it should never look like a
consumer subscription upsell or a generic SaaS admin template.

## Evidence on Hand

- Seven live cameras and a working DVR on the owner's LAN.
- Real vision events produced by `LiquidAI/LFM3-VL-2B-MLX-4bit` with reasoning
  disabled and a 0.75 confidence floor.
- Real recordings and snapshots under the configured data directory.
- No marketing copy, customers, pricing, or benchmarks exist; none may be
  invented.

## Product Principles

1. The live feed is the product; telemetry exists to make it trustworthy.
2. Every state is legible at a glance, including degraded and offline states.
3. The interface gets out of the way of switching, recording, and reviewing.
4. Local and private is visible in the language, not just the architecture.
5. Density is earned: show real operational detail, never decorative filler.

## Accessibility & Inclusion

Operate on a laptop at arm's length and a phone at close range. Body text and
controls must meet WCAG AA contrast; status cannot rely on colour alone; all
interactive controls are keyboard reachable.
