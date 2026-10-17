#!/usr/bin/env python3
"""Prepare a pinned, disposable iOS build tree. Requires Python 3.10+ and Git.

Only files below this repository's tmp/ directory may be generated. Each run
derives patched upstream files from the locked Git object, so it is idempotent
and never accumulates edits. The upstream model download UI is left intact.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
APP = "MiniCPM-V-demo"
PROJECT = f"{APP}.xcodeproj/project.pbxproj"
PACKAGES = f"{APP}.xcodeproj/project.xcworkspace/xcshareddata/swiftpm/Package.resolved"
APP_CONFIG_IDS = ("FA3FA2782C1176E90077F732", "FA3FA2792C1176E90077F732")
LOCAL_NETWORK_DESCRIPTION = "接收同一局域网内 Rokid 眼镜的图像和麦克风音频。"
SPEECH_DESCRIPTION = "在 iPhone 上将眼镜麦克风语音转换为文字；仅使用设备端识别。"


class PrepareError(RuntimeError):
    pass


def git(directory: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(directory), *args], check=True,
        stdout=subprocess.PIPE, encoding="utf-8", errors="strict",
    )
    return result.stdout.strip()


def git_file(directory: Path, revision: str, path: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(directory), "show", f"{revision}:{path}"])


def safe_generated_path(root: Path, path: Path) -> Path:
    """Reject the repository itself, outside directories, and escaping symlinks."""
    root = root.resolve()
    temporary = (root / "tmp").resolve()
    if temporary != root / "tmp":
        raise PrepareError("tmp/ must be a real directory inside the repository")
    resolved = path.resolve()
    if resolved == temporary or not resolved.is_relative_to(temporary):
        raise PrepareError(f"Generated path must be strictly below {temporary}: {path}")
    return resolved


def read_lock(root: Path) -> dict:
    lock = json.loads((root / "ios/upstream-lock.json").read_text(encoding="utf-8"))
    for revision in (lock["commit"], lock["submodule"]["commit"], *lock["swift_packages"].values()):
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise PrepareError("Upstream dependencies must use full 40-character Git SHAs")
    if lock["submodule"]["path"] != "llama.cpp-omni":
        raise PrepareError("Unexpected inference submodule path")
    return lock


def ensure_checkout(destination: Path, lock: dict, skip_submodules: bool) -> None:
    if not destination.exists():
        destination.mkdir(parents=True)
    if not (destination / ".git").exists():
        if any(destination.iterdir()):
            raise PrepareError("Refusing to initialize a nonempty, non-Git destination")
        git(destination, "init")
        git(destination, "remote", "add", "origin", lock["repository"])
        git(destination, "fetch", "--depth", "1", "origin", lock["commit"])
        git(destination, "checkout", "--detach", "FETCH_HEAD")
    if Path(git(destination, "rev-parse", "--show-toplevel")).resolve() != destination.resolve():
        raise PrepareError("Destination is not the root of its own Git checkout")
    if git(destination, "remote", "get-url", "origin").rstrip("/") != lock["repository"]:
        raise PrepareError("Existing checkout has a different origin; choose a fresh tmp/ directory")
    if git(destination, "rev-parse", "HEAD") != lock["commit"]:
        raise PrepareError("Existing checkout is not at the locked commit; no reset was performed")
    managed_files = {PROJECT, PACKAGES, f"{APP}/SceneDelegate.swift", f"{APP}/Info.plist", f"{APP}/{APP}.entitlements"}
    changed = set(git(destination, "diff", "HEAD", "--name-only", "--ignore-submodules=all").splitlines())
    unexpected = changed - managed_files
    if unexpected:
        raise PrepareError("Upstream has edits outside generated files: " + ", ".join(sorted(unexpected)))
    recorded = git(destination, "ls-tree", "HEAD", lock["submodule"]["path"]).split()
    if len(recorded) != 4 or recorded[0] != "160000" or recorded[2] != lock["submodule"]["commit"]:
        raise PrepareError("Upstream Git tree does not match the locked inference submodule")
    if not skip_submodules:
        git(destination, "submodule", "update", "--init", "--recursive", "--depth", "1", "--checkout")
        submodule = destination / lock["submodule"]["path"]
        if git(submodule, "rev-parse", "HEAD") != lock["submodule"]["commit"]:
            raise PrepareError("Inference submodule checkout does not match its lock")
        if git(submodule, "status", "--porcelain", "--untracked-files=no"):
            raise PrepareError("Inference submodule has local edits; refusing a non-reproducible build")


def replace_once(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise PrepareError(f"Expected exactly one upstream patch anchor: {old[:100]!r}")
    return text.replace(old, new, 1)


def object_id(kind: str, name: str) -> str:
    return hashlib.sha256(f"OpenGlass:{kind}:{name}".encode()).hexdigest()[:24].upper()


def patch_project(original: str, source_names: list[str], lock: dict, build_number: str) -> str:
    if not re.fullmatch(r"[1-9][0-9]{0,8}(?:\.[0-9]{1,3}){0,2}", build_number):
        raise PrepareError("Build number must be a positive Apple-compatible numeric version")
    if not source_names or len(set(source_names)) != len(source_names):
        raise PrepareError("Expected a nonempty list of distinct Swift source files")
    if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_+.\-]*\.swift", name) for name in source_names):
        raise PrepareError("Swift source filenames must be safe PBX filenames")
    source_names = sorted(source_names)
    group_id = object_id("group", "OpenGlass")
    build_files, references, children, sources = [], [], [], []
    for name in source_names:
        reference, build = object_id("file", name), object_id("build", name)
        if reference in original or build in original:
            raise PrepareError("Generated PBX identifier collides with an upstream object")
        build_files.append(f"\t\t{build} /* {name} in Sources */ = {{isa = PBXBuildFile; fileRef = {reference} /* {name} */; }};\n")
        references.append(f'\t\t{reference} /* {name} */ = {{isa = PBXFileReference; lastKnownFileType = sourcecode.swift; path = "{name}"; sourceTree = "<group>"; }};\n')
        children.append(f"\t\t\t\t{reference} /* {name} */,\n")
        sources.append(f"\t\t\t\t{build} /* {name} in Sources */,\n")
    text = replace_once(original, "/* Begin PBXBuildFile section */\n", "/* Begin PBXBuildFile section */\n" + "".join(build_files))
    text = replace_once(text, "/* Begin PBXFileReference section */\n", "/* Begin PBXFileReference section */\n" + "".join(references))
    group = (f"\t\t{group_id} /* OpenGlass */ = {{\n\t\t\tisa = PBXGroup;\n\t\t\tchildren = (\n"
             + "".join(children) + f'\t\t\t);\n\t\t\tname = OpenGlass;\n\t\t\tpath = "{APP}/OpenGlass";\n\t\t\tsourceTree = SOURCE_ROOT;\n\t\t}};\n')
    text = replace_once(text, "/* Begin PBXGroup section */\n", "/* Begin PBXGroup section */\n" + group)
    root_group = "\t\tFA3FA25A2C1176E80077F732 = {\n\t\t\tisa = PBXGroup;\n\t\t\tchildren = (\n"
    text = replace_once(text, root_group, root_group + f"\t\t\t\t{group_id} /* OpenGlass */,\n")
    phase = "\t\tFA3FA25F2C1176E80077F732 /* Sources */ = {\n\t\t\tisa = PBXSourcesBuildPhase;\n\t\t\tbuildActionMask = 2147483647;\n\t\t\tfiles = (\n"
    text = replace_once(text, phase, phase + "".join(sources))
    settings = {
        "PRODUCT_BUNDLE_IDENTIFIER": lock["bundle_identifier"],
        "INFOPLIST_KEY_CFBundleDisplayName": json.dumps(lock["display_name"]),
        "INFOPLIST_KEY_NSLocalNetworkUsageDescription": json.dumps(LOCAL_NETWORK_DESCRIPTION, ensure_ascii=False),
        "INFOPLIST_KEY_NSSpeechRecognitionUsageDescription": json.dumps(SPEECH_DESCRIPTION, ensure_ascii=False),
        "MARKETING_VERSION": lock["version"],
        "CURRENT_PROJECT_VERSION": build_number,
    }
    # Patch only the app's two configurations, retaining upstream test settings.
    for config_id in APP_CONFIG_IDS:
        pattern = rf"(?ms)^\t\t{config_id} /\* .*? \*/ = \{{\n.*?^\t\t\}};"
        match = re.search(pattern, text)
        if match is None:
            raise PrepareError(f"Missing app build configuration {config_id}")
        config = match.group(0)
        for key, value in settings.items():
            config, count = re.subn(rf"(?m)^(\t{{4}}{key} = )[^\n;]*;", lambda m: f"{m[1]}{value};", config)
            if count != 1:
                raise PrepareError(f"Expected one {key} in app build configuration")
        text = text[:match.start()] + config + text[match.end():]
    return text


def write_generated(root: Path, destination: Path, relative: str, data: bytes) -> None:
    path = safe_generated_path(root, destination / relative)
    if not path.is_relative_to(destination):
        raise PrepareError("Generated file escapes its upstream checkout")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def prepare(root: Path, destination: Path, lock: dict, build_number: str) -> dict:
    destination = safe_generated_path(root, destination)
    source_dir = root / "ios/Sources"
    sources = sorted(source_dir.glob("*.swift"))
    for source in sources:
        if source.is_symlink() or source.resolve().parent != source_dir.resolve():
            raise PrepareError("Overlay sources must be regular files in ios/Sources")
    revision = lock["commit"]
    original = git_file(destination, revision, PROJECT).decode("utf-8").replace("\r\n", "\n")
    project = patch_project(original, [source.name for source in sources], lock, build_number)
    scene = git_file(destination, revision, f"{APP}/SceneDelegate.swift").decode("utf-8").replace("\r\n", "\n")
    scene = replace_once(scene, "let vc = MBHomeViewController()", "let vc = OpenGlassViewController()")
    info = plistlib.loads(git_file(destination, revision, f"{APP}/Info.plist"))
    info.update({"CFBundleDisplayName": lock["display_name"],
                 "NSLocalNetworkUsageDescription": LOCAL_NETWORK_DESCRIPTION,
                 "NSBonjourServices": ["_openglass._tcp"],
                 "NSSpeechRecognitionUsageDescription": SPEECH_DESCRIPTION})
    # No background-mode claim: receiving and inference require foreground use.
    info.setdefault("NSAppTransportSecurity", {})["NSAllowsLocalNetworking"] = True
    packages_data = git_file(destination, revision, PACKAGES)
    pins = {item["identity"]: item["state"]["revision"] for item in json.loads(packages_data)["pins"]}
    if pins != lock["swift_packages"]:
        raise PrepareError("Package.resolved dependencies differ from upstream-lock.json")
    outputs = {PROJECT: project.encode("utf-8"), f"{APP}/SceneDelegate.swift": scene.encode("utf-8"),
               f"{APP}/Info.plist": plistlib.dumps(info, sort_keys=False), PACKAGES: packages_data,
               # Free personal-team signing cannot use increased-memory-limit.
               f"{APP}/{APP}.entitlements": plistlib.dumps({})}
    outputs.update({f"{APP}/OpenGlass/{source.name}": source.read_bytes() for source in sources})
    for relative, content in outputs.items():
        write_generated(root, destination, relative, content)
    manifest = {"upstream_commit": revision, "submodule_commit": lock["submodule"]["commit"],
                "bundle_identifier": lock["bundle_identifier"], "version": lock["version"],
                "build_number": build_number, "sources": {source.name: hashlib.sha256(source.read_bytes()).hexdigest() for source in sources}}
    write_generated(root, destination, ".openglass-prepared.json", (json.dumps(manifest, indent=2) + "\n").encode())
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream-dir", type=Path, default=ROOT / "tmp/MiniCPM-V-Apps")
    parser.add_argument("--build-number", default=os.environ.get("GITHUB_RUN_NUMBER", "1"))
    parser.add_argument("--skip-submodules", action="store_true", help="Prepare sources only; cannot build the IPA yet")
    args = parser.parse_args()
    try:
        destination = safe_generated_path(ROOT, args.upstream_dir)
        lock = read_lock(ROOT)
        ensure_checkout(destination, lock, args.skip_submodules)
        manifest = prepare(ROOT, destination, lock, args.build_number)
    except (PrepareError, subprocess.CalledProcessError, OSError, ValueError) as exc:
        print(f"prepare_upstream: {exc}", file=sys.stderr)
        return 1
    print(f"Prepared OpenGlass {manifest['version']} ({manifest['build_number']}), {len(manifest['sources'])} Swift sources in {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
