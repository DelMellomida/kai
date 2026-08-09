# S2 — `/params` rebuilds the whole snapshot at 20 Hz, per client

| | |
|---|---|
| **Tier** | 1 |
| **Severity** | Medium |
| **Effort** | Small |
| **Confidence** | High |
| **Lens** | Software |

## Location

- `web/server.py` — `_params_stream()` generator, `_PARAMS_POLL_S = 0.05`; `Dashboard.params_snapshot()`
- `ai/session.py` — `ConversationSession.get_status()`, `_project_status()`
- `ai/voice_assistant.py` — `VoiceAssistant.get_status()`, `input_levels()`, `stage_timings()`
- `web/state.py` — `DashboardState.merged()`

## Problem

Every connected browser gets its own Flask generator thread that calls `params_snapshot()` 20 times
a second. Each call builds a ~70-key dict from four sources: `state.merged()`,
`voice.get_status()`, `session.get_status()` and the reboot flag.

`session.get_status()` takes the session `RLock` for the bulk of its body, and inside that lock
`_project_status()` calls `self._voice.get_status()`, which takes the assistant lock. It then does
further work (`input_levels()`, `recorder.status()`) after releasing.

Meanwhile the publishers only refresh at 25 Hz (`WEB_PUBLISH_INTERVAL = 0.04`), so most of that
work produces JSON byte-identical to the previous tick.

## Why it matters

The cost scales linearly with the number of open dashboard tabs, and it contends for the same
`RLock` that the 30 blocks/s audio worker needs in `_on_audio` (VAD) and that the 20 Hz session
tick holds. Two or three tabs open at a venue is a realistic load that has never been tested. This
is pure overhead: the data cannot change faster than the publishers write it.

## Acceptance criteria

- [ ] The full snapshot is built at most once per publish interval regardless of how many `/params`
      clients are connected — verified by instrumenting `session.get_status()` with a counter and
      confirming the rate does not scale with client count.
- [ ] All SSE generators serve from the shared cached snapshot; a new client connecting does not
      trigger an extra build outside the normal cadence.
- [ ] Session lock acquisitions attributable to `/params` no longer scale with client count.
- [ ] The observable SSE contract is unchanged: same key set, same value types, same ~20 Hz
      delivery cadence to each client, so `web/frontend/dashboard.html` needs no changes.
- [ ] `tests/test_web.py`'s existing `params_snapshot()` assertions still pass (the function stays
      public and directly testable — the cache sits in front of it, not inside it).
- [ ] With three simultaneous `/params` clients plus one `/video` client, `[control] N Hz` in the
      log stays at its normal rate and `sess_blocks_dropped` does not climb.

## Suggested approach

Add a small time-boxed cache alongside the published state — `DashboardState` is the natural owner,
since it already exists to be the seam between producers and Flask threads:

```
# sketch, not final
_snap_lock, _snap, _snap_t = threading.Lock(), None, 0.0

def cached_snapshot(self, build, now, max_age):
    with self._snap_lock:
        if self._snap is not None and now - self._snap_t < max_age:
            return self._snap
    fresh = build()                      # built OUTSIDE the cache lock
    with self._snap_lock:
        self._snap, self._snap_t = fresh, now
    return fresh
```

`_params_stream` then calls `dash.state.cached_snapshot(dash.params_snapshot, time.monotonic(),
WEB_PUBLISH_INTERVAL)`. Build outside the cache lock so a slow build never serialises the readers;
a brief duplicate build during a race is harmless and strictly better than holding the lock across
`session.get_status()`.

Two follow-on options, both optional and worth measuring before adopting:

- Emit only when the snapshot actually changes, with a keep-alive comment frame every second or
  two. Cuts network and JSON-encoding work further, but changes the frontend's assumptions about
  cadence — check `dashboard.html`'s transition detection (it appends chat bubbles on
  `voice_turn_id` changes, which is edge-based and safe, but confirm the rest).
- Encode the JSON once per snapshot rather than once per client.
