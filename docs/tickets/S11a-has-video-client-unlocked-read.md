# S11a — `has_video_client()` reads a shared counter without the lock

> Part of the grouped finding **S11 — Minor correctness and hygiene**, split into
> [S11a](S11a-has-video-client-unlocked-read.md) · [S11b](S11b-publish-web-fps-mislabelled.md) ·
> [S11c](S11c-dead-and-stray-code.md) · [S11d](S11d-persona-reread-per-call.md)
> for independent tracking. They share no code and can land in any order.

| | |
|---|---|
| **Tier** | 4 |
| **Severity** | Low |
| **Effort** | Small |
| **Confidence** | Medium |
| **Lens** | Software |

## Location

- `web/state.py` — `DashboardState.has_video_client()`, `add_video_client()`, `remove_video_client()`,
  `_video_clients`
- `face_track.py` — `run()`, the `compute_pose=(will_log or _web.has_video_client())` call
- `web/server.py` — `_video_feed()`'s generator, which increments on entry and decrements in `finally`

## Problem

`DashboardState` is explicitly "what the dashboard is currently being told, behind one lock", and
every other accessor — `publish_frame`, `publish_status`, `merged`, `latest_frame`,
`add_video_client`, `remove_video_client` — takes `self._lock`. `has_video_client()` does not:

```python
def has_video_client(self) -> bool:
    return self._video_clients > 0
```

Under CPython the read of a single `int` attribute is atomic, and the writers only ever `+= 1` /
`-= 1` under the lock, so this cannot observe a torn or half-written value. It is a consistency
defect, not a live race.

## Why it matters

Low, and honestly so. The practical risk is not the read itself but the precedent: this method sits
in a class whose entire contract is "one lock guards all of it", and the next person to add a field
here may reasonably copy the unguarded pattern into something where it does matter (a dict, a
tuple, a pair of fields that must agree).

There is a second, smaller point: `remove_video_client()` can drive the counter negative if it is
ever called without a matching `add`, and nothing clamps it. The `finally` in `_video_feed()` makes
that unreachable today.

## Acceptance criteria

- [ ] `has_video_client()` takes `self._lock`, matching every sibling accessor.
- [ ] The call site in `face_track.run()`'s inference branch is unaffected in behaviour — it runs at
      most `INFERENCE_FPS` times a second and only when a face is valid, so the added lock
      acquisition is immaterial. Confirm `[control] N Hz` is unchanged.
- [ ] `remove_video_client()` clamps at zero (`max(0, ...)`), so a bookkeeping bug degrades to
      "conditional solvePnP stays on" rather than to a permanently negative counter that reports no
      clients while clients exist.
- [ ] A short comment in `web/state.py` records the invariant — every accessor takes the lock, no
      exceptions — so the next field added inherits it.
- [ ] `tests/test_web.py` gains a case asserting the counter cannot go negative after an unmatched
      remove.

## Suggested approach

Three lines. Add the `with self._lock:` and the clamp:

```python
def has_video_client(self) -> bool:
    with self._lock:
        return self._video_clients > 0

def remove_video_client(self) -> None:
    with self._lock:
        self._video_clients = max(0, self._video_clients - 1)
```

Resist the temptation to "optimise" this back out on the grounds that the read is atomic. The class
is small and uncontended; the value of a uniform rule here is higher than the cost of one lock
acquisition at 15 Hz.
