# S9 — Fail-open blanket excepts swallow bugs silently

> **Status: FIXED** — `fix/rag-silent-failures`. `ai/rag.py` gained a rate-limited `_note_error()`
> plus `rag_errors` / `rag_last_error` on `/params`; `ai/session.py`'s presence handlers log through
> a throttled `_note_presence_error()`. Suite green (1182 passed). Every acceptance criterion is met
> — nothing deferred.

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

- [x] Every blanket `except Exception` listed above logs `{type(exc).__name__}: {exc}` with the
      layer that failed (`retrieve_context`, `embed_query`, `load_index`), and none of them re-raise.
- [x] Logging is rate-limited — per *distinct* error, once per `_ERROR_LOG_INTERVAL_S` (60 s). A new
      failure mode logs immediately even inside the window, which is the behaviour
      `CameraSupervisor._report_failure` has; a repeat of the same one is suppressed. Both are
      pinned by tests.
- [x] `/params` gains `rag_errors` (monotonic across the process) and `rag_last_error`, via
      `rag.status()` merged in `Dashboard.params_snapshot()`.
- [x] Fail-open behaviour is bit-identical: `retrieve_context` still returns `""`, `embed_query`
      still returns `None`, presence still clears the absence clock. The counter is incremented
      before the existing return in every case; no new exception can escape.
- [x] A test asserts an exception inside `_build_query` produces `""` **and** increments the
      counter — both halves, in separate cases, so a regression in either is attributable.
- [x] The `except Exception` in `tick()` keeps its existing behaviour of clearing
      `_face_absent_since`; only the logging is added.

**Judgement call made while fixing, not in the original ticket:** a **missing** index
(`FileNotFoundError` from `INDEX_PATH.read_text()`) is deliberately *not* counted. A fresh checkout
has no index, and that means "nothing indexed yet", not a fault — counting it would leave
`rag_errors` non-zero on every healthy first boot and make the number useless as a signal. A
*malformed* index still counts. Both cases are pinned by tests.

**Scope note:** `_warn_if_stale()`'s `except Exception` was left silent. It guards a `stat()` sweep
whose only job is to print an advisory, so a failure there costs a warning about a warning — adding
a log line would be noise, not signal.

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
