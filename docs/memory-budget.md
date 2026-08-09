# Memory Budget

8 GB Jetson Orin Nano with **shared CPU/GPU memory** — the GPU allocation comes out of the same
7.6 GB the OS and every process are using. This is the tightest constraint in the system and it is
the one that gets discovered by crashing into it, so: measured with robot, camera and Ollama all up.

| | |
|---|---|
| Total | 7.6 GB |
| Available, steady state | **~2.0–2.3 GB** |
| Ollama `gemma2:2b` | 2.4 GB resident (`keep_alive=-1`, 100% GPU) |
| `face_track.py` | ~1.4 GB |
| zram swap | active, ~300 MB used across 3 devices |

**`OLLAMA_NUM_CTX`** — 2048 costs ~35 MB over 1024 and is fine. **4096 hard-crashes the llama
runner** (`llama runner process has terminated: signal arrived during cgo execution`). Do not raise
it past 2048 on this hardware.

Two rules follow from the numbers:

1. **Stop `face_track.py` before changing the model or the context size.** Raising `num_ctx` forces
   a model reload, and that reload OOM'd once with the camera up — it succeeded on retry with more
   free memory. There is no headroom to absorb both at once.
2. **A runner crash self-heals, the voice turn does not.** Ollama runs `Restart=always`, so the
   service comes back on its own; the turn that triggered the crash is still lost, in front of
   whoever was talking to the robot.
