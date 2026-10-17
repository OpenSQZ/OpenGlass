import unittest
from unittest.mock import patch, Mock
from runtime.openglass_omni.rokid_device import RokidDevice


class RokidDeviceTests(unittest.TestCase):
    def device(self, **extra):
        return RokidDevice(dict(rokid_adb="test-adb", rokid_mode="wifi",
                                rokid_pc_url="http://192.168.1.5:18080",
                                rokid_activity="test/Main", **extra), Mock())

    def test_usb_rokid_selected_among_network_and_other_devices(self):
        d = self.device()
        d._run_bare = Mock(return_value=(0, "List of devices attached\nabc device model:RG_glasses\n"
                                        "192.168.1.2:5555 device model:RG_glasses\nphone device model:phone\n"))
        d._enable_wireless_from_usb = Mock(side_effect=RuntimeError("offline"))
        d.select()
        self.assertEqual(d.prefix, ["test-adb", "-s", "abc"])

    def test_explicit_serial_missing_does_not_choose_other_device(self):
        d = self.device(rokid_serial="missing")
        d._run_bare = Mock(return_value=(0, "abc device model:RG_glasses\n"))
        d._enable_wireless_from_usb = Mock(side_effect=RuntimeError("offline"))
        with self.assertRaises(RuntimeError): d.select()
        self.assertIsNone(d.prefix)

    def test_ambiguous_rokids_are_rejected(self):
        d = self.device()
        d._run_bare = Mock(return_value=(0, "abc device model:RG_glasses\ndef device model:RG_glasses\n"))
        with self.assertRaises(RuntimeError): d.select()

    def test_launch_passes_configured_address(self):
        for version in ("0.1.3-debug", "0.2.0-debug", "0.2.0-wifi", "0.2.0"):
            with self.subTest(version=version):
                d = self.device()
                d.run = Mock(return_value=(0, f"versionName={version}\nStatus: ok"))
                d.launch()
                args = d.run.call_args.args
                self.assertEqual(args[-3:], ("--es", "pc_base_url", "http://192.168.1.5:18080"))

    def test_old_app_does_not_silently_ignore_new_address(self):
        d = self.device();d.run = Mock(return_value=(0, "versionName=0.1.2-debug"))
        with self.assertRaises(RuntimeError): d.launch()
        self.assertEqual(d.run.call_count, 1)

    def test_remote_shell_arguments_keep_spaces_and_metacharacters(self):
        import shlex
        d = self.device()
        d._run_bare = Mock(return_value=(0, "abc device model:RG_glasses\n"))
        d.select()
        args = ("cmd", "wifi", "connect-network", "test network", "wpa2", "test' $secret; x")
        with patch("subprocess.run", return_value=Mock(returncode=0, stdout="", stderr="")) as run:
            d.run("shell", *args)
        sent = run.call_args.args[0]
        self.assertEqual(sent[:4], ["test-adb", "-s", "abc", "shell"])
        self.assertEqual(shlex.split(sent[4]), list(args))

    @patch("runtime.openglass_omni.rokid_device.time.sleep")
    def test_usb_enables_wifi_and_waits_before_wireless_adb(self, sleep):
        d = self.device()
        d._run_with_prefix = Mock(side_effect=[
            (0, ""), (0, "state DOWN"),
            (0, "inet 192.168.1.2/24"), (0, ""), (0, "")])
        d._try_wireless = Mock(return_value=True)
        d._save_addr = Mock()
        prefix = ["test-adb", "-s", "abc"]
        self.assertEqual(d._enable_wireless_from_usb(prefix), "192.168.1.2:5555")
        self.assertEqual(d._run_with_prefix.call_args_list[0].args,
                         (prefix, "shell", "svc", "wifi", "enable"))
        d._save_addr.assert_called_once_with("192.168.1.2:5555")

    @patch("runtime.openglass_omni.rokid_device.time.sleep")
    def test_no_wifi_address_does_not_claim_wireless_ready(self, sleep):
        d = self.device()
        d._run_with_prefix = Mock(return_value=(0, "state DOWN"))
        d._try_wireless = Mock()
        with self.assertRaisesRegex(RuntimeError, "暂勿拔 USB"):
            d._enable_wireless_from_usb(["test-adb", "-s", "abc"])
        d._try_wireless.assert_not_called()
