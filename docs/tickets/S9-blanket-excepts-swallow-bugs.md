# S9 — Fail-open blanket excepts swallow bugs silently

| | |
|---|---|
| **Tier** | 1 |
| **Severity** | Low-Medium |
| **Effort** | Small |
| **Confidence** | High |
| **Lens** | Software |

## Location

- `ai/rag.py` — `retrieve_context()` (`except Exception: return ""`), `embed_query()`
  (`except Exception: return None`), `load_index()` (`except Exception:` → empty cache),
  `_warn_if_stale()` (`except Exception: return`)
- `ai/session.py` — `tick()`'s presence read (`except Exception: self._face_absent_since = None`),
  `_face_state()` (`except Exception: return "unknown"`)
- `settings.py` — `load()`'s blanket handler (this one already logs — use it as the model)

## Problem

The fail-open policy is right and is documented at every site: a broken index must behave exactly as
if RAG did not exist, a broken presence feed must not end a conversation. The problem is not the
recovery, it is the **silence**. Several of these handlers discard the exception object entirely and
return a value that is indistinguishable from a legitimate negative result.

`retrieve_context()` is the sharpest case. A `TypeError` in `_build_query`, a shape change in the
index, or an `AttributeError` after a refactor all produce `""` — the same value that means "no
documents were relevant." And `""` is documented in that very module as the *dangerous* state, the
one where gemma2:2b answers DEVCON questions from pretraining instead of from the corpus.

`settings.load()` shows the pattern done correctly: same blanket catch, but it prints
`{type(exc).__name__}: {exc}` before falling back.

## Why it matters

An entire retrieval regression could ship and present only as "Kai's answers got vaguer", with
nothing anywhere in the log and no counter moving on `/params`. The failure is silent, gradual and
attributed to the model rather than to the code. The same applies, less severely, to the presence
reads: a broken snapshot callable fails open forever and looks like a camera that never sees anyone.

## Acceptance criteria

- [ ] Every blanket `except Exception` listed above logs `{type(exc).__name__}: {exc}` with enough
      context to identify the site (module tag + which layer), and none of them re-raise.
- [ ] Logging is rate-limited so a persistent fault cannot flood the log — the same principle
      `CameraSupervisor._report_failure` already applies (log on change, or at most once per N
      seconds). `[face_track] NO FACE` at 58% of a 1.5-hour log is the precedent this must not repeat.
- [ ] `/params` gains a monotonic `rag_errors` counter (and, if cheap, `rag_last_error`) so a
      persistent retrieval failure is visible over ssh without reading the log.
- [ ] Fail-open behaviour is bit-identical: `retrieve_context` still returns `""`, `embed_query`
      still returns `None`, presence still clears the absence clock. No new exception can escape.
- [ ] A test asserts that an exception raised inside `_build_query` produces `""` **and** increments
      the counter — i.e. that the failure is both survivable and observable.
- [ ] The `except Exception` in `tick()` keeps its existing behaviour of clearing
      `_face_absent_since`; only the logging is added.

## Suggested approach

Add a tiny module-local helper in `ai/rag.py` rather than repeating the rate-limit logic:

```
# sketch
_error_count = 0
_last_error_t = 0.0
_last_error = ""

def _note_error(where: str, exc: BaseException) -> None:
    global _error_count, _last_error_t, _last_error
    _error_count += 1
    detail = f"{type(exc).__name__}: {exc}"
    now = time.monotonic()
    if detail != _last_error or now - _last_error_t > 60.0:
        print(f"[rag] WARNING: {where} failed ({detail}) — answering without documents", flush=True)
        _last_error_t, _last_error = now, detail
```

Call it from each handler with the layer name (`"retrieve_context"`, `"embed_query"`,
`"load_index"`). Expose `_error_count` through a small `status()` function that
`Dashboard.params_snapshot()` merges in, alongside the existing `sess_*` keys.

For `ai/session.py`, the presence handlers are hit at 20 Hz, so the rate limit matters more than the
message — one line on the first failure and then at most one per minute is sufficient. Do not add a
counter for these; `sess_face_present == "unknown"` already reports the state, and the log line is
what explains *why*.

Deliberately out of scope: narrowing the catches. Broad catches are correct here — a wake tier that
raises `NotImplementedError` at import is the documented precedent — and narrowing them would be a
behaviour change, not an observability one.
