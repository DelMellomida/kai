"""Talking to Ollama: the persona, the prompt, the request, and what the timings mean.

An HTTP client for a service that happens to run on the same box. It has nothing to do with
microphones, jaws or conversation state, which is why it is no longer inside the class that owns
those — ai/voice_assistant.py calls in here for one thing, a reply to a line of text.

The three timing numbers Ollama returns on every non-streamed response are the only thing that
tells the causes of "Kai feels slow" apart, and each has a different fix:

  prompt_eval_*  how long the PROMPT took. Large means the KV cache prefix is being invalidated
                 every turn (see the context-placement note in VoiceAssistant._call_ollama).
  eval_*         token generation. A tok/s well under the GPU figure means the model got placed on
                 CPU — which log_model_placement() exists to say out loud.
  load_duration  non-zero means the model was evicted and reloaded, ~48 s on this box.

Every logging path here is best-effort: a partial or mocked response logs nothing and returns
zeros rather than raising into a turn.
"""

from __future__ import annotations

from pathlib import Path

import requests

from config.voice import (
    MAX_HISTORY_TURNS, OLLAMA_KEEP_ALIVE, OLLAMA_LOG_TIMINGS, OLLAMA_MODEL, OLLAMA_NUM_CTX,
    OLLAMA_NUM_GPU, OLLAMA_NUM_PREDICT, OLLAMA_PS_TIMEOUT_S, OLLAMA_TIMEOUT_S, OLLAMA_URL,
)

# Fallback only — the real, editable persona lives in persona.txt (see load_persona()) so it
# can be tuned without touching code or restarting the service.
_DEFAULT_PERSONA = (
    "You are Kai, a small friendly companion robot built by Devcon Philippines. "
    "You have a camera for eyes and a servo neck that lets you look toward whoever "
    "is talking to you. Speak warmly and simply, like a curious, upbeat companion — "
    "not like a generic assistant. Keep replies short: 1-3 sentences, plain "
    "conversational text, no markdown, no lists, no code blocks."
)
PERSONA_PATH = Path(__file__).parent / "persona.txt"


def load_persona() -> str:
    """Read persona.txt fresh on every call — cheap relative to STT+LLM latency, and gives
    free 'edits apply on the next turn' behavior with no file-watcher or caching needed.
    Falls back to _DEFAULT_PERSONA on any read failure or empty content — must never hard-fail."""
    try:
        content = PERSONA_PATH.read_text().strip()
    except OSError as exc:
        print(f"[voice_assistant] WARNING: could not read {PERSONA_PATH} ({exc}) — using default persona")
        return _DEFAULT_PERSONA
    if not content:
        print(f"[voice_assistant] WARNING: {PERSONA_PATH} is empty — using default persona")
        return _DEFAULT_PERSONA
    return content


def build_chat_messages(system_prompt: str, history: list[dict], user_text: str) -> list[dict]:
    """Pure helper: system prompt + capped rolling history + new user turn."""
    capped = history[-(MAX_HISTORY_TURNS * 2):]
    return [{"role": "system", "content": system_prompt}, *capped, {"role": "user", "content": user_text}]


# Ollama reports its own per-request timings, in nanoseconds, on every non-streamed response.
# They were being discarded, which is what made "Kai feels slow" unactionable: these three numbers
# are the only thing that tells the causes apart, and each has a different fix.
#   prompt_eval_*  — how long the PROMPT took to evaluate. Large means the KV cache prefix is being
#                    invalidated every turn (see the context placement note in _call_ollama).
#   eval_*         — token generation. A tok/s well under the GPU figure means the model got placed
#                    on CPU (see ensure_llm_warm / log_model_placement).
#   load_duration  — non-zero means the model was evicted and reloaded, which is ~48 s on this box.
_NS_PER_MS = 1_000_000


def _tok_per_s(count, duration_ns) -> float:
    """Tokens per second from Ollama's (count, nanoseconds) pair. 0.0 when either is missing or
    zero — this only ever feeds a log line, so it must not raise on a partial response."""
    try:
        return (count / (duration_ns / 1e9)) if count and duration_ns else 0.0
    except (TypeError, ZeroDivisionError):
        return 0.0


def _log_llm_timings(data: dict, label: str = "turn") -> dict:
    """Log Ollama's own timings for one response and return them as milliseconds.

    Best-effort and never raises: a mocked or partial response (no timing fields at all) logs
    nothing and returns zeros, so this can sit on the hot path without being able to cost a reply."""
    if not isinstance(data, dict):
        return {}
    prompt_n  = data.get("prompt_eval_count") or 0
    prompt_ns = data.get("prompt_eval_duration") or 0
    gen_n     = data.get("eval_count") or 0
    gen_ns    = data.get("eval_duration") or 0
    load_ns   = data.get("load_duration") or 0
    out = {
        "llm_prompt_ms": int(prompt_ns // _NS_PER_MS),
        "llm_gen_ms":    int(gen_ns // _NS_PER_MS),
        "llm_load_ms":   int(load_ns // _NS_PER_MS),
        "llm_prompt_tokens": int(prompt_n),
        "llm_gen_tokens":    int(gen_n),
        "llm_tok_s":         round(_tok_per_s(gen_n, gen_ns), 1),
    }
    if not (prompt_ns or gen_ns):
        return out          # nothing measured (e.g. a stubbed response) — don't print an empty line
    # The load line is separate and only printed when it fires: a reload is a different event from a
    # slow turn, and burying it in the common case is how it stayed invisible.
    if out["llm_load_ms"]:
        print(f"[llm] MODEL RELOADED: {out['llm_load_ms']}ms — placement was re-decided, "
              f"check `ollama ps` for the GPU/CPU split", flush=True)
    if not OLLAMA_LOG_TIMINGS:
        return out          # the reload warning above stays regardless — it is rare and actionable
    print(f"[llm] {label}: prompt {out['llm_prompt_tokens']} tok in {out['llm_prompt_ms']}ms "
          f"({_tok_per_s(prompt_n, prompt_ns):.0f} tok/s) | "
          f"gen {out['llm_gen_tokens']} tok in {out['llm_gen_ms']}ms "
          f"({out['llm_tok_s']:.1f} tok/s)", flush=True)
    return out


def _ollama_request(messages: list[dict]) -> dict:
    """POST to Ollama's chat endpoint and return the parsed response body. Raises RuntimeError with
    a human-readable message on failure.

    Returns the decoded JSON rather than the Response object so callers read the body — and the
    timing fields above — exactly once."""
    options = {"num_ctx": OLLAMA_NUM_CTX}
    if OLLAMA_NUM_GPU is not None:      # None = let Ollama auto-decide the GPU/CPU split (fast path)
        options["num_gpu"] = OLLAMA_NUM_GPU
    if OLLAMA_NUM_PREDICT is not None:  # bound the reply: unbounded generation was paid for twice,
        options["num_predict"] = OLLAMA_NUM_PREDICT   # once generating and again synthesizing it
    try:
        resp = requests.post(
            OLLAMA_URL,
            json={
                "model": OLLAMA_MODEL,
                "messages": messages,
                "stream": False,
                "keep_alive": OLLAMA_KEEP_ALIVE,
                "options": options,
            },
            timeout=OLLAMA_TIMEOUT_S,
        )
        resp.raise_for_status()
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(
            f"Ollama unavailable — check `ollama pull {OLLAMA_MODEL}` "
            f"and that the service is running ({exc})"
        ) from exc
    return resp.json()


def log_model_placement() -> dict:
    """Log whether Ollama actually put the model on the GPU, by asking `/api/ps`.

    This is the failure config/voice.py already documents but nothing ever reported: Ollama decides
    the CPU/GPU split at LOAD time from whatever memory is free, OLLAMA_KEEP_ALIVE=-1 then pins that
    choice for the life of the service, and after hours of uptime GPU fragmentation produces a
    partial offload that runs at roughly half speed. From the outside that is indistinguishable from
    "Kai is being slow today", which is exactly why it needs a line in the log.

    Purely diagnostic and entirely best-effort — every failure path returns {} after at most a
    warning, so this can never affect a reply. Returns the parsed size/size_vram pair when known."""
    # Derived from OLLAMA_URL so there is one host to configure, not two. The chat endpoint is
    # ".../api/chat"; the process list is its sibling.
    ps_url = OLLAMA_URL.rsplit("/", 1)[0] + "/ps"
    try:
        resp = requests.get(ps_url, timeout=OLLAMA_PS_TIMEOUT_S)
        resp.raise_for_status()
        models = resp.json().get("models") or []
    except (requests.exceptions.RequestException, ValueError, AttributeError) as exc:
        print(f"[llm] could not read model placement from {ps_url} ({exc})", flush=True)
        return {}
    entry = next((m for m in models if m.get("name", "").startswith(OLLAMA_MODEL.split(":")[0])),
                 None)
    if entry is None:
        print(f"[llm] {OLLAMA_MODEL} is not loaded — the next reply pays the model load", flush=True)
        return {}
    total = entry.get("size") or 0
    vram  = entry.get("size_vram") or 0
    if not total:
        return {}
    pct = 100.0 * vram / total
    mb = 1024 * 1024
    if pct >= 99.0:
        print(f"[llm] {entry.get('name')} fully on GPU ({vram // mb} MB VRAM)", flush=True)
    else:
        # The actionable half: this is the ~2x slowdown, and the fix is a restart/reboot to
        # defragment, not a config change.
        print(f"[llm] WARNING: {entry.get('name')} is only {pct:.0f}% on GPU "
              f"({vram // mb} of {total // mb} MB) — generation will run roughly half speed. "
              f"Restart ollama (or reboot) to defragment GPU memory before relying on it.",
              flush=True)
    return {"size": total, "size_vram": vram, "gpu_pct": round(pct, 1)}
