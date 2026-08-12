# S2 — `/params` rebuilds the whole snapshot at 20 Hz, per client

> **Status: FIXED** — `perf/params-snapshot-cache`. `DashboardState.cached_snapshot()` sits in front
> of `params_snapshot()`; `_params_stream` goes through it at `WEB_PUBLISH_INTERVAL`.
> Suite green (1246 passed, was 1239).
>
> **Measured builds per second, by tab count:**
>
> | tabs | before | after | saved |
> |---:|---:|---:|---:|
> | 1 | 20.0 Hz | 20.0 Hz | **0%** |
> | 2 | 40.0 Hz | 20.0 Hz | 50% |
> | 3 | 60.0 Hz | 20.0 Hz | 67% |
> | 5 | 100.0 Hz | 20.0 Hz | 80% |
> | 8 | 160.0 Hz | 20.0 Hz | 88% |
>
> **The single-tab case saves nothing, and that is not a defect.** `_PARAMS_POLL_S` (0.05) is longer
> than `WEB_PUBLISH_INTERVAL` (0.04), so one client's polls always find the cache already expired.
> The ticket is about the load *scaling with tab count*, and that is what goes away: the build rate
> is now flat. Anyone reading this later and expecting a win on their own laptop with one tab open
> will not find one.
>
> Note the flat rate is 20 Hz, not the 25 Hz the cache would allow — with one builder the poll rate
> is the binding constraint, not `max_age`.

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

- [x] Built at most once per publish interval regardless of client count. Measured with a build
      counter over concurrent client threads — see the table above; the rate is flat from 1 to 8
      tabs. `test_build_count_tracks_time_not_client_count` asserts 1 client and 6 produce an
      identical count over the same window.
- [x] All SSE generators serve from the shared snapshot, and a new client does not trigger an extra
      build. **Stronger than the sketch in the ticket**, which left `build()` unguarded and accepted
      duplicate builds under a race — that is exactly the "new client connecting" case this
      criterion names, so a second `_build_lock` admits one builder and the losers re-check and find
      the fresh snapshot. `test_many_clients_on_a_cold_cache_build_once` pins it with 8 threads on a
      barrier.
- [x] Session lock acquisitions attributable to `/params` no longer scale with client count — they
      happen once per build, and builds no longer scale.
- [x] The observable SSE contract is unchanged: `params_snapshot()` is untouched, so the key set and
      value types are identical, each client keeps its own `_PARAMS_POLL_S` cadence, and
      `web/frontend/dashboard.html` is not modified. What changed is only that clients within a
      40 ms window now serialise the *same* dict — which they already did in content.
- [x] `tests/test_web.py`'s existing `params_snapshot()` assertions pass unchanged. The cache sits
      in front of the function, not inside it, and `TestParamsSnapshot` still calls it directly.
- [ ] **DEFERRED — needs the robot.** Three `/params` clients plus one `/video` client, with
      `[control] N Hz` holding its normal rate and `sess_blocks_dropped` not climbing. This is the
      criterion that measures the thing the ticket is actually about — contention with the audio
      worker and the control thread — and it cannot be observed off-hardware.

## Note on the two follow-on options

Neither taken.

**Emit only on change** would cut more, but it trades a cadence guarantee for a bandwidth saving on
a link that is one LAN hop long, and it needs the frontend audited for anything that assumes a tick
(the ticket flags `voice_turn_id` as safe; the rest was not checked). Not worth it for a dashboard.

**Encode the JSON once per snapshot** is the tempting one — every client currently runs
`json.dumps` over the same ~70-key dict — and it is a strict win with no contract change, since all
clients within a window would emit identical bytes. Left out only to keep this diff to one idea:
the cache is what the acceptance criteria are written against, and `cached_snapshot` is general
enough that caching the encoded string instead is a two-line follow-up if the JSON cost ever shows
up in a profile.

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
