# S11b — `_publish_web`'s `fps` can never report the real camera or inference rate

> Part of the grouped finding **S11 — Minor correctness and hygiene**, split into
> [S11a](S11a-has-video-client-unlocked-read.md) · [S11b](S11b-publish-web-fps-mislabelled.md) ·
> [S11c](S11c-dead-and-stray-code.md) · [S11d](S11d-persona-reread-per-call.md)
> for independent tracking. They share no code and can land in any order.

| | |
|---|---|
| **Tier** | 4 |
| **Severity** | Low |
| **Effort** | Small |
| **Confidence** | High |
| **Lens** | Software |

## Location

- `face_track.py` — `_publish_web()`: `fps_val = int(1.0 / (now - last_web_t)) if last_web_t else 0`
- `config/tracking.py` — `WEB_PUBLISH_INTERVAL = 0.04` (the 25 fps publish cap),
  `INFERENCE_FPS = 15`
- `config/camera.py` — `CSI_FRAMERATE = 30`
- `web/frontend/dashboard.html` — wherever the `fps` field is rendered

## Problem

The published `fps` is derived from the interval between *publishes*, and publishes are gated at
`WEB_PUBLISH_INTERVAL`. It therefore saturates at 25 and can never report either of the two rates an
operator would assume it means:

- the **camera** rate (30 fps on CSI), which it clips,
- the **inference** rate (15 fps), which it has no relationship to at all — the loop publishes raw
  frames between inferences precisely so the video stays smooth while MediaPipe is decimated.

The code comment is accurate about this ("Effective feed rate = inter-publish interval (steady ~25
fps while streaming), which is meaningful even though inference is decimated below the camera
rate"). The dashboard label is not — it just says fps.

## Why it matters

Purely diagnostic, which is why it is Low. But this is a robot with no screen, where `/params` is
the entire instrument panel, and the surrounding fields are scrupulously honest about what they
mean: `cam_reason` explains an absent camera, `sess_rms_floor` reports the *live* adapted floor
rather than the configured one specifically because "reporting the startup default while the gate
used a dashboard-set value would make the one number an operator relies on a lie."

A number labelled `fps` that pins at 25 whatever the camera does is the same class of small lie, and
it would mislead exactly during the investigation where frame rate matters — a stalling camera or a
GIL-starved loop (see **R2**, **R8**).

## Acceptance criteria

- [ ] The dashboard distinguishes the rates it actually has. At minimum: the existing value is
      relabelled to say what it is (e.g. "feed" / "stream fps"), so nothing claims to be the camera
      rate.
- [ ] Preferably, a true **inference** rate is published alongside it. `face_track.run()` already
      computes one — `frame_times` is averaged into `fps` for the `[face_track]` log line every
      0.5 s — so this is a matter of publishing a number that already exists rather than measuring a
      new one.
- [ ] If a camera-delivery rate is also wanted, it comes from `CameraThread` (frames stored per
      second), not from the publish interval.
- [ ] Each published rate has a distinct key; no key changes meaning silently, so an operator
      comparing against an older log is not misled.
- [ ] The frontend renders whichever rates are published with labels that match, and the tooltip or
      guide page (`web/frontend/guide.html`) explains the difference between feed rate and
      inference rate once.
- [ ] The publish path stays cheap — no new work in the hot loop beyond assigning an already-computed
      float.

## Suggested approach

Smallest useful version: `face_track.run()` already builds `frame_times` and turns it into an
inference fps for the log every 0.5 s, but clears the list when it logs. Keep the last computed
value in a local, pass it into `_publish_web`, and publish it as `infer_fps` next to the existing
`fps`. Rename the existing one's *label* in the frontend to "feed" and leave the key alone if
changing it would break anything reading it.

Fuller version, if the frame-rate picture is being built out for **R8**'s watchdog anyway: publish
three numbers — camera delivery rate (from `CameraThread`), inference rate (from `frame_times`), and
stream/publish rate (today's value) — since together they say *where* frames are being lost, which
is what the watchdog will want to report too. Do not build this speculatively; do it if and when R8
lands.

Either way, add a one-line comment in `config/tracking.py` next to `WEB_PUBLISH_INTERVAL` noting
that it caps the published feed rate, so the ceiling is discoverable from the constant that causes it.
