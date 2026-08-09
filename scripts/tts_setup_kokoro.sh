#!/usr/bin/env bash
# Fetch everything scripts/tts_bench.py needs to A/B Kokoro-82M against the current Piper voice.
#
# Installs NOTHING into the running system and touches no config: it only unpacks two archives
# under vendor/ and voices/kokoro/. Kai keeps speaking with Piper until config/voice.py is edited,
# so this is safe to run on the robot while it is up (though the download competes for bandwidth
# and the unpack for CPU — do it outside a demo).
#
#   bash scripts/tts_setup_kokoro.sh
#
# What lands where:
#   vendor/sherpa-onnx/bin/sherpa-onnx-offline-tts   C++ CPU binary, no Python in the synth path
#   voices/kokoro/                                   Kokoro int8 multi-lang v1.1 + espeak-ng data
#
# WHY the prebuilt C++ binary rather than `pip install sherpa-onnx`: the current synth path pays a
# whole CPython + onnxruntime import on EVERY reply (`python3 -m piper`). The standalone binary has
# no interpreter to start, which is the half of the latency that is pure overhead rather than model.
#
# Disk: ~480 MB unpacked. That partition already has four known ext4 errors (see docs/known-issues),
# so check `df -h .` if the unpack fails oddly rather than assuming a bad download.

set -euo pipefail

SHERPA_VER="1.13.4"
SHERPA_TARBALL="sherpa-onnx-v${SHERPA_VER}-linux-aarch64-shared-cpu.tar.bz2"
SHERPA_URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/v${SHERPA_VER}/${SHERPA_TARBALL}"

# int8 rather than the fp32 build: ~90 MB of weights instead of ~310 MB, and Kokoro is documented as
# quantization-resilient. If the int8 voice sounds gritty, swap this for
# kokoro-multi-lang-v1_1.tar.bz2 and re-run — tts_bench.py takes a --kokoro-dir override.
MODEL_TARBALL="kokoro-int8-multi-lang-v1_1.tar.bz2"
MODEL_URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/${MODEL_TARBALL}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENDOR="${ROOT}/vendor"
DL="${VENDOR}/downloads"
mkdir -p "${DL}" "${ROOT}/voices"

need_free_mb=900
avail_mb=$(df -Pm "${ROOT}" | awk 'NR==2 {print $4}')
if [ "${avail_mb}" -lt "${need_free_mb}" ]; then
    echo "ERROR: only ${avail_mb} MB free on the project partition; need ~${need_free_mb} MB" >&2
    echo "       (download + unpack both live there). Free space or point ROOT elsewhere." >&2
    exit 1
fi

fetch() {  # url dest — resumable, so a dropped connection costs only the remainder
    local url="$1" dest="$2"
    if [ -s "${dest}" ]; then
        echo "[setup] already downloaded: $(basename "${dest}")"
        return
    fi
    echo "[setup] downloading $(basename "${dest}") ..."
    curl -fL --retry 3 --retry-delay 2 -C - -o "${dest}.part" "${url}"
    mv "${dest}.part" "${dest}"
}

fetch "${SHERPA_URL}" "${DL}/${SHERPA_TARBALL}"
fetch "${MODEL_URL}"  "${DL}/${MODEL_TARBALL}"

if [ ! -x "${VENDOR}/sherpa-onnx/bin/sherpa-onnx-offline-tts" ]; then
    echo "[setup] unpacking sherpa-onnx ..."
    tar -xjf "${DL}/${SHERPA_TARBALL}" -C "${VENDOR}"
    # The tarball unpacks to a versioned directory; normalise the name so config/voice.py and the
    # bench script can hardcode one path across upgrades.
    rm -rf "${VENDOR}/sherpa-onnx"
    mv "${VENDOR}/sherpa-onnx-v${SHERPA_VER}-linux-aarch64-shared-cpu" "${VENDOR}/sherpa-onnx"
fi

if ! ls "${ROOT}"/voices/kokoro/model*.onnx >/dev/null 2>&1; then
    echo "[setup] unpacking Kokoro ..."
    tar -xjf "${DL}/${MODEL_TARBALL}" -C "${ROOT}/voices"
    rm -rf "${ROOT}/voices/kokoro"
    mv "${ROOT}/voices/kokoro-int8-multi-lang-v1_1" "${ROOT}/voices/kokoro"
fi

BIN="${VENDOR}/sherpa-onnx/bin/sherpa-onnx-offline-tts"
export LD_LIBRARY_PATH="${VENDOR}/sherpa-onnx/lib:${LD_LIBRARY_PATH:-}"

echo "[setup] smoke test ..."
# --kokoro-dict-dir / --kokoro-lexicon are the Chinese-side jieba dict and the pronunciation
# lexicons; which of them ship varies by model revision, so pass each only if it is actually there.
# An absent path is not a no-op to this binary — it aborts.
EXTRA=()
[ -d "${ROOT}/voices/kokoro/dict" ] && EXTRA+=("--kokoro-dict-dir=${ROOT}/voices/kokoro/dict")
LEX="$(ls "${ROOT}"/voices/kokoro/lexicon*.txt 2>/dev/null | paste -sd, -)"
[ -n "${LEX}" ] && EXTRA+=("--kokoro-lexicon=${LEX}")
# The int8 build names its weights model.int8.onnx, the fp32 one model.onnx — glob rather than
# assume, so switching builds to A/B quantization needs no edit here.
MODEL="$(ls "${ROOT}"/voices/kokoro/model*.onnx | head -1)"

"${BIN}" \
    --kokoro-model="${MODEL}" \
    --kokoro-voices="${ROOT}/voices/kokoro/voices.bin" \
    --kokoro-tokens="${ROOT}/voices/kokoro/tokens.txt" \
    --kokoro-data-dir="${ROOT}/voices/kokoro/espeak-ng-data" \
    "${EXTRA[@]}" \
    --num-threads=2 --sid=0 \
    --output-filename=/tmp/kokoro_smoke.wav \
    "Hi, I'm Kai. Ask me anything about DEVCON." >/dev/null

echo
echo "[setup] OK — wrote /tmp/kokoro_smoke.wav"
echo "[setup] speaker id -> voice-name table:"
ls "${ROOT}/voices/kokoro/" | sed 's/^/           /'
echo "           (the tarball ships a README/voice list — that is the sid mapping)"
echo
echo "Next: python3 -m scripts.tts_bench --play        # A/B through the real speaker"
