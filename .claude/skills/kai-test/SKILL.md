---
name: kai-test
description: Run Kai's test suite and interpret the result against the known baseline and the two named flakes. Use when asked to run the tests, check the suite is green, or work out whether a failure is real.
---

# Running Kai's tests

```bash
python -m pytest -q
```

Runs anywhere — **no hardware, no network, no models, no sound card.** Every audio, vision and
serial boundary is a fake, every clock is injected. It works on the Windows box and on the Jetson;
prefer local, it needs nothing from the robot.

`python -m unittest discover -s tests` also works and is what the Jetson-side runs have used.
`pytest-timeout` is **not** installed — `--timeout=` errors out.

## Baseline

**1185 passed, 2675 subtests, ~51 s** on Windows (measured 2026-08-10; ~12 s warm on the Jetson).
Take the baseline *before* your change — the working tree usually carries unrelated in-flight work,
and a concurrent session may be editing the same share.

The subtest count is high because table-driven cases use `subTest`. Both numbers move when tests are
added; quote both.

## The two known flakes

Neither is a product bug. Both are test-isolation defects. Check here before blaming a change.

| Flake | Root cause | Tell |
|---|---|---|
| `test_session.TestWhisperTierScan::test_cooldown_blocks_a_second_scan` and `::test_matched_phrase_is_reported_on_params` | The tests drive a fake clock at `T0 = 1000.0`, but `ConversationSession._on_scan_done` reads the **real** `time.monotonic()`. Under ~1000 s of uptime the arithmetic goes negative. | **Fails for ~17 min after every reboot, then passes forever.** Check: `python -c "import time; print(time.monotonic())"` |
| `test_tts.TestStop::test_terminates_both_stages_at_once` | `tts._synth_proc` is a module global. Session re-warm tests spawn `kai-ack-rewarm` daemon threads that outlive the `ai.session.tts.prewarm_canned` patch and overwrite it mid-test. | ~20% of full runs, passes alone every time. `[tts] WARNING: Piper synthesis failed ... No module named piper` appears during unrelated tests. |

Real fixes exist for both (inject the clock into `_on_scan_done`; join or suppress the re-warm
threads in `SessionCase.tearDown`) — worth doing deliberately, not as a drive-by inside another
change.

## Reading a failure

1. Re-run the failing test **alone**. Passing alone + failing in the suite = isolation, not logic.
2. Check uptime if it is one of the session scan tests.
3. Check `git status` and mtimes — the diff may not be yours.
4. `docs/plan/wip/known-issues.md` for anything else already recorded. Ignore its stale "863 passed".
5. If the whole suite suddenly **crawls**, suspect a new real `time.sleep` on a production path a
   test reaches — not a deadlock. `session._prewarm_bank` paces with real sleeps and `SessionCase`
   patches three constants to 0/0/1 to keep it fast.

## Reporting

Quote the actual counts and the delta from baseline. Never call the suite green without having run
it; if something failed, say so with the output, then say whether it is one of the two known flakes
and how you established that.
