#!/usr/bin/env bash
# Build an unsigned device IPA for local signing on Windows. No Apple secrets.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
UPSTREAM="${ROOT}/tmp/MiniCPM-V-Apps"
BUILD_ROOT="${ROOT}/tmp/ios-build"
ARTIFACTS="${ROOT}/artifacts"
BUILD_NUMBER="${GITHUB_RUN_NUMBER:-1}"
export ROOT UPSTREAM BUILD_ROOT ARTIFACTS

if [[ "$(uname -s)" != Darwin ]]; then
    echo "build_ipa.sh requires macOS with Xcode; run the iOS IPA GitHub Actions workflow from Windows." >&2
    exit 1
fi
python3 "${ROOT}/ios/scripts/prepare_upstream.py" --build-number "${BUILD_NUMBER}"

# Check every generated root before upstream's build script cleans its own dirs.
python3 - <<'PY'
import os
from pathlib import Path
root = Path(os.environ['ROOT']).resolve()
upstream = Path(os.environ['UPSTREAM']).resolve()
if not upstream.is_relative_to(root / 'tmp'):
    raise SystemExit('Upstream build root escapes repository tmp/')
for relative in ('llama.cpp-omni/build-ios-sim', 'llama.cpp-omni/build-ios-device',
                 'llama.cpp-omni/build-macos', 'llama.cpp-omni/build-apple',
                 'MiniCPM-V-demo/thirdparty/llama.xcframework'):
    if not (upstream / relative).resolve().is_relative_to(upstream):
        raise SystemExit('Upstream build output resolves outside its checkout')
for name in ('BUILD_ROOT', 'ARTIFACTS'):
    if not Path(os.environ[name]).resolve().is_relative_to(root):
        raise SystemExit(name + ' escapes repository')
PY

FRAMEWORK="${UPSTREAM}/MiniCPM-V-demo/thirdparty/llama.xcframework"
FRAMEWORK_STAMP="${UPSTREAM}/MiniCPM-V-demo/thirdparty/.openglass-framework-key"
FRAMEWORK_KEY="$(python3 - <<'PY'
import hashlib, os, subprocess
from pathlib import Path
payload = (Path(os.environ['ROOT']) / 'ios/upstream-lock.json').read_bytes()
payload += subprocess.check_output(['xcodebuild', '-version'])
payload += subprocess.check_output(['uname', '-m'])
print(hashlib.sha256(payload).hexdigest())
PY
)"
if [[ ! -f "${FRAMEWORK}/Info.plist" || ! -f "${FRAMEWORK_STAMP}" || "$(cat "${FRAMEWORK_STAMP}")" != "${FRAMEWORK_KEY}" ]]; then
    (cd "${UPSTREAM}" && MINIMAL_MODE=ios-device bash scripts/build_xcframework.sh)
    printf '%s\n' "${FRAMEWORK_KEY}" > "${FRAMEWORK_STAMP}"
fi

mkdir -p "${BUILD_ROOT}" "${ARTIFACTS}"
PROJECT="${UPSTREAM}/MiniCPM-V-demo.xcodeproj"
COMMON_ARGS=(
    -project "${PROJECT}" -scheme MiniCPM-V-demo
    -clonedSourcePackagesDirPath "${BUILD_ROOT}/SourcePackages"
    -onlyUsePackageVersionsFromResolvedFile
)
xcodebuild "${COMMON_ARGS[@]}" -resolvePackageDependencies 2>&1 | tee "${BUILD_ROOT}/resolve.log"
xcodebuild "${COMMON_ARGS[@]}" \
    -configuration Release -sdk iphoneos -destination 'generic/platform=iOS' \
    -derivedDataPath "${BUILD_ROOT}/DerivedData" -disableAutomaticPackageResolution \
    CODE_SIGNING_ALLOWED=NO CODE_SIGNING_REQUIRED=NO CODE_SIGN_IDENTITY='' \
    DEVELOPMENT_TEAM='' CODE_SIGN_ENTITLEMENTS='' \
    build 2>&1 | tee "${BUILD_ROOT}/xcodebuild.log"

APP="${BUILD_ROOT}/DerivedData/Build/Products/Release-iphoneos/MiniCPM-V-demo.app"
export APP
python3 - <<'PY'
import json, os, plistlib, subprocess
from pathlib import Path
root, app = Path(os.environ['ROOT']), Path(os.environ['APP'])
lock = json.loads((root / 'ios/upstream-lock.json').read_text())
info = plistlib.loads((app / 'Info.plist').read_bytes())
if info.get('CFBundleIdentifier') != lock['bundle_identifier']:
    raise SystemExit('Built app has the wrong bundle identifier')
if info.get('CFBundleDisplayName') != lock['display_name']:
    raise SystemExit('Built app has the wrong display name')
if info.get('CFBundleShortVersionString') != lock['version']:
    raise SystemExit('Built app has the wrong version')
if (app / 'embedded.mobileprovision').exists():
    raise SystemExit('Unsigned build unexpectedly contains a provisioning profile')
if any(app.rglob('*.gguf')) or any(app.rglob('*.safetensors')):
    raise SystemExit('Model weights must not be packaged into the IPA')
manifest = json.loads((Path(os.environ['UPSTREAM']) / '.openglass-prepared.json').read_text())
manifest.update({'signed': False, 'xcode': subprocess.check_output(['xcodebuild', '-version'], text=True).strip(),
                 'commit': subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()})
(Path(os.environ['ARTIFACTS']) / 'build-info.json').write_text(json.dumps(manifest, indent=2) + '\n')
PY

# mktemp creates a fresh staging tree; no broad rm -rf or stale Payload merging.
STAGING="$(mktemp -d "${BUILD_ROOT}/package.XXXXXX")"
mkdir -p "${STAGING}/Payload"
ditto "${APP}" "${STAGING}/Payload/OpenGlass.app"
IPA="${ARTIFACTS}/OpenGlass-1.0.ipa"
ditto -c -k --keepParent "${STAGING}/Payload" "${STAGING}/OpenGlass-1.0.ipa"
mv -f "${STAGING}/OpenGlass-1.0.ipa" "${IPA}"
unzip -tq "${IPA}"
echo "Unsigned IPA ready: ${IPA}"
echo "Sign this IPA on your Windows computer before installing on iPhone."
