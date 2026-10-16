"""Offline tests for pinned-source preparation and generated-project boundaries."""

import importlib.util
import json
from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest.mock import patch

REPOSITORY = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("prepare_upstream", REPOSITORY / "ios/scripts/prepare_upstream.py")
prepare = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prepare)


def project_fixture():
    settings = """\t\t\t\tPRODUCT_BUNDLE_IDENTIFIER = old.bundle;
\t\t\t\tINFOPLIST_KEY_CFBundleDisplayName = old;
\t\t\t\tINFOPLIST_KEY_NSLocalNetworkUsageDescription = old;
\t\t\t\tINFOPLIST_KEY_NSSpeechRecognitionUsageDescription = old;
\t\t\t\tMARKETING_VERSION = 2.8;
\t\t\t\tCURRENT_PROJECT_VERSION = 2.8;
"""
    configs = "".join(
        f"\t\t{identifier} /* {name} */ = {{\n\t\t\tisa = XCBuildConfiguration;\n\t\t\tbuildSettings = {{\n"
        + settings + "\t\t\t};\n\t\t};\n"
        for identifier, name in zip(prepare.APP_CONFIG_IDS, ("Debug", "Release"))
    )
    return """// !$*UTF8*$!
/* Begin PBXBuildFile section */
/* End PBXBuildFile section */
/* Begin PBXFileReference section */
/* End PBXFileReference section */
/* Begin PBXGroup section */
\t\tFA3FA25A2C1176E80077F732 = {
\t\t\tisa = PBXGroup;
\t\t\tchildren = (
\t\t\t);
\t\t};
/* End PBXGroup section */
\t\tFA3FA25F2C1176E80077F732 /* Sources */ = {
\t\t\tisa = PBXSourcesBuildPhase;
\t\t\tbuildActionMask = 2147483647;
\t\t\tfiles = (
                OFFICIAL /* MBV46ModelDetailViewController.swift in Sources */,
\t\t\t);
\t\t};
""" + configs


class PrepareTests(unittest.TestCase):
    def setUp(self):
        scratch = REPOSITORY / "tmp"
        scratch.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="ios-prepare-tests-", dir=scratch)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.destination = self.root / "tmp/upstream"
        self.destination.mkdir(parents=True)
        (self.root / "ios/Sources").mkdir(parents=True)
        (self.root / "ios/Sources/OpenGlassViewController.swift").write_text("import UIKit\n", encoding="utf-8")
        (self.root / "ios/Sources/RokidWireProtocol.swift").write_text("import Foundation\n", encoding="utf-8")
        self.lock = json.loads((REPOSITORY / "ios/upstream-lock.json").read_text(encoding="utf-8"))
        self.originals = {
            prepare.PROJECT: project_fixture().encode(),
            f"{prepare.APP}/SceneDelegate.swift": b"let vc = MBHomeViewController()\n",
            f"{prepare.APP}/Info.plist": plistlib.dumps({"UIFileSharingEnabled": True}),
            prepare.PACKAGES: json.dumps({"pins": [
                {"identity": name, "state": {"revision": revision}}
                for name, revision in self.lock["swift_packages"].items()
            ]}).encode(),
        }

    def run_prepare(self):
        with patch.object(prepare, "git_file", side_effect=lambda _directory, _revision, path: self.originals[path]):
            return prepare.prepare(self.root, self.destination, self.lock, "42")

    def test_generates_launch_permissions_signing_and_keeps_official_download_ui(self):
        manifest = self.run_prepare()
        project = (self.destination / prepare.PROJECT).read_text(encoding="utf-8")
        self.assertEqual(project.count("PRODUCT_BUNDLE_IDENTIFIER = org.opensqz.openglass.ios;"), 2)
        self.assertEqual(project.count("CURRENT_PROJECT_VERSION = 42;"), 2)
        self.assertIn("MBV46ModelDetailViewController.swift in Sources", project)
        self.assertEqual((self.destination / f"{prepare.APP}/SceneDelegate.swift").read_text(encoding="utf-8"), "let vc = OpenGlassViewController()\n")
        info = plistlib.loads((self.destination / f"{prepare.APP}/Info.plist").read_bytes())
        self.assertTrue(info["UIFileSharingEnabled"])
        self.assertTrue(info["NSAppTransportSecurity"]["NSAllowsLocalNetworking"])
        self.assertIn("Rokid", info["NSLocalNetworkUsageDescription"])
        self.assertIn("设备端", info["NSSpeechRecognitionUsageDescription"])
        self.assertNotIn("UIBackgroundModes", info)
        self.assertEqual(plistlib.loads((self.destination / f"{prepare.APP}/{prepare.APP}.entitlements").read_bytes()), {})
        self.assertEqual(set(manifest["sources"]), {"OpenGlassViewController.swift", "RokidWireProtocol.swift"})
        self.assertEqual((self.destination / prepare.PACKAGES).read_bytes(), self.originals[prepare.PACKAGES])

    def test_repeat_is_identical_and_removed_source_is_not_compiled(self):
        self.run_prepare()
        before = (self.destination / prepare.PROJECT).read_bytes()
        self.run_prepare()
        self.assertEqual(before, (self.destination / prepare.PROJECT).read_bytes())
        (self.root / "ios/Sources/RokidWireProtocol.swift").unlink()
        self.run_prepare()
        self.assertNotIn("RokidWireProtocol.swift", (self.destination / prepare.PROJECT).read_text(encoding="utf-8"))

    def test_rejects_output_escape_and_tmp_root(self):
        for path in (self.root, self.root / "tmp", self.root.parent / "outside", self.root / "tmp/../../outside"):
            with self.subTest(path=path), self.assertRaises(prepare.PrepareError):
                prepare.safe_generated_path(self.root, path)
        self.assertEqual(prepare.safe_generated_path(self.root, self.destination), self.destination)

    def test_rejects_symlink_escape(self):
        link = self.destination / "escape"
        try:
            link.symlink_to(self.root / "ios", target_is_directory=True)
        except OSError:
            self.skipTest("This Windows account cannot create symlinks")
        with self.assertRaises(prepare.PrepareError):
            prepare.write_generated(self.root, self.destination, "escape/modified.swift", b"bad")

    def test_rejects_upstream_patch_drift_and_bad_build_number(self):
        with self.assertRaises(prepare.PrepareError):
            prepare.patch_project(project_fixture().replace("FA3FA25A2C1176E80077F732", "CHANGED"), ["App.swift"], self.lock, "1")
        for version in ("0", "-1", "1; BAD=YES", "v1", "", "1.2.3.4"):
            with self.subTest(version=version), self.assertRaises(prepare.PrepareError):
                prepare.patch_project(project_fixture(), ["App.swift"], self.lock, version)

    def test_rejects_package_lock_drift_before_writing(self):
        self.originals[prepare.PACKAGES] = b'{"pins": []}'
        with self.assertRaises(prepare.PrepareError):
            self.run_prepare()
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_does_not_reset_an_existing_checkout_at_wrong_revision(self):
        (self.destination / ".git").mkdir()
        def fake_git(_destination, *args):
            return {
                ("rev-parse", "--show-toplevel"): str(self.destination),
                ("remote", "get-url", "origin"): self.lock["repository"],
                ("rev-parse", "HEAD"): "0" * 40,
            }[args]
        with patch.object(prepare, "git", side_effect=fake_git) as git:
            with self.assertRaises(prepare.PrepareError):
                prepare.ensure_checkout(self.destination, self.lock, True)
        self.assertEqual(git.call_count, 3)


if __name__ == "__main__":
    unittest.main()
