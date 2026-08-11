---
name: kai-changelog
description: Add an entry to Kai's CHANGELOG.md in the house style. Use after a change that alters behaviour, after landing a ticket, or when asked to write up what changed.
---

# Writing a CHANGELOG entry

`CHANGELOG.md` is kept by hand, newest first, because it says **why** — with the measurement — and
that is not derivable from commit subjects.

## What earns an entry

Behaviour changes, measurements, and reverts. **Refactors with no observable effect do not** —
unless they change how the robot is operated or debugged.

One heading per *change*, not per commit. A change that took six commits gets one entry. Multiple
entries on one date stay in the order they were written.

**Never edit entries dated 2026-08-07 or earlier.** They were reconstructed verbatim from the old
README running log and are reproduced as originally written, claims and tense included.

## Shape

```markdown
## YYYY-MM-DD — <the symptom or the change, in plain words>

<One line of framing where it helps — e.g. "Two independent bugs behind one symptom."
Suite green: 1185 passed, 2675 subtests (was 1180).>

- **<The specific thing, stated as a fact.>** What the code did, why that produced the observed
  behaviour, and what it does now. Quote the constant, the measured duration, the line that was
  load-bearing.
- **<The second thing.>** Same again.

<A closing paragraph only when something non-obvious ties the fixes together — an ordering
constraint, a seam that is load-bearing for one of the fixes.>
```

Headline the **symptom the user saw**, not the internal cause: "Long replies were being cut off
mid-sentence, with the jaw still moving" beats "fix `_enter_speaking` deadline". The mechanism goes
in the bullets.

## Rules that make it good

- **Numbers, not adjectives.** "any answer past ~18 s was cut mid-word", "~90 words at
  `SPEAK_SEC_PER_WORD`", "measured ~6.4–9.6 s to first audio" — not "much faster".
- **Name the code.** `` `TTS_MAX_SPOKEN_CHARS` (500) ``, `session._speaking_deadline()`,
  `config/tracking.py`. Backticks for identifiers, links for tickets and docs.
- **Include the suite count when it moves**, with the previous value: `1185 passed, 2675 subtests
  (was 1180)`. Run it — do not quote a number you did not measure.
- **Say what is still true.** "The backstop is intact: a wedged paplay either publishes no end time
  or overruns the one it did." Entries that only describe the fix leave the reader unsure what
  protection remains.
- Same voice as the code comments: mechanism, measurement, and the trade-off stated plainly.

## Also check

If the entry describes a landed ticket, add its `## Resolution` section and update
`docs/tickets/README.md`. If it changes flags, cadence, wiring or tunables, `README.md`,
`docs/architecture.md`, `docs/operating.md` and `config/README.md` may now be wrong.
