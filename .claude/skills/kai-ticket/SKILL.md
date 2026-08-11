---
name: kai-ticket
description: Write a new engineering ticket in docs/tickets/ in the house format and index it, or update an existing one after implementation. Use when a finding should be captured as a spec rather than fixed on the spot, or when a ticket has landed and needs its resolution recorded.
---

# Kai engineering tickets

`docs/tickets/` holds findings from the 2026-08-10 two-lens codebase review, converted into
implementation-ready **specifications, not change records**. New findings join them in the same
format so the set stays one thing.

## Filename and ID

`<ID>-<kebab-summary>.md` — e.g. `R7-tts-subprocesses-outlive-process.md`.

`R` = robotics-engineering lens (real-time behaviour, hardware, actuation, latency).
`S` = software-engineering lens (structure, correctness, security, testing, dependencies).
Take the next free number in that prefix. A grouped finding splits as `S11a`–`S11d`.

## Format

```markdown
# <ID> — <one-line summary, same as the README table>

| | |
|---|---|
| **Tier** | 1–4 |
| **Severity** | Low / Medium / High |
| **Effort** | Small / Medium / Large |
| **Confidence** | e.g. Medium-High |
| **Lens** | Robotics / Software |

## Location
- `path.py` — the function, and the exact line or expression
- `config/x.py` — the constant involved, and the comment that documents it

## Problem
What the code does. Mechanism, not adjectives.

## Why it matters
The measured consequence, and the observable robot behaviour it produces. Cite the measurement
in `config/` or the CHANGELOG that makes it matter.

## Acceptance criteria
A checklist. This is the contract the implementer works to — testable, not aspirational.

## Suggested approach
The smallest change that satisfies the criteria, written with the whole review in view.
```

**Tier** ranks severity × inverse effort: 1 = high impact / small effort (do first, self-contained,
individually revertible), 2 = high impact / medium, 3 = high impact / large (plan these), 4 =
cleanup.

## Then index it

Add a row to the right tier table in `docs/tickets/README.md`, in priority order within the tier,
with a one-sentence summary in the same voice as its neighbours. If the new ticket is cheaper or
safer before or after an existing one, add it to **§Cross-ticket dependencies** with the reason —
that section is the most useful part of the file.

## Recording a ticket as done

Do not delete it. Append a `## Resolution` section: what landed, which acceptance criteria are met,
which are deliberately not, and the commit or date. Then update `docs/tickets/README.md` — including
the "**Nothing here has been implemented.**" line at the top, once that stops being true — and add a
`CHANGELOG.md` entry if the behaviour observably changed.

## Writing voice

Match the existing tickets: mechanism over adjectives, the measurement quoted, the failure stated as
inputs → observable behaviour. The review's own framing is worth preserving — most of what these
describe is **policy, not defect**, and the defect density is low. Do not inflate a policy call into
a bug to make a ticket sound urgent.
