"""Safety/lifecycle checks with fake devices; never invokes ADB or usbmuxd."""

import argparse
import asyncio
import signal
import socket
import types
import unittest
from unittest.mock import patch

import usb_relay


class FakeForwarder:
    instances = []

    def __init__(self, serial, dst_port, src_port, listening_event, usbmux_connection_type):
        assert usbmux_connection_type == "USB"
        self.src_port = src_port
        self.ready = listening_event
        self.stopped = asyncio.Event()
        self.server = None
        self.instances.append(self)

    async def start(self, address):
        self.server = await asyncio.start_server(lambda r, w: w.close(), address, self.src_port)
        self.ready.set()
        try:
            await self.stopped.wait()
        finally:
            self.server.close()
            await self.server.wait_closed()

    def stop(self):
        self.stopped.set()


class RelayTests(unittest.TestCase):
    def setUp(self):
        FakeForwarder.instances = []
        self.args = argparse.Namespace(
            adb="not-called", rokid_serial="TESTDEVICE", activity="org.opensqz.openglass.rokid.debug/.MainActivity",
            iphone_udid=None, local_port=0, iphone_port=18080, rokid_port=18080,
        )
        self.calls = []
        self.maps = []
        self.change_mapping = False
        self.reject_mapping = False

    async def list_devices(self):
        return [types.SimpleNamespace(serial="TESTPHONE", is_usb=True)]

    def fake_adb(self, args, *command):
        self.calls.append(command)
        if command == ("reverse", "--list"):
            return "\n".join(f"UsbFfs {remote} {local}" for remote, local in self.maps)
        if command[:2] == ("reverse", "--no-rebind"):
            if self.reject_mapping:
                raise RuntimeError("mapping was claimed by another session")
            self.maps = [(command[2], command[3])]
        if command[:3] == ("shell", "am", "start"):
            if self.change_mapping:
                self.maps = [("tcp:18080", "tcp:19000")]
            asyncio.get_running_loop().call_soon(signal.getsignal(signal.SIGINT), signal.SIGINT, None)
        if command[:2] == ("reverse", "--remove"):
            self.maps = []
        return ""

    def execute(self, health=None):
        package = types.ModuleType("pymobiledevice3")
        package.usbmux = types.SimpleNamespace(list_devices=self.list_devices)
        forwarder = types.ModuleType("pymobiledevice3.tcp_forwarder")
        forwarder.UsbmuxTcpForwarder = FakeForwarder
        modules = {"pymobiledevice3": package, "pymobiledevice3.tcp_forwarder": forwarder}
        with patch.dict("sys.modules", modules), patch.object(usb_relay, "adb", self.fake_adb), patch.object(
            usb_relay, "read_health", side_effect=health or (lambda port: {"service": "openglass-ios"})
        ):
            asyncio.run(usb_relay.run(self.args))

    def test_preexisting_reverse_aborts_without_changes(self):
        self.maps = [("tcp:18080", "tcp:12345")]
        with self.assertRaisesRegex(RuntimeError, "already has"):
            self.execute()
        self.assertEqual(self.calls, [("reverse", "--list")])
        self.assertEqual(FakeForwarder.instances, [])

    def test_busy_port_does_not_touch_mapping(self):
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            self.args.local_port = occupied.getsockname()[1]
            with self.assertRaises(OSError):
                self.execute()
        self.assertEqual(self.calls, [("reverse", "--list")])

    def test_unreachable_receiver_closes_listener_without_launch(self):
        with self.assertRaisesRegex(RuntimeError, "keep the app|Keep the app"):
            self.execute(health=ConnectionRefusedError("app not running"))
        self.assertEqual(self.calls, [("reverse", "--list")])
        self.assertFalse(FakeForwarder.instances[0].server.is_serving())

    def test_success_launch_and_cleanup_only_own_mapping(self):
        self.execute()
        self.assertIn(("reverse", "--no-rebind", "tcp:18080", "tcp:0"), self.calls)
        self.assertIn(("reverse", "--remove", "tcp:18080"), self.calls)
        self.assertTrue(any(command[:3] == ("shell", "am", "start") for command in self.calls))
        self.assertEqual(self.maps, [])
        self.assertFalse(FakeForwarder.instances[0].server.is_serving())

    def test_replaced_mapping_is_left_untouched(self):
        self.change_mapping = True
        self.execute()
        self.assertNotIn(("reverse", "--remove", "tcp:18080"), self.calls)
        self.assertEqual(self.maps, [("tcp:18080", "tcp:19000")])

    def test_no_rebind_race_failure_does_not_remove_mapping(self):
        self.reject_mapping = True
        with self.assertRaisesRegex(RuntimeError, "another session"):
            self.execute()
        self.assertNotIn(("reverse", "--remove", "tcp:18080"), self.calls)
        self.assertFalse(FakeForwarder.instances[0].server.is_serving())

    def test_non_iphone_health_is_rejected(self):
        response = types.SimpleNamespace(read=lambda limit: b'{"service":"other"}')
        context = unittest.mock.MagicMock()
        context.__enter__.return_value = response
        opener = types.SimpleNamespace(open=lambda *a, **kw: context)
        with patch.object(usb_relay.urllib.request, "build_opener", return_value=opener):
            with self.assertRaisesRegex(RuntimeError, "not the OpenGlass iOS"):
                usb_relay.read_health(18080)


if __name__ == "__main__":
    unittest.main()
