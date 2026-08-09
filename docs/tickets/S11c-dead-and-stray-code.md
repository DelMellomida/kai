# S11c — Dead and stray code

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

- `vision/camera.py` — `open_camera()`, the raising wrapper around `try_open_camera()`; no caller
  in the tree
- `scripts/autostart.sh.new` — sitting beside the live `scripts/autostart.sh`
- `servo/servo_serial.py` — a CLI tool that calls `ServoSerial._force_send()`, a private method,
  in four places (`sweep()`, the position test, interactive mode, the park-on-exit)

## Problem

Three unrelated bits of residue, grouped because each is a two-minute decision and none warrants its
own ticket.

1. **`open_camera()`** is retained deliberately — its docstring says "Retained because a hard failure
   is still the right behaviour for a one-shot script" — but no one-shot script uses it. It is
   documented dead code, which is better than undocumented dead code and still dead.
2. **`autostart.sh.new`** is a second copy of the boot script with no indication of which is
   authoritative. `app/lifecycle.py` builds real behaviour on top of `autostart.sh` — the
   `KAI_SUPERVISED` env var, the `EXIT_ALREADY_RUNNING` / `EXIT_RESTART` exit-code contract, and the
   `/proc/<ppid>/cmdline` fallback that exists specifically because "a supervisor loop that was
   already running when this file was updated started from the OLD autostart.sh". A stray `.new`
   file in that context is a genuine trap during a deploy.
3. **`servo_serial.py` reaching into `_force_send()`** bypasses the rate gate, the deadband and the
   `SERVO_MIN`/`SERVO_MAX` clamps in `send()`. That is arguably correct for a diagnostic tool
   (`sweep()` deliberately wants ungated 0–180 travel), but it is expressed as a private-method call
   rather than as an intentional API, so a refactor of `ServoSerial`'s internals breaks the
   diagnostic silently — and the tool commands angles the rest of the system considers unsafe.

## Why it matters

Individually trivial. Collectively they cost the same thing: a reader cannot tell what is live. That
matters most for `autostart.sh.new`, where picking the wrong file during a rebuild (see **S10**)
would produce a robot that boots but is not supervised — and `lifecycle.supervised()` is explicitly
designed around that scenario being hard to detect.

The `_force_send()` coupling also interacts with **R4**: if firmware-side limits land, a sweep tool
that commands 0° and 180° will be silently clamped by the firmware, and the tool's output will claim
angles it did not reach.

## Acceptance criteria

- [ ] `vision/camera.open_camera()` is either deleted, or kept with a comment naming an actual
      caller (existing or planned). "Might be useful" is not sufficient — decide.
- [ ] `scripts/autostart.sh.new` is either merged into `scripts/autostart.sh` and deleted, or
      renamed to something unambiguously non-executable and dated. Whichever is live is stated in the
      README's deploy section.
- [ ] If `.new` contains changes not in the live script, those changes are reviewed on their own
      merits before merging — do not fold in an unreviewed boot script as part of a cleanup.
- [ ] `servo/servo_serial.py` no longer calls a private method. Either `ServoSerial` grows a public
      `force(pan, tilt, jaw=None)` (documented as bypassing the gate and the clamps, for diagnostics
      only), or the tool uses `send()` and accepts the gate.
- [ ] If the diagnostic keeps its ungated 0–180 sweep, that is stated in the tool's `--help` and in
      a comment, along with the fact that it deliberately exceeds `SERVO_MIN`/`SERVO_MAX` and may be
      clamped firmware-side once **R4** lands.
- [ ] Full suite green; no import of a removed symbol anywhere (`grep` for `open_camera` and
      `_force_send` before and after).

## Suggested approach

Take them one commit each, so a mistaken deletion is trivially revertible.

`open_camera()` — delete. `try_open_camera()` is the real API, is the one with the failure reasons
the dashboard shows, and any future one-shot script can raise on `(None, reason)` in two lines. If
it is kept instead, `scripts/camera_diag.sh` is the natural caller to point the comment at.

`autostart.sh.new` — diff it against the live script first (`diff scripts/autostart.sh
scripts/autostart.sh.new`). If it is stale, delete it. If it carries the `wait_for_capture_device`
or exit-code handling that `app/lifecycle.py`'s docstrings describe, that is a real change and needs
a real review, not a rename.

`_force_send()` — the smaller change is to add the public `force()` method and have it be the one
`_force_send` becomes, keeping the private name as an alias only if `tests/test_servo.py` depends
on it. Check the tests first.
