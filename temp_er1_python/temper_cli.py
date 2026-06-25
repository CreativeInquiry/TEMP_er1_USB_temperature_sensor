#!/usr/bin/env python3

"""
Read a TEMPer/TEMP_er1 USB HID temperature sensor.
Developed at the STUDIO for Creative Inquiry, CMU
By Golan Levin, June 2026

Typical OSC streaming command, run from this repository root:
    python3 temp_er1_python/temper_cli.py --osc --quiet

Useful standalone commands:
    python3 temp_er1_python/temper_cli.py --list
    python3 temp_er1_python/temper_cli.py --once
    python3 temp_er1_python/temper_cli.py --osc --log

Dependencies are loaded from temp_er1_python/vendor when that directory exists.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


VENDOR_DIR = Path(__file__).resolve().parent / "vendor"
if VENDOR_DIR.exists():
    # Prefer project-local vendored wheels over global Python packages.
    sys.path.insert(0, str(VENDOR_DIR))

DEFAULT_OSC_HOST = "127.0.0.1"
DEFAULT_OSC_PORT = 8367
DEFAULT_LOG_INTERVAL_SECONDS = 10.0
LOG_DIR = Path(__file__).resolve().parent / "logs"

SUPPORTED_DEVICES = {
    (0x0C45, 0x7401): "TEMPer1V1.4 / TEMP_er1",
    (0x1130, 0x660C): "Legacy TEMPer",
}

CMD_TEMPERATURE = bytes([0x01, 0x80, 0x33, 0x01, 0x00, 0x00, 0x00, 0x00])
CMD_INIT_1 = bytes([0x01, 0x82, 0x77, 0x01, 0x00, 0x00, 0x00, 0x00])
CMD_INIT_2 = bytes([0x01, 0x86, 0xFF, 0x01, 0x00, 0x00, 0x00, 0x00])

# Command sequences are from the TEMPer/TEMP_er1 libusb driver lineage.
LEGACY_CMD_0 = bytes([0, 0, 0, 0, 0, 0, 0, 0])
LEGACY_CMD_1 = bytes([10, 11, 12, 13, 0, 0, 2, 0])
LEGACY_CMD_2 = bytes([10, 11, 12, 13, 0, 0, 1, 0])
LEGACY_CMD_3 = bytes([0x52, 0, 0, 0, 0, 0, 0, 0])
LEGACY_CMD_4 = bytes([0x54, 0, 0, 0, 0, 0, 0, 0])


class TemperError(RuntimeError):
    """Expected runtime failure for sensor, dependency, logging, or OSC setup."""

    pass


class TemperatureLogger:
    """Append readings to local-date TSV files with periodic fsyncs."""

    def __init__(self, interval_seconds: float, log_dir: Path = LOG_DIR) -> None:
        """Create a logger that records rows every interval_seconds."""
        self.interval_seconds = interval_seconds
        self.save_interval_seconds = max(60.0, interval_seconds)
        self.log_dir = log_dir
        self.next_log_time = 0.0
        self.next_save_time = 0.0
        self.current_date: dt.date | None = None
        self.file = None

    def maybe_log(self, value: float, unit: str, *, now_monotonic: float | None = None) -> None:
        """Write a row only when the configured logging interval has elapsed."""
        now_monotonic = time.monotonic() if now_monotonic is None else now_monotonic
        if now_monotonic < self.next_log_time:
            return

        self.write(value, unit, now_monotonic=now_monotonic)
        self.next_log_time = now_monotonic + self.interval_seconds

    def write(self, value: float, unit: str, *, now_monotonic: float) -> None:
        """Write one TSV row to the file for the current local date."""
        now = dt.datetime.now()
        self.ensure_file_for_date(now.date())
        assert self.file is not None
        self.file.write(f"{now:%H:%M:%S}\t{value:.2f} {unit}\n")

        if now_monotonic >= self.next_save_time:
            self.save(now_monotonic=now_monotonic)

    def ensure_file_for_date(self, date: dt.date) -> None:
        """Open the daily log file for date, rolling over at local midnight."""
        if self.current_date == date and self.file is not None:
            return

        # Closing first guarantees the previous day's file is flushed to disk.
        self.close()
        self.current_date = date
        self.log_dir.mkdir(parents=True, exist_ok=True)
        path = self.log_dir / f"temp_er1_{date:%Y-%m-%d}.tsv"
        self.file = path.open("a", encoding="utf-8")
        self.next_save_time = 0.0

    def save(self, *, now_monotonic: float | None = None) -> None:
        """Flush buffered rows and ask the OS to persist them to storage."""
        if self.file is None:
            return
        self.file.flush()
        os.fsync(self.file.fileno())
        if now_monotonic is not None:
            self.next_save_time = now_monotonic + self.save_interval_seconds

    def close(self) -> None:
        """Flush and close the current daily log file if one is open."""
        if self.file is None:
            return
        try:
            self.save()
        finally:
            self.file.close()
            self.file = None


def import_osc_udp_client():
    """Import python-osc lazily so non-OSC commands do not need it loaded."""
    try:
        from pythonosc import udp_client  # type: ignore
    except ImportError as exc:
        raise TemperError(
            "Missing Python OSC package. Install it with: cd temp_er1_python && "
            "python3 -m pip install --target vendor -r requirements.txt"
        ) from exc
    return udp_client


@dataclass(frozen=True)
class HidDeviceInfo:
    """Small normalized wrapper for a hidapi enumeration result."""

    path: bytes | str
    vendor_id: int
    product_id: int
    product_string: str
    manufacturer_string: str
    interface_number: int | None

    @property
    def label(self) -> str:
        """Human-readable device label shown in CLI output and OSC metadata."""
        name = SUPPORTED_DEVICES.get((self.vendor_id, self.product_id), "Unknown")
        iface = "?" if self.interface_number is None else str(self.interface_number)
        product = self.product_string or name
        return f"{product} ({self.vendor_id:04x}:{self.product_id:04x}, interface {iface})"

    @property
    def path_text(self) -> str:
        """Return the hidapi path as displayable text."""
        if isinstance(self.path, bytes):
            return self.path.decode("utf-8", errors="replace")
        return self.path


def import_hid():
    """Import hidapi lazily and raise an actionable project-local install hint."""
    try:
        import hid  # type: ignore
    except ImportError as exc:
        raise TemperError(
            "Missing Python HID package. Install it with: cd temp_er1_python && "
            "python3 -m pip install --target vendor -r requirements.txt"
        ) from exc
    return hid


def enumerate_devices() -> list[HidDeviceInfo]:
    """Return supported TEMPer/TEMP_er1 HID interfaces sorted by preference."""
    hid = import_hid()
    devices: list[HidDeviceInfo] = []
    for item in hid.enumerate():
        key = (item.get("vendor_id"), item.get("product_id"))
        if key not in SUPPORTED_DEVICES:
            continue
        devices.append(
            HidDeviceInfo(
                path=item["path"],
                vendor_id=item["vendor_id"],
                product_id=item["product_id"],
                product_string=item.get("product_string") or "",
                manufacturer_string=item.get("manufacturer_string") or "",
                interface_number=item.get("interface_number"),
            )
        )
    return sorted(devices, key=device_sort_key)


def device_sort_key(info: HidDeviceInfo) -> tuple[int, int]:
    """Prefer the modern device's vendor-specific sensor interface."""
    interface = 99 if info.interface_number is None else info.interface_number
    preferred_interface = 0 if interface == 1 else 1
    preferred_model = 0 if (info.vendor_id, info.product_id) == (0x0C45, 0x7401) else 1
    return (preferred_model, preferred_interface)


class TemperDevice:
    """Context manager around one open TEMPer/TEMP_er1 HID interface."""

    def __init__(self, info: HidDeviceInfo, *, read_timeout_ms: int = 1000) -> None:
        """Prepare a hidapi device wrapper; the device opens in __enter__."""
        self.info = info
        self.read_timeout_ms = read_timeout_ms
        self._hid = import_hid()
        self._device = self._hid.device()
        self._is_open = False

    def __enter__(self) -> "TemperDevice":
        """Open and initialize the HID device."""
        if self._is_open:
            return self
        self._device.open_path(self.info.path)
        self._is_open = True
        self._device.set_nonblocking(False)
        self._initialize()
        return self

    def __exit__(self, *_exc: object) -> None:
        """Close the HID device when leaving a with block."""
        self.close()

    def close(self) -> None:
        """Close the HID handle, ignoring close-time hidapi errors."""
        try:
            if self._is_open:
                self._device.close()
        except Exception:
            pass
        finally:
            self._is_open = False

    def _initialize(self) -> None:
        """Send the startup command sequence expected by the selected device."""
        if (self.info.vendor_id, self.info.product_id) == (0x1130, 0x660C):
            self._write(LEGACY_CMD_1)
            self._write(LEGACY_CMD_3)
            self._write(LEGACY_CMD_2)
            self._get_feature_report(0, 256)
            return

        # The 0c45:7401 device needs three setup commands with report reads.
        self._write(CMD_TEMPERATURE)
        self._read_report(required=False)
        self._write(CMD_INIT_1)
        self._read_report(required=False)
        self._write(CMD_INIT_2)
        self._read_report(required=False)
        self._read_report(required=False)

    def read_celsius(self) -> float:
        """Read one temperature sample in Celsius."""
        if (self.info.vendor_id, self.info.product_id) == (0x1130, 0x660C):
            return self._read_legacy_celsius()

        self._write(CMD_TEMPERATURE)
        report = self._read_report(required=True)
        return report_to_celsius(report[2], report[3])

    def _read_legacy_celsius(self) -> float:
        """Read one sample from the older 1130:660c protocol variant."""
        self._write(LEGACY_CMD_1)
        self._write(LEGACY_CMD_4)
        for _ in range(7):
            self._write(LEGACY_CMD_0)
        self._write(LEGACY_CMD_2)
        report = self._get_feature_report(0, 256)
        if len(report) < 2:
            raise TemperError("Legacy device returned a short feature report")
        return report_to_celsius(report[0], report[1])

    def _write(self, payload: bytes) -> None:
        """Write one HID report, trying both macOS report-ID conventions."""
        errors: list[str] = []
        # Some hidapi backends require a leading report ID byte; some do not.
        for report in (b"\x00" + payload, payload):
            try:
                written = self._device.write(report)
                if written > 0:
                    return
                errors.append(f"write returned {written}")
            except OSError as exc:
                errors.append(str(exc))
        raise TemperError("Could not write HID command: " + "; ".join(errors))

    def _read_report(self, *, required: bool) -> list[int]:
        """Read one 8-byte interrupt report from the sensor interface."""
        report = self._device.read(8, self.read_timeout_ms)
        if report:
            if len(report) < 8:
                raise TemperError(f"Short HID report: {report}")
            return report
        if required:
            raise TemperError("Timed out waiting for HID temperature report")
        return []

    def _get_feature_report(self, report_id: int, size: int) -> list[int]:
        """Read a HID feature report used by the legacy protocol."""
        try:
            return self._device.get_feature_report(report_id, size)
        except OSError as exc:
            raise TemperError(f"Could not read feature report: {exc}") from exc


def report_to_celsius(high: int, low: int) -> float:
    """Convert the sensor's 16-bit fixed-point report value to Celsius."""
    raw = ((high & 0xFF) << 8) | (low & 0xFF)
    if raw & 0x8000:
        raw -= 0x10000
    return raw * (125.0 / 32000.0)


def celsius_to_fahrenheit(celsius: float) -> float:
    """Convert Celsius to Fahrenheit."""
    return (celsius * 9.0 / 5.0) + 32.0


def open_first_working_device(devices: Iterable[HidDeviceInfo]) -> TemperDevice:
    """Open the first enumerated device/interface that initializes cleanly."""
    errors: list[str] = []
    for info in devices:
        try:
            device = TemperDevice(info)
            device.__enter__()
            return device
        except Exception as exc:
            errors.append(f"{info.label}: {exc}")
    if not errors:
        raise TemperError("No supported TEMPer/TEMP_er1 sensor found")
    raise TemperError("Could not open a supported sensor. " + " | ".join(errors))


def output_temperature(celsius: float, args: argparse.Namespace) -> tuple[float, str]:
    """Return the calibrated temperature value and selected output unit."""
    celsius = (celsius * args.scale) + args.offset
    if args.fahrenheit:
        return celsius_to_fahrenheit(celsius), "F"
    return celsius, "C"


def format_reading(celsius: float, args: argparse.Namespace) -> str:
    """Format a temperature reading for stdout in plain, JSON, or text form."""
    value, unit = output_temperature(celsius, args)
    if args.plain:
        return f"{value:.2f}"

    now = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    if args.json:
        return json.dumps({"timestamp": now, "temperature": round(value, 4), "unit": unit})
    return f"{now} {value:.2f} {unit}"


def parse_osc_endpoint(endpoint: str) -> tuple[str, int]:
    """Parse a host:port OSC destination string."""
    if ":" not in endpoint:
        raise TemperError(f"--osc must be in host:port form, for example {DEFAULT_OSC_HOST}:{DEFAULT_OSC_PORT}")
    host, port_text = endpoint.rsplit(":", 1)
    if not host:
        raise TemperError("--osc host must not be empty")
    try:
        port = int(port_text)
    except ValueError as exc:
        raise TemperError("--osc port must be an integer") from exc
    validate_osc_port(port)
    return host, port


def validate_osc_port(port: int) -> None:
    """Validate that port is a usable UDP port number."""
    if port < 1 or port > 65535:
        raise TemperError("OSC port must be between 1 and 65535")


def normalize_osc_prefix(prefix: str) -> str:
    """Normalize an OSC prefix to a leading-slash, no-trailing-slash form."""
    prefix = prefix.strip()
    if not prefix:
        raise TemperError("--osc-prefix must not be empty")
    if not prefix.startswith("/"):
        prefix = "/" + prefix
    return prefix.rstrip("/")


def create_osc_client(args: argparse.Namespace):
    """Create an OSC UDP client when OSC output was requested."""
    if args.osc:
        args.osc_host, args.osc_port = parse_osc_endpoint(args.osc)
    if args.osc_port is None:
        return None

    validate_osc_port(args.osc_port)
    args.osc_prefix = normalize_osc_prefix(args.osc_prefix)
    udp_client = import_osc_udp_client()
    return udp_client.SimpleUDPClient(args.osc_host, args.osc_port)


def create_logger(args: argparse.Namespace) -> TemperatureLogger | None:
    """Create a logger when --log/-log was requested."""
    if args.log is None:
        return None
    if args.log <= 0 or not math.isfinite(args.log):
        raise TemperError("Log interval must be a positive number of seconds")
    return TemperatureLogger(args.log)


def send_osc_reading(client, prefix: str, device: TemperDevice, celsius: float) -> None:
    """Send one reading and current device metadata over OSC."""
    fahrenheit = celsius_to_fahrenheit(celsius)
    client.send_message(f"{prefix}/temperature", [celsius, fahrenheit])
    client.send_message(f"{prefix}/temperature/celsius", celsius)
    client.send_message(f"{prefix}/temperature/fahrenheit", fahrenheit)
    client.send_message(f"{prefix}/device", device.info.label)
    client.send_message(f"{prefix}/device/path", device.info.path_text)


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(description="Read a TEMPer/TEMP_er1 USB HID temperature sensor.")
    parser.add_argument("--list", action="store_true", help="list supported HID devices and exit")
    parser.add_argument("--once", action="store_true", help="print one reading and exit")
    parser.add_argument("--count", type=int, default=0, help="number of readings before exit; 0 means forever")
    parser.add_argument("--interval", type=float, default=1.0, help="seconds between readings")
    parser.add_argument("--plain", action="store_true", help="print only the numeric temperature")
    parser.add_argument("--json", action="store_true", help="print one JSON object per reading")
    parser.add_argument("-F", "--fahrenheit", action="store_true", help="report Fahrenheit instead of Celsius")
    parser.add_argument("--scale", type=float, default=1.0, help="calibration scale applied to Celsius")
    parser.add_argument("--offset", type=float, default=0.0, help="calibration offset applied to Celsius")
    parser.add_argument(
        "--osc",
        nargs="?",
        const=f"{DEFAULT_OSC_HOST}:{DEFAULT_OSC_PORT}",
        help=f"send OSC to host:port; defaults to {DEFAULT_OSC_HOST}:{DEFAULT_OSC_PORT} when no value is given",
    )
    parser.add_argument("--osc-host", default=DEFAULT_OSC_HOST, help="OSC destination host when using --osc-port")
    parser.add_argument("--osc-port", type=int, help="OSC destination port")
    parser.add_argument("--osc-prefix", default="/temp_er1", help="OSC address prefix")
    parser.add_argument(
        "-log",
        "--log",
        nargs="?",
        const=DEFAULT_LOG_INTERVAL_SECONDS,
        type=float,
        help=f"log readings to temp_er1_python/logs every N seconds; default N is {DEFAULT_LOG_INTERVAL_SECONDS:g}",
    )
    parser.add_argument("--quiet", action="store_true", help="do not print readings to stdout")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the command-line utility."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.interval <= 0 or not math.isfinite(args.interval):
        parser.error("--interval must be a positive number")
    if args.count < 0:
        parser.error("--count must not be negative")
    if args.once:
        args.count = 1

    logger: TemperatureLogger | None = None
    try:
        osc_client = create_osc_client(args)
        logger = create_logger(args)
        devices = enumerate_devices()
        if args.list:
            if not devices:
                print("No supported TEMPer/TEMP_er1 HID devices found")
                return 1
            for info in devices:
                print(f"{info.label} path={info.path_text}")
            return 0

        with open_first_working_device(devices) as device:
            readings = 0
            while args.count == 0 or readings < args.count:
                celsius = device.read_celsius()
                if osc_client is not None:
                    send_osc_reading(osc_client, args.osc_prefix, device, celsius)
                if logger is not None:
                    value, unit = output_temperature(celsius, args)
                    logger.maybe_log(value, unit)
                if not args.quiet:
                    print(format_reading(celsius, args), flush=True)
                readings += 1
                if args.count == 0 or readings < args.count:
                    # Keep the HID read rate controlled by --interval.
                    time.sleep(args.interval)
            return 0
    except KeyboardInterrupt:
        return 130
    except TemperError as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        return 2
    finally:
        if logger is not None:
            logger.close()


if __name__ == "__main__":
    raise SystemExit(main())
