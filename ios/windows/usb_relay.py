"""Relay a Rokid USB collector to an iPhone TCP listener without Wi-Fi.

The model runs on iPhone. Windows forwards bytes only. Uses upstream
pymobiledevice3 v11.32.0 UsbmuxTcpForwarder and a scoped ADB reverse mapping.
No signing, Apple account access, automatic pairing, or process-wide cleanup.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import re
import signal
import subprocess
import sys
import urllib.request


def adb(args: argparse.Namespace, *command: str) -> str:
    result = subprocess.run(
        [args.adb, "-s", args.rokid_serial, *command],
        capture_output=True, text=True, timeout=20, check=False,
    )
    if result.returncode:
        raise RuntimeError(f"ADB failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout.strip()


def reverse_pairs(text: str) -> list[tuple[str, str]]:
    """ADB prints one line per mapping: transport remote local."""
    pairs = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 2:
            pairs.append((fields[-2], fields[-1]))
    return pairs


def read_health(port: int) -> dict:
    # Ignore system HTTP proxies for a loopback-only request.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://127.0.0.1:{port}/health", timeout=5) as response:
        payload = json.loads(response.read(64 * 1024))
    if not isinstance(payload, dict) or payload.get("service") != "openglass-ios":
        raise RuntimeError("/health is not the OpenGlass iOS receiver.")
    return payload


async def run(args: argparse.Namespace) -> None:
    try:
        from pymobiledevice3 import usbmux
        from pymobiledevice3.tcp_forwarder import UsbmuxTcpForwarder
    except ImportError as exc:
        raise RuntimeError("Install ios/windows/requirements.txt into this Python environment first.") from exc

    devices = [device for device in await asyncio.wait_for(usbmux.list_devices(), 10) if device.is_usb]
    if args.iphone_udid:
        devices = [device for device in devices if device.matches_udid(args.iphone_udid)]
    if len(devices) != 1:
        raise RuntimeError("Expected one USB iPhone. Unlock/trust it, check Apple Mobile Device Service, or pass --iphone-udid.")
    remote, local = f"tcp:{args.rokid_port}", f"tcp:{args.local_port}"
    if any(pair[0] == remote for pair in reverse_pairs(adb(args, "reverse", "--list"))):
        raise RuntimeError(f"Rokid {remote} already has an ADB reverse mapping. Stop its owning session or choose --rokid-port. No mapping was replaced.")

    ready = asyncio.Event()
    stop = asyncio.Event()
    forwarder = UsbmuxTcpForwarder(
        devices[0].serial, args.iphone_port, args.local_port,
        listening_event=ready, usbmux_connection_type="USB",
    )
    task = asyncio.create_task(forwarder.start(address="127.0.0.1"))
    readiness = asyncio.create_task(ready.wait())
    mapping_created = False
    previous_handlers = {}
    loop = asyncio.get_running_loop()
    # signal.signal works with Windows' default Proactor loop; add_signal_handler does not.
    for sig in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[sig] = signal.getsignal(sig)
        signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
    try:
        done, _ = await asyncio.wait({task, readiness}, timeout=10, return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            await task  # Surface bind/port-conflict failures without touching ADB state.
            raise RuntimeError("USB listener stopped before startup.")
        if readiness not in done:
            raise RuntimeError("Timed out starting the local USB listener.")
        if stop.is_set():
            return
        try:
            health = await asyncio.to_thread(read_health, args.local_port)
        except Exception as exc:
            raise RuntimeError("iPhone OpenGlass /health is unavailable. Keep the app in front and start its receiver. USB discovery alone does not mean the app is installed/running.") from exc
        if stop.is_set():
            return
        # --no-rebind closes the race between the preflight list and mapping creation.
        adb(args, "reverse", "--no-rebind", remote, local)
        mapping_created = True
        result = adb(args, "shell", "am", "start", "-S", "-W", "-n", args.activity,
                     "--es", "pc_base_url", f"http://127.0.0.1:{args.rokid_port}")
        if "Error:" in result or "Exception" in result:
            raise RuntimeError(f"Collector launch failed: {result}")
        print(f"Rokid:{args.rokid_port} -> Windows loopback:{args.local_port} -> iPhone USB:{args.iphone_port}", flush=True)
        print(f"Receiver ready: image_count={health.get('image_count', 0)}, audio_packets_in={health.get('audio_packets_in', 0)}", flush=True)
        print("Keep this terminal and the iPhone app open. Ctrl+C stops this relay only.", flush=True)
        stopping = asyncio.create_task(stop.wait())
        try:
            done, _ = await asyncio.wait({task, stopping}, return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                await task
        finally:
            stopping.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stopping
    finally:
        if mapping_created:
            try:
                pairs = reverse_pairs(adb(args, "reverse", "--list"))
                if (remote, local) in pairs:
                    adb(args, "reverse", "--remove", remote)
                elif any(pair[0] == remote for pair in pairs):
                    print("Mapping changed by another session; leaving it untouched.", file=sys.stderr)
            except Exception as exc:
                print(f"Could not remove this session's reverse mapping (device may be unplugged): {exc}", file=sys.stderr)
        forwarder.stop()
        if not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5)
            except asyncio.TimeoutError:
                task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        readiness.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await readiness
        for sig, previous in previous_handlers.items():
            signal.signal(sig, previous)


def port(value: str) -> int:
    number = int(value)
    if not 1 <= number <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return number


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adb", required=True)
    parser.add_argument("--rokid-serial", required=True)
    parser.add_argument("--activity", required=True, help="Resolved installed Android launcher component")
    parser.add_argument("--iphone-udid")
    parser.add_argument("--local-port", type=port, default=18080)
    parser.add_argument("--iphone-port", type=port, default=18080)
    parser.add_argument("--rokid-port", type=port, default=18080)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", args.rokid_serial):
        parser.error("Rokid must use an explicit USB serial, not a wireless ADB address")
    if not re.fullmatch(r"org\.opensqz\.openglass\.rokid(?:\.debug)?/[A-Za-z0-9_.$]+", args.activity):
        parser.error("activity must be the resolved OpenGlass Rokid launcher")
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    try:
        asyncio.run(run(args))
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"USB relay stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
