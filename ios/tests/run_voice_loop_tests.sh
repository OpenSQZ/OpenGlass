#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/openglass-voice-loop-tests-$$"
trap 'rm -f "$OUT"' EXIT
swiftc "$ROOT/ios/Sources/OpenGlassVoiceLoopPolicy.swift" "$ROOT/ios/tests/voice_loop/main.swift" -o "$OUT"
"$OUT"
