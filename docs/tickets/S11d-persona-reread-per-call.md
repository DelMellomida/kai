# S11d — `load_persona()` re-reads `persona.txt` on every LLM call

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

- `ai/llm.py` — `load_persona()`, `PERSONA_PATH`, `_DEFAULT_PERSONA`
- `ai/voice_assistant.py` — `_call_ollama()` (once per turn), `ensure_llm_warm()` (once at startup)
- `config/voice.py` — `RAG_CONTEXT_PLACEMENT` and the KV-cache-prefix reasoning in `_call_ollama`

## Problem

`load_persona()` reads `persona.txt` from disk on every call, and it is called once per LLM request.
The docstring defends this and the reasoning is sound: "cheap relative to STT+LLM latency, and gives
free 'edits apply on the next turn' behavior with no file-watcher or caching needed."

The unstated consequence is that the persona is not stable within a run. `ensure_llm_warm()` reads
it at startup to warm the model; every subsequent turn reads it again. An edit made while the robot
is running takes effect at an arbitrary point — which is the intended feature — but it also means
the system prompt can differ between two turns of the *same conversation*, and the conversation
history in `self._history` was generated under the earlier one.

There is a second-order effect worth naming. `_call_ollama()` places the RAG context in the user
turn specifically so the system prompt and the rolling history stay "byte-identical between turns",
which is what lets Ollama reuse its KV cache prefix. A persona edit mid-conversation invalidates
that prefix — correctly and unavoidably, but it means an innocuous-looking text edit costs a full
prompt re-evaluation on the next turn, which `sess_last_llm_prompt_ms` will show as a spike with no
obvious cause.

## Why it matters

Low, and the current behaviour is mostly a feature — tuning the persona without restarting is
genuinely useful and is used. This is a documentation and observability gap rather than a defect:
nothing tells you the persona changed, and the two symptoms it can produce (a mid-conversation tone
shift, a one-off prompt-eval spike) are both attributable to other things.

The per-call file read itself is immaterial: one small `read_text()` against several seconds of
STT + LLM.

## Acceptance criteria

- [ ] The live-reload behaviour is **preserved** — this ticket must not turn the persona into a
      restart-required value. That would remove a working tuning workflow.
- [ ] A persona change is detectable: when the content differs from the previously-used content,
      log one line (e.g. `[llm] persona.txt changed — the prefix cache will re-evaluate on this
      turn`). Log on change only, never per turn.
- [ ] The comparison is cheap — compare the loaded string (or a hash of it) against the last one
      used; do not add a file-watcher, and do not stat-cache in a way that defeats the reload.
- [ ] `ensure_llm_warm()`'s read and the first turn's read are consistent: if the file changed
      between them, that is logged like any other change rather than being silent.
- [ ] The existing fallbacks are untouched: an unreadable file still logs and returns
      `_DEFAULT_PERSONA`; an empty file still logs and returns `_DEFAULT_PERSONA`. Both must remain
      non-fatal.
- [ ] The docstring gains a sentence naming the two consequences (mid-conversation drift, KV prefix
      invalidation) so the trade is recorded where the decision lives, not only in this ticket.
- [ ] `tests/test_llm.py` covers the change-detection path (same content twice → one log line at
      most; changed content → a line).

## Suggested approach

A module-level `_last_persona: str | None` in `ai/llm.py`, compared at the end of `load_persona()`:

```
# sketch
if _last_persona is not None and content != _last_persona:
    print("[llm] persona.txt changed — the KV prefix cache will re-evaluate on this turn", flush=True)
_last_persona = content
```

Keep it to that. Deliberately **not** in scope:

- **Caching the file contents.** The read is cheap and the reload is the point.
- **Pinning the persona per conversation.** Snapshotting it at `_begin_session` and reusing it for
  the whole conversation would make the history internally consistent, and is arguably more correct
  — but it changes when edits take effect, which is a behavioural change to a tuning workflow that
  is actively used. If it is ever wanted, it belongs in its own ticket with the workflow question
  answered first.
- **A file-watcher.** The whole design avoids one on purpose.
