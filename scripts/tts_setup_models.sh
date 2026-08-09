#!/usr/bin/env bash
# Fetch the offline TTS candidates that scripts/tts_bench.py can compare against Piper.
#
# All of them run through the SAME sherpa-onnx binary that scripts/tts_setup_kokoro.sh already
# installed at vendor/sherpa-onnx/ — swapping engines is a model download plus a different set of
# flags, not new infrastructure. That is the whole reason this list is worth benching: it costs
# bandwidth and CPU time, not architecture.
#
#   bash scripts/tts_setup_models.sh            # everything below
#   bash scripts/tts_setup_models.sh matcha     # just the ones whose name matches
#
# Installs NOTHING into the running system and touches no config. Kai keeps speaking with Piper
# until config/voice.py is edited, so this is safe to run while the robot is up — though the
# download competes for bandwidth and the unpack for CPU, so not during a demo.
#
# Disk: ~1.1 GB unpacked for the full set. The partition has four known ext4 errors
# (docs/known-issues.md), so check `df -h .` if an unpack fails oddly rather than assuming a bad
# download.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DL="${ROOT}/vendor/downloads"
VOICES="${ROOT}/voices"
REL="https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models"
VOCODER_REL="https://github.com/k2-fsa/sherpa-onnx/releases/download/vocoder-models"

FILTER="${1:-}"

# name|tarball — unpacked into voices/, one directory each. Chosen in the order tts_bench.py
# reports them, cheapest first:
#   kitten-nano     ~15M params. Establishes the speed CEILING — if this is slow on the Jetson,
#                   no offline neural engine will be fast enough and the answer is Piper + a
#                   persistent worker instead.
#   kitten-mini     same family with more capacity: the speed/quality knee.
#   matcha-ljspeech flow-matching rather than VITS, 3 sampling steps. THE PROSODY BET — a different
#                   architecture is the likeliest fix for "flat and lifeless", which is the actual
#                   complaint. Needs a separate vocoder, fetched below.
#   supertonic-3    66M, four ONNX graphs, claimed ~1.5x faster than Kokoro. Marginal on this CPU
#                   but cheap to rule in or out.
#   kokoro fp32     settles int8-vs-fp32: ONNX int8 kernels are frequently DEOPTIMIZED on ARM, so
#                   the bigger build can be the faster one. Measured int8 was 4-9x too slow.
MODELS=(
  "kitten-nano|kitten-nano-en-v0_8-int8.tar.bz2"
  "kitten-mini|kitten-mini-en-v0_8.tar.bz2"
  "matcha-ljspeech|matcha-icefall-en_US-ljspeech.tar.bz2"
  "supertonic-3|sherpa-onnx-supertonic-3-tts-int8-2026-05-11.tar.bz2"
  "kokoro-fp32|kokoro-multi-lang-v1_0.tar.bz2"
)

mkdir -p "${DL}" "${VOICES}/vocoders"

avail_mb=$(df -Pm "${ROOT}" | awk 'NR==2 {print $4}')
if [ "${avail_mb}" -lt 2500 ]; then
    echo "ERROR: only ${avail_mb} MB free; need ~2500 MB (download + unpack both live here)" >&2
    exit 1
fi

fetch() {  # url dest — resumable, so a dropped connection costs only the remainder
    local url="$1" dest="$2"
    if [ -s "${dest}" ]; then
        echo "[setup] have $(basename "${dest}")"
        return
    fi
    echo "[setup] downloading $(basename "${dest}") ..."
    curl -fL --retry 3 --retry-delay 2 -C - --progress-bar -o "${dest}.part" "${url}"
    mv "${dest}.part" "${dest}"
}

# Matcha ships its acoustic model but NOT its vocoder — that lives in a separate release, and
# sherpa aborts without it. tts_bench.py looks in the model dir first, then here.
if [ -z "${FILTER}" ] || [[ "matcha-ljspeech" == *"${FILTER}"* ]]; then
    fetch "${VOCODER_REL}/vocos-22khz-univ.onnx" "${VOICES}/vocoders/vocos-22khz-univ.onnx"
fi

for entry in "${MODELS[@]}"; do
    name="${entry%%|*}"
    tarball="${entry#*|}"
    if [ -n "${FILTER}" ] && [[ "${name}" != *"${FILTER}"* ]]; then
        continue
    fi
    fetch "${REL}/${tarball}" "${DL}/${tarball}"

    # Every sherpa tts-models tarball unpacks to a directory named exactly like itself minus the
    # suffix. Derived, NOT read out of the archive: `tar -tjf … | head -1` closes the pipe early,
    # which SIGPIPEs tar, which under `set -o pipefail` + `set -e` kills this script silently after
    # the first model. It also decompresses the whole archive just to learn one name — a real cost
    # on the 349 MB bz2. Verified against the unpack below, which is the actual check.
    dir="$(basename "${tarball}" .tar.bz2)"
    if [ -d "${VOICES}/${dir}" ]; then
        echo "[setup] have ${dir}/"
    else
        echo "[setup] unpacking ${dir}/ ..."
        tar -xjf "${DL}/${tarball}" -C "${VOICES}"
        if [ ! -d "${VOICES}/${dir}" ]; then
            echo "ERROR: ${tarball} did not unpack to ${dir}/ — check ls ${VOICES}" >&2
            exit 1
        fi
    fi

    # LICENSE matters here: this robot represents DEVCON publicly, and at least one sherpa model
    # (matcha-icefall-zh-baker) is explicitly non-commercial. Surface it rather than bury it.
    # No `| head` anywhere in here, for the SIGPIPE reason above.
    for lic in "${VOICES}/${dir}"/LICENSE*; do
        [ -f "${lic}" ] || continue
        echo "           license: $(head -c 200 "${lic}" | tr '\n' ' ' | cut -c1-100)"
        break
    done
done

echo
echo "[setup] on disk under voices/:"
ls -d "${VOICES}"/kitten-* "${VOICES}"/matcha-* "${VOICES}"/*supertonic* "${VOICES}"/kokoro* \
    2>/dev/null | sed "s|${VOICES}/|           |"
echo
echo "Next: python3 -m scripts.tts_bench --threads 4 --lines short,medium,long"
