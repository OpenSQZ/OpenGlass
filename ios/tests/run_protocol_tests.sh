#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/openglass-protocol-tests-$$"
trap 'rm -f "$OUT"' EXIT
swiftc "$ROOT/ios/Sources/RokidWireProtocol.swift" "$ROOT/ios/tests/main.swift" -o "$OUT"
"$OUT"
