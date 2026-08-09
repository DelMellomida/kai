# wake/

Wake-word models. Kai tries three engines in order and keeps the first that initializes
(`WAKE_ENGINE_ORDER` in `config/wake.py`) — so no single vendor, key or binary blob can leave the
robot without hands-free:

| Tier | Kind | Setup | Notes |
|---|---|---|---|
| 1. `porcupine` | frame, 512 samples | account + key + aarch64 `.ppn` | best latency and accuracy |
| 2. `openwakeword` | frame, 1280 samples | trained `.onnx` + one-time front-end download | no account, no per-platform blob |
| 3. `whisper` | **utterance** | **none at all** | reuses the resident faster-whisper; ~0.4-1.0 s per check |

A tier failing is normal and logged, not an error. `sess_wake_engine` on `/params` is the ground
truth for which one is actually running — read it first when someone reports "Kai stopped hearing
me", because tier 2 or 3 winning does **not** mean tier 2 or 3 is *good*.

Pin one tier for testing with `WAKE_ENGINE_FORCE`, or `scripts/wake_test.py --engine <name>`.

---

## Tier 1: Porcupine

`config/wake.py` looks for `hey-kai.ppn` here by default (`WAKE_KEYWORD_PATHS`, resolved against the
project root so it survives any cwd — same convention as `TTS_VOICE_MODEL`).

## Getting the .ppn

1. Sign in at <https://console.picovoice.ai/> (free tier is enough).
2. Create a custom wake word: **"Hey Kai"**.
3. Download it for **Linux (ARM64 / aarch64)** — the Jetson's platform.
4. Drop the file in here as `hey-kai.ppn`.

**The platform matters.** A `.ppn` built for Windows or macOS raises
`PorcupineInvalidArgumentError` on the Jetson, and the message does not say why. If the wake word
refuses to initialize, check this first — it is the most common cause.

## Access key

The key is **not** stored in this repo. `ai/audio.py` looks for it in this order:

1. `$PICOVOICE_ACCESS_KEY`
2. `~/.config/kai/porcupine.key`

```bash
mkdir -p ~/.config/kai
printf '%s' 'YOUR_KEY' > ~/.config/kai/porcupine.key
chmod 600 ~/.config/kai/porcupine.key
```

The file is the primary source because `scripts/autostart.sh` runs under `@reboot cron` with no login
shell — and sometimes with no `HOME` at all, which is handled.

With no key, no `.ppn`, or `pvporcupine` not installed, this tier logs one reason and the chain moves
on to tier 2. Push-to-talk is unaffected in every case.

**Known hardware issue on this board.** pvporcupine resolves the CPU at *import time* by matching
`/proc/cpuinfo`'s "CPU part" against a short hardcoded list, and raises `NotImplementedError` on
anything else. The Jetson Orin's Cortex-A78AE reports `0xd42`, which is in **no** published version
(checked 2.1 → 4.0). `WAKE_CPU_PART_OVERRIDE = "0xd0b"` makes it load the bundled `cortex-a76` build
instead, which runs correctly on an A78 (same ARMv8.2-A baseline). The override is only attempted
*after* a normal import has failed on CPU detection, so it is a no-op on hardware Porcupine knows.

---

## Tier 2: openWakeWord

No account and no per-platform binary — pure onnxruntime, which is already installed here for Piper.

```bash
# --no-deps because openwakeword's dependency list pulls tflite-runtime, which has no aarch64/py3.10
# wheel; onnxruntime, numpy and scipy are already present. But its __init__ imports a custom-verifier
# module that hard-requires sklearn, so scikit-learn must be installed explicitly or `import
# openwakeword` raises ModuleNotFoundError. (Harmless if you skip it — ai/audio.py catches that and
# falls through to tier 3 — but the tier will never run.)
pip3 install --no-deps openwakeword
pip3 install requests tqdm scikit-learn

# One-time, needs network ONCE — the shared melspectrogram + speech-embedding front-end.
python3 -c "import openwakeword.utils as u; u.download_models()"
# Verify they landed; ai/audio.py REFUSES this tier unless both exist on disk, so that Kai still
# boots with the network unplugged instead of hanging on an HTTP timeout.
python3 -c "import openwakeword, pathlib; \
  print(sorted(p.name for p in (pathlib.Path(openwakeword.__file__).parent/'resources'/'models').glob('*.onnx')))"
# expect at least: ['embedding_model.onnx', 'melspectrogram.onnx']
```

If the Jetson is permanently offline, fetch those two `.onnx` elsewhere, copy them anywhere on the
board, and point `WAKE_OWW_MELSPEC_PATH` / `WAKE_OWW_EMBEDDING_PATH` at them.

### Training "hey kai"

openWakeWord ships no pretrained "hey kai", so this tier needs a model trained once — offline, and
only once.

1. Open openWakeWord's **`automatic_model_training.ipynb`** (linked from its README) in Google Colab,
   GPU runtime.
2. Target phrase: `hey kai`.
3. **Add adversarial negatives**: `kaya`, `kayo`, `okay`, `hey guys`, `hey Kyle`, `hey Kayla`. This
   pays up front for the confusions that are genuinely hard here — Tagalog `ka-` words and the
   English names that sound like "Kai". (The whisper tier hits exactly the same set; see below.)
4. Leave the other defaults. Raise `n_samples` only if the first model false-*rejects*.
5. Run to completion — **~30-60 minutes** on a Colab GPU.
6. Download **`hey_kai.onnx`**. Ignore the `.tflite` it also emits; there is no tflite runtime here.
7. Copy it to `wake/hey_kai.onnx` (that path is `WAKE_OWW_MODEL_PATH`).
8. Tune the threshold: `python3 scripts/wake_test.py --engine openwakeword` prints the live score.
   Start at `WAKE_OWW_THRESHOLD = 0.5`; raise it if a podcast in the room false-accepts, lower it if
   it false-rejects from 3 m.

---

## Tier 3: Whisper phrase spotting

Needs **nothing installed and nothing trained** — it reuses the faster-whisper instance already
resident for turns. It is the safety net that means Kai always has *some* hands-free path.

It works differently from tiers 1-2, and the trade-offs are real:

- It can only decide **after** a complete utterance, so ~0.4-1.0 s is added before the ack.
- It **transcribes nearby speech** (locally, nothing stored) to look for the phrase. That is why
  `WAKE_WHISPER_LOG_TEXT` defaults to off — otherwise sentences nobody addressed to Kai would land in
  `/tmp/face-servo.log`.
- Because the transcript already contains whatever followed the wake words, it is the only tier that
  can answer **in one breath**: *"Hey Kai, what time is it?"* skips the ack and replies directly. On
  the frame tiers you must wait for "Yes?" first, since the self-hearing gate shuts the mic during it.

Cost is bounded by discarding, without ever running Whisper, anything shorter than
`WAKE_WHISPER_MIN_UTTERANCE_S` or longer than `WAKE_WHISPER_MAX_UTTERANCE_S` (someone mid-conversation
is not saying a two-word phrase), plus a cooldown floor between checks. A quiet room costs nothing.

### If it works at your desk but not in the room

Check **`sess_rms_ambient`** on `/params` first — before touching any `WAKE_PHRASE_*` value. The
matcher is the last step and the least likely culprit; two things upstream of it fail much louder.

| symptom on `/params` | what is happening |
|---|---|
| `sess_scan_skip_long` climbing, `sess_scan_checks` flat | The room is above the hold floor, so scan utterances never close — they hit the 6 s ceiling and are discarded. **Whisper is running zero times.** `sess_rms_ambient` vs `sess_rms_hold_live` shows it. |
| `sess_scan_skip_short` climbing | Phrases are being discarded as blips before Whisper runs — `WAKE_WHISPER_MIN_UTTERANCE_S` is too high for how briefly people say "hey". |
| `sess_scan_checks` healthy, `sess_scan_matches` ~0 | Whisper *is* running and the transcript isn't matching. This is the only one that is actually about the matcher — or about `WAKE_WHISPER_SCAN_MODEL` being too weak for the room. |
| `sess_scan_skip_cooldown` high | You are retrying inside the dead window. See `WAKE_SCAN_HANGOVER_S`. |

The floors adapt to the room automatically (`WAKE_AMBIENT_*`), so `sess_rms_floor_live` and
`sess_rms_hold_live` are what the gate is really using — `sess_rms_floor` is only the configured
base. If `sess_rms_floor_live` has hit its cap (4× the base) the room is louder than this tier can
handle and the answer is tier 2, not more tuning.

Tuning is `WAKE_PHRASE_*` in `config/wake.py`, against a real mic and a real accent:

```bash
python3 scripts/wake_test.py --engine whisper --seconds 120
#   check: 1.4s -> "hey ky what time is it" (820ms) MATCH 0.86 cmd="what time is it"
```

Expect to add to `WAKE_PHRASE_BLOCKLIST`. Tagalog `ka-` words are frequent and `difflib` scores
several of them close to "kai". Two measured limitations worth knowing:

- `"hey Kaye"` **does** match. "Kaye" and "Kai" are near-homophones; the acoustic tiers cannot
  separate them either, and blocklisting `kaye` would reject legitimate wakes that Whisper renders
  that way. Accepted deliberately.
- `"sabihin mo kay Kai"` (talking *about* Kai) does **not** match — `kay` is in
  `WAKE_PHRASE_PREFIX_BLOCKLIST` precisely because it scores 0.86 against "okay".

### The name is optional here

**Only on this tier**, a bare `"hey"` is enough — `WAKE_PHRASE_SOLO_PREFIXES` in `config/wake.py`.
The name slot is where tier 3 loses real wakes: `tiny` renders "Kai" as *guy*, *gai*, *chi*, *嘿哀*,
and `WAKE_PHRASE_NAMES` only ever grows after someone has already been ignored. The prefix is one
common English word the model gets right.

Three constraints keep it from becoming a firehose, and all three are load-bearing:

- **Token 0 only** (`WAKE_PHRASE_SOLO_SCAN_TOKENS = 1`). `"hey"` starting an utterance is addressing
  someone; `"...and I was like hey, no"` is conversation.
- **`"hey"` only** — not the rest of `WAKE_PHRASE_PREFIXES`. `"okay"`/`"ok"`/`"oy"`/`"ey"` open
  ordinary sentences, and `"hi"` is how people greet each other in the room.
- **Exact match, no ratio.** `"they"` scores **0.857** against `"hey"` — the same score as `"heyy"`,
  a real drawn-out wake. Nothing separates them at three characters, so form C compares literally
  after collapsing repeated letters. Add renderings to the tuple; do not reach for a threshold.

The cost, stated plainly: **greeting any person by name wakes Kai** — `"hey Chris"`, `"hey guys"`,
`"hey everyone"`. At token 0 that is indistinguishable from a real wake, and it costs an ack plus a
listening window that self-ends. A false *reject* costs the whole feature. If the room makes this
intolerable, the fix is tier 2 above, not a stricter matcher.

Tiers 1-2 are trained blobs and still need the full "Hey Kai", so **which phrase works depends on
which tier won at startup** — pin `WAKE_ENGINE_FORCE = "whisper"` while evaluating. Set
`WAKE_PHRASE_SOLO_PREFIXES = ()` to restore strict two-word matching.

---

## Checking it works

```bash
python3 scripts/wake_test.py --seconds 60                  # whichever tier won
python3 scripts/wake_test.py --engine openwakeword         # pin one tier
```

It prints the whole chain at exit, including each skipped tier's reason. Verify the winning tier also
initializes **with the network unplugged**, so you don't discover an online activation step in front
of an audience.

Test the one-breath path with no mic at all:

```bash
curl -X POST localhost:8081/voice/wake -H 'content-type: application/json' \
     -d '{"text":"hey kai what time is it"}'      # -> runs a turn with command "what time is it"
curl -X POST localhost:8081/voice/wake -H 'content-type: application/json' \
     -d '{"text":"kaya naman"}'                   # -> 400, no wake phrase
```
