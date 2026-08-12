# A4 — Only DEVCON facts are grounded; everything else is answered from a 2B model's memory

| | |
|---|---|
| **Tier** | 1 |
| **Severity** | Medium |
| **Effort** | Small |
| **Confidence** | High |
| **Lens** | AI |

## Location

- `ai/persona.txt` — line 5: "State a DEVCON fact only if the FACTS block in this message contains
  it. If it is not there, say you are not sure. Never invent a chapter, a number, a program, a person
  or a date." Line 7: "If someone is just chatting, answer them properly first"
- `ai/rag.py` — `retrieve_context()`, which returns `""` for any turn not provably about DEVCON, and
  `NO_CONTEXT_NOTICE`, which fires only on brand-flagged turns
- `config/rag.py` — `NO_CONTEXT_NOTICE`, whose text is scoped to DEVCON
- `config/voice.py` — `OLLAMA_MODEL = "gemma2:2b"`

## Problem

The grounding rule in the persona has exactly one scope: DEVCON facts. Every other factual question
reaches gemma2:2b with an empty context and no instruction about what it may claim.

That is by construction, and the retrieval side is deliberate about it. `retrieve_context()`'s
docstring: "an unrelated question still falls through to `""` exactly as before" — the failsafe chain
(layers 4–6, including `NO_CONTEXT_NOTICE`) fires **only** when the turn is provably about DEVCON.
So on a non-DEVCON question the model gets the persona, the history, the question, and nothing else.

The persona then actively encourages it to engage: chatty, curious, a little chismosa, "answer them
properly first". There is no category of question it is told it cannot know.

## Why it matters

Inputs: a visitor at a booth asks Kai something ordinary and factual that is not about DEVCON — the
time or the date, today's weather, where the toilets are, who won a recent match, a general-knowledge
question, anything about the venue. Observable behaviour: a 2B model with no clock, no network
(`documents/` records "offline", "no cloud" as facts about Kai) and no tools produces a confident,
warm, specific-sounding answer, delivered in the same voice it uses for the facts it *is* grounded
in. A listener has no way to tell the two apart, and the whole point of the FACTS block is that Kai
sounds authoritative.

The date case is the sharpest: this board has no RTC battery — `ai/session._greeting_age()`'s comment
records the clock stepping when NTP lands — so Kai cannot know what day it is even in principle, and
will answer as though it does.

This is not a hypothetical class of failure for this corpus. `scripts/rag_accuracy.py`'s comment
records the model saying "Micro:bit", "Qwen" and "Google AI Suite" out loud, none of which this repo
has ever used — and that was with a document in front of it. With no document there is nothing
holding it at all.

Two things make this a small ticket rather than a large one. The failure is concentrated in a short
list of categories, and the fix is in the same file the rest of the behaviour is tuned from.

## Acceptance criteria

- [ ] `ai/persona.txt` gains a rule for what Kai cannot know, in its existing voice and at its
      existing length — one short paragraph, not a list. It must cover at minimum: the current date
      and time, the weather, anything happening in the world now, and the venue/room Kai is standing
      in. The honest answer is that Kai is a robot that runs offline with no clock, and offering to
      talk about something it does know is the persona-consistent recovery.
- [ ] It does **not** turn Kai into a refuser. Opinions, jokes, small talk, "how are you", "what can
      you do", explaining what it is and how it works — all of that stays. The rule is about
      *facts Kai has no access to*, not about engagement, and the existing chismosa framing survives
      unchanged. Verify by ear that a five-turn chat session is not noticeably more evasive.
- [ ] Reply length is unaffected. `ai/rag.format_context()`'s docstring records that the *placement*
      of a length-adjacent instruction is what broke replies twice; this rule says nothing about
      length, and the check is that a plain greeting still comes back at one sentence.
- [ ] The persona stays inside its current token budget — see **A3**. A rule added here is paid for
      on every turn including RAG turns, which are the ones already closest to `OLLAMA_NUM_CTX`. If
      it does not fit in one short paragraph, tighten it rather than growing the file.
- [ ] Verified against a small fixed question set, run before and after, and recorded: date, time,
      weather, "where's the bathroom", one general-knowledge question, plus three controls that must
      still be answered normally (a DEVCON question, "how are you", "what can you do"). Ten
      questions is enough. If **A2** has landed, these become cases in it instead.
- [ ] Nothing in `ai/rag.py` or `config/rag.py` changes. Extending the failsafe chain to non-DEVCON
      turns would mean returning a context block for questions retrieval correctly declined, and the
      "" fall-through is a documented, measured decision.

## Suggested approach

One paragraph in `ai/persona.txt`, placed after the DEVCON grounding rule (line 5) so the two read as
one policy rather than as two unrelated instructions, and before the "DEVCON is your favourite
subject" line so the pivot lands naturally.

Keep it concrete and short. Something in the shape of: *you have no clock, no internet and no idea
what is happening outside this room, so you cannot know the date, the time, the weather or the news —
say so plainly and offer what you do know.* Concrete categories beat an abstract rule on a 2B model,
which is the same reasoning `format_context()` uses for its short imperative lines.

Two things worth knowing before writing it:

- **Position matters more than wording here.** `persona.txt` is folded into the first user turn
  (gemma2 has no system role — see `format_context()`'s note), and on a RAG turn the FACTS block sits
  after it in the strongest slot. So this rule is competing with that block for a 2B model's
  attention. Keeping it adjacent to the DEVCON grounding rule, which demonstrably works, is the
  cheapest way to borrow its weight.
- **It is live-reloaded** (**S11d**), so it applies from the next turn with no restart — which makes
  the before/after comparison easy to take, and also means an edit at a venue takes effect
  mid-conversation. Take the measurement deliberately rather than by editing during a demo.
