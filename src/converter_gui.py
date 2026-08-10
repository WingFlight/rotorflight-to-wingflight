#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Rotorflight to Wingflight
=========================
Small Windows GUI for converting an MSP-speaking Rotorflight board to the
latest matching Wingflight firmware build.
"""

from __future__ import annotations

import json
import io
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from collections import namedtuple
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import ttk, scrolledtext, messagebox
except ImportError:
    print("Error: tkinter is required but not found.")
    sys.exit(1)

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    print("Error: pyserial is required. Install with: pip install pyserial")
    sys.exit(1)


def _get_resource_dir():
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent


RESOURCE_DIR = _get_resource_dir()
DRIVERS_DIR = RESOURCE_DIR / "drivers"
TOOLS_DIR = RESOURCE_DIR / "tools"

APP_REPO_URL = "https://github.com/WingFlight/rotorflight-to-wingflight"
FIRMWARE_RELEASES_URL = "https://api.github.com/repos/WingFlight/wingflight-firmware/releases"
GITHUB_API_URL = "https://api.github.com"
DFU_UTIL_WINDOWS_ZIP_URL = "https://dfu-util.sourceforge.net/releases/dfu-util-0.9-win64.zip"

STM32_DFU = (0x0483, 0xDF11)
GD32_DFU = (0x28E9, 0x0189)

KNOWN_SERIAL_CHIPS = {
    (0x0483, 0x5740): "STM32 Virtual COM Port",
    (0x10C4, 0xEA60): "CP210x USB-UART",
    (0x1A86, 0x7523): "CH340 USB-UART",
    (0x0403, 0x6001): "FTDI FT232",
}

DRIVER_PACKAGES = {
    STM32_DFU: {
        "label": "STM32 DFU (signed)",
        "inf": DRIVERS_DIR / "stm32" / "STM32Bootloader.inf",
    },
    GD32_DFU: {
        "label": "GD32/AT32 DFU (unsigned, best-effort)",
        "inf": DRIVERS_DIR / "gd32" / "GD32Bootloader.inf",
    },
}

MSP_API_VERSION = 1
MSP_FC_VARIANT = 2
MSP_FC_VERSION = 3
MSP_BOARD_INFO = 4
MSP_SET_REBOOT = 68
REBOOT_BOOTLOADER_ROM = 1
REBOOT_BOOTLOADER_FLASH = 4
REBOOT_BAUD = 115200
REBOOT_WAIT_SECONDS = 12.0
REBOOT_POLL_SECONDS = 1.0
IDENTIFY_TIMEOUT_SECONDS = 0.8
HAS_FLASH_BOOTLOADER_BIT = 3

CUSTOM_DEFAULTS_POINTER_ADDRESS = 0x08002800
HEX_CHUNK_SIZE = 16384
MAX_PADDED_DFU_IMAGE_BYTES = 4 * 1024 * 1024
FIRMWARE_ASSET_RE = re.compile(
    r"^wingflight_"
    r"(?P<version>\d+\.\d+\.\d+(?:(?:-[A-Za-z]\w*)|(?:-\d+(?:\.\d+)*))?)"
    r"_(?P<target>[A-Za-z]\w*)[.]hex$"
)
TARGET_CONFIG_RE = re.compile(r"^([^-]{1,4})-(.*)[.]config$")
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


DeviceRow = namedtuple(
    "DeviceRow",
    ["id", "label", "vidpid_str", "dfu_key", "is_serial", "driver_ok", "board_info"],
)


@dataclass
class BoardInfo:
    port: str
    fc_variant: str = ""
    fc_version: str = ""
    board_identifier: str = ""
    board_version: int = 0
    board_type: int = 0
    target_capabilities: int = 0
    target_name: str = ""
    board_name: str = ""
    board_design: str = ""
    manufacturer_id: str = ""

    @property
    def normalized_board_name(self):
        return self.board_name.replace(".", "_")

    @property
    def unified_target(self):
        if self.manufacturer_id and self.normalized_board_name:
            return f"{self.manufacturer_id}-{self.normalized_board_name}"
        return ""

    @property
    def has_flash_bootloader(self):
        return bool(self.target_capabilities & (1 << HAS_FLASH_BOOTLOADER_BIT))

    @property
    def display_name(self):
        parts = [self.unified_target or self.target_name or self.board_identifier]
        if self.fc_variant or self.fc_version:
            parts.append(f"{self.fc_variant} {self.fc_version}".strip())
        return " / ".join([p for p in parts if p])


@dataclass
class ReleaseAsset:
    version: str
    target: str
    file: str
    tag: str
    url: str
    release_url: str
    prerelease: bool
    published_at: str
    notes: str

    @property
    def is_official(self):
        return not self.prerelease and "-" not in self.version


@dataclass
class TargetConfig:
    target: str
    manufacturer: str
    board: str
    source_repo: str
    branch: str
    path: str
    download_url: str
    supported: bool
    config_text: str = ""
    bare_board: str = ""
    commit_hash: str = "unknown"
    commit_date: str = ""


@dataclass
class FirmwarePlan:
    board: BoardInfo
    asset: ReleaseAsset
    firmware_hex: str
    target_config: TargetConfig | None
    used_snapshot_fallback: bool


class HttpError(RuntimeError):
    pass


def is_admin():
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _run_hidden(cmd, timeout):
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=CREATE_NO_WINDOW,
    )


def _hwid_fragment(vid, pid):
    return f"VID_{vid:04X}&PID_{pid:04X}"


def build_msp_v1(code, payload=b""):
    body = bytes([len(payload), code]) + payload
    checksum = 0
    for b in body:
        checksum ^= b
    return b"$M<" + body + bytes([checksum])


def read_msp_v1_response(ser, expected_code, timeout=IDENTIFY_TIMEOUT_SECONDS):
    deadline = time.time() + timeout
    buffer = bytearray()
    while time.time() < deadline:
        chunk = ser.read(64)
        if chunk:
            buffer.extend(chunk)
        while len(buffer) >= 6:
            start = buffer.find(b"$M")
            if start < 0:
                del buffer[:]
                break
            if start:
                del buffer[:start]
            if len(buffer) < 6:
                break
            direction = buffer[2]
            size = buffer[3]
            code = buffer[4]
            frame_len = 6 + size
            if len(buffer) < frame_len:
                break
            payload = bytes(buffer[5 : 5 + size])
            checksum = buffer[5 + size]
            calc = size ^ code
            for b in payload:
                calc ^= b
            del buffer[:frame_len]
            if calc != checksum:
                continue
            if direction == ord("!") and code == expected_code:
                raise RuntimeError(f"MSP command {expected_code} returned an error")
            if direction == ord(">") and code == expected_code:
                return payload
    raise TimeoutError(f"Timed out waiting for MSP command {expected_code}")


def msp_request_open(ser, code, payload=b"", timeout=IDENTIFY_TIMEOUT_SECONDS):
    ser.reset_input_buffer()
    ser.write(build_msp_v1(code, payload))
    ser.flush()
    return read_msp_v1_response(ser, code, timeout)


def msp_request(port_name, code, payload=b"", timeout=IDENTIFY_TIMEOUT_SECONDS):
    with serial.Serial(port_name, REBOOT_BAUD, timeout=0.08, write_timeout=1.0) as ser:
        return msp_request_open(ser, code, payload, timeout)


def probe_msp(port_name):
    try:
        msp_request(port_name, MSP_API_VERSION)
        return True
    except Exception:
        return False


class PayloadReader:
    def __init__(self, payload):
        self.payload = payload
        self.index = 0

    def remaining(self):
        return len(self.payload) - self.index

    def read_u8(self):
        if self.remaining() < 1:
            return 0
        value = self.payload[self.index]
        self.index += 1
        return value

    def read_u16(self):
        if self.remaining() < 2:
            self.index = len(self.payload)
            return 0
        value = self.payload[self.index] | (self.payload[self.index + 1] << 8)
        self.index += 2
        return value

    def read_u32(self):
        if self.remaining() < 4:
            self.index = len(self.payload)
            return 0
        value = struct.unpack_from("<I", self.payload, self.index)[0]
        self.index += 4
        return value

    def read_string(self, length):
        length = min(length, self.remaining())
        raw = self.payload[self.index : self.index + length]
        self.index += length
        return raw.decode("ascii", errors="replace").strip("\x00")


def query_board_info(port_name):
    info = BoardInfo(port=port_name)

    with serial.Serial(port_name, REBOOT_BAUD, timeout=0.08, write_timeout=1.0) as ser:
        try:
            info.fc_variant = msp_request_open(ser, MSP_FC_VARIANT).decode("ascii", errors="replace")
        except Exception:
            pass

        try:
            version = msp_request_open(ser, MSP_FC_VERSION)
            if len(version) >= 3:
                info.fc_version = f"{version[0]}.{version[1]}.{version[2]}"
        except Exception:
            pass

        payload = msp_request_open(ser, MSP_BOARD_INFO, timeout=1.2)
    data = PayloadReader(payload)
    info.board_identifier = data.read_string(4)
    info.board_version = data.read_u16()
    info.board_type = data.read_u8()
    info.target_capabilities = data.read_u8()

    if data.remaining() > 0:
        info.target_name = data.read_string(data.read_u8())
    if data.remaining() > 0:
        info.board_name = data.read_string(data.read_u8())
    if data.remaining() > 0:
        info.board_design = data.read_string(data.read_u8())
    if data.remaining() > 0:
        info.manufacturer_id = data.read_string(data.read_u8())

    return info


def reboot_to_bootloader(port_name, use_flash_bootloader=False):
    mode = REBOOT_BOOTLOADER_FLASH if use_flash_bootloader else REBOOT_BOOTLOADER_ROM
    packet = build_msp_v1(MSP_SET_REBOOT, bytes([mode]))
    with serial.Serial(port_name, REBOOT_BAUD, timeout=2.0, write_timeout=1.0) as ser:
        ser.write(packet)
        ser.flush()


def scan_dfu_devices():
    if sys.platform != "win32":
        return []

    fragments = [_hwid_fragment(vid, pid) for vid, pid in DRIVER_PACKAGES]
    regex = "|".join(fragments)
    ps_command = (
        "$ErrorActionPreference='SilentlyContinue'; "
        f"Get-PnpDevice -PresentOnly | Where-Object {{ $_.InstanceId -match '{regex}' }} "
        "| Select-Object InstanceId, FriendlyName, Status | ConvertTo-Json -Compress"
    )
    try:
        result = _run_hidden(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps_command],
            timeout=10,
        )
    except Exception:
        return []

    text = (result.stdout or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except Exception:
        return []
    if isinstance(data, dict):
        data = [data]

    found = []
    for entry in data:
        instance_id = str(entry.get("InstanceId", "")).upper()
        friendly = entry.get("FriendlyName") or "USB Device"
        driver_ok = str(entry.get("Status", "")).strip().upper() == "OK"
        for vid, pid in DRIVER_PACKAGES:
            if _hwid_fragment(vid, pid) in instance_id:
                found.append(((vid, pid), instance_id, friendly, driver_ok))
                break
    return found


def describe_port(port):
    if port.vid is not None and port.pid is not None:
        vid_pid = (port.vid, port.pid)
        if vid_pid in DRIVER_PACKAGES:
            return DRIVER_PACKAGES[vid_pid]["label"]
        if vid_pid in KNOWN_SERIAL_CHIPS:
            return KNOWN_SERIAL_CHIPS[vid_pid]
    return port.description or "Unknown device"


def scan_candidate_devices():
    rows = []
    dfu_keys_seen = set()

    for port in list_ports.comports():
        vid_pid = (port.vid, port.pid) if port.vid is not None else None
        dfu_key = vid_pid if vid_pid in DRIVER_PACKAGES else None
        if dfu_key:
            dfu_keys_seen.add(dfu_key)
        vidpid_str = f"{port.vid:04X}:{port.pid:04X}" if port.vid is not None else "-"
        rows.append(
            DeviceRow(
                id=port.device,
                label=describe_port(port),
                vidpid_str=vidpid_str,
                dfu_key=dfu_key,
                is_serial=True,
                driver_ok=True,
                board_info=None,
            )
        )

    for dfu_key, instance_id, friendly, driver_ok in scan_dfu_devices():
        if dfu_key in dfu_keys_seen:
            continue
        vid, pid = dfu_key
        rows.append(
            DeviceRow(
                id=instance_id,
                label=f"{friendly} - {DRIVER_PACKAGES[dfu_key]['label']}",
                vidpid_str=f"{vid:04X}:{pid:04X}",
                dfu_key=dfu_key,
                is_serial=False,
                driver_ok=driver_ok,
                board_info=None,
            )
        )

    return rows


def scan_identified_devices():
    candidates = scan_candidate_devices()
    identified = [row for row in candidates if row.dfu_key]
    for row in candidates:
        if row.dfu_key:
            continue
        try:
            board = query_board_info(row.id)
            if board.board_identifier or board.board_name or board.target_name:
                identified.append(row._replace(board_info=board, label=board.display_name or row.label))
        except Exception:
            if probe_msp(row.id):
                identified.append(row)
    return identified


def install_driver(inf_path):
    if not inf_path.is_file():
        return False, f"Driver file not found: {inf_path}"
    try:
        result = _run_hidden(["pnputil.exe", "/add-driver", str(inf_path), "/install"], timeout=30)
    except Exception as e:
        return False, f"Failed to run pnputil: {e}"

    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode == 0:
        return True, output.strip() or "Driver installed."
    return False, output.strip() or f"pnputil exited with code {result.returncode}"


def http_json(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "rotorflight-to-wingflight"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise HttpError(f"HTTP {e.code}: {url}") from e


def http_text(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "rotorflight-to-wingflight"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return res.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raise HttpError(f"HTTP {e.code}: {url}") from e


def firmware_artifact_url(tag, filename):
    quoted_tag = urllib.parse.quote(tag, safe="")
    quoted_file = urllib.parse.quote(filename, safe="")
    return f"https://cdn.jsdelivr.net/gh/WingFlight/wingflight-artifacts@master/firmware/{quoted_tag}/{quoted_file}"


def parse_version_key(version):
    main, _, suffix = version.partition("-")
    parts = tuple(int(p) for p in main.split("."))
    suffix_key = (0, "") if not suffix else (-1, suffix)
    return (*parts, *suffix_key)


def load_release_assets():
    releases = http_json(FIRMWARE_RELEASES_URL)
    assets = []
    for release in releases:
        for asset in release.get("assets", []):
            name = asset.get("name", "")
            match = FIRMWARE_ASSET_RE.match(name)
            if not match:
                continue
            assets.append(
                ReleaseAsset(
                    version=match.group("version"),
                    target=match.group("target"),
                    file=name,
                    tag=release.get("tag_name", ""),
                    url=firmware_artifact_url(release.get("tag_name", ""), name),
                    release_url=release.get("html_url", ""),
                    prerelease=bool(release.get("prerelease")),
                    published_at=release.get("published_at", ""),
                    notes=release.get("body") or "",
                )
            )
    return assets


def newest_asset_for_target(assets, target):
    matches = [a for a in assets if a.target.upper() == target.upper()]
    if not matches:
        return None, False
    official = [a for a in matches if a.is_official]
    if official:
        return sorted(official, key=lambda a: parse_version_key(a.version), reverse=True)[0], False
    return sorted(matches, key=lambda a: (a.published_at, parse_version_key(a.version)), reverse=True)[0], True


def github_contents(project, branch, path):
    encoded_path = "/".join(urllib.parse.quote(part, safe="") for part in path.split("/"))
    encoded_project = urllib.parse.quote(project, safe="/")
    url = f"{GITHUB_API_URL}/repos/{encoded_project}/contents/{encoded_path}?ref={urllib.parse.quote(branch, safe='')}"
    return http_json(url)


def github_commit_info(project, branch, path):
    query = urllib.parse.urlencode({"sha": branch, "path": path})
    encoded_project = urllib.parse.quote(project, safe="/")
    url = f"{GITHUB_API_URL}/repos/{encoded_project}/commits?{query}"
    try:
        commits = http_json(url)
        commit = commits[0]
        return commit.get("sha", "unknown")[:8], commit["commit"]["author"]["date"]
    except Exception:
        return "unknown", datetime.now(timezone.utc).isoformat()


def find_target_config(unified_target):
    if not unified_target:
        return None
    filename = f"{unified_target}.config"
    candidates = [
        ("WingFlight/wingflight-targets", "master", f"configs/{filename}", True),
        ("rotorflight/rotorflight-targets", "rotorflight", f"legacy/{filename}", False),
    ]
    for project, branch, path, supported in candidates:
        try:
            entry = github_contents(project, branch, path)
        except Exception:
            continue
        name = entry.get("name", filename)
        match = TARGET_CONFIG_RE.match(name)
        if not match:
            continue
        return TargetConfig(
            target=unified_target,
            manufacturer=match.group(1),
            board=match.group(2),
            source_repo=project,
            branch=branch,
            path=path,
            download_url=entry["download_url"],
            supported=supported,
        )
    return None


def clean_unified_config_file(input_text):
    ignore_patterns = [
        r"^feature [-]?AIRMODE",
        r"^feature [-]?ANTI",
        r"^feature [-]?DISPLAY",
        r"^feature [-]?DYNAMIC",
        r"^feature [-]?ESC_SENSOR",
        r"^feature [-]?GPS",
        r"^feature [-]?LED_STRIP",
        r"^feature [-]?MOTOR_STOP",
        r"^feature [-]?OSD",
        r"^feature [-]?RSSI",
        r"^feature [-]?RX_PARALLEL",
        r"^feature [-]?RX_SERIAL",
        r"^feature [-]?RX_SPI",
        r"^feature [-]?SOFTSERIAL",
        r"^feature [-]?TELEMETRY",
        r"^resource PWM",
        r"^resource MOTOR [5-8]",
        r"^resource OSD",
        r"^serial [0-9]",
        r"^set serialrx",
        r"^set max7456",
    ]
    compiled = [re.compile(p, re.I) for p in ignore_patterns]
    output = []
    fork = "BF"
    for index, raw in enumerate(re.split(r"[\r\n]+", input_text)):
        line = raw
        if index == 0 and re.match(r"^# [A-Za-z]*flight", line):
            if re.match(r"^# Rotorflight", line):
                fork = "RF"
        else:
            line = re.sub(r"#.*$", "", line)
            line = re.sub(r"[ \t]+$", "", line)
            line = re.sub(r"[ \t]+", " ", line)
            if not line.strip():
                continue
            if fork != "RF" and any(p.match(line) for p in compiled):
                continue
        output.append(line)
    return "\n".join(output) + "\n"


def grab_build_name_from_config(config):
    match = re.search(r".+/ (STM32[^ ]*)", config)
    return match.group(1) if match else None


def inject_default_design(target_config, board_design="BTFL"):
    if re.search(r"board_design [A-Za-z0-9_+-]+\n", target_config):
        return target_config
    return re.sub(
        r"(board_name [A-Za-z0-9_+-]+\n)",
        rf"\1board_design {board_design}\n",
        target_config,
        count=1,
    )


def inject_target_info(target_config, config_name, target_name, manufacturer_id, commit_hash, commit_date):
    target_config = re.sub(r"# config: manufacturer_id: .*\n", "", target_config)
    return (
        "## Wingflight Custom Defaults\n"
        f"# config: {config_name}\n"
        f"# board: {target_name}\n"
        f"# make: {manufacturer_id}\n"
        f"# hash: {commit_hash}\n"
        f"# date: {commit_date}\n"
        "##\n"
        f"{target_config}"
    )


def prepare_target_config(target_config):
    raw = http_text(target_config.download_url)
    cleaned = clean_unified_config_file(raw)
    bare = grab_build_name_from_config(cleaned)
    if not bare:
        raise RuntimeError(f"Could not identify base firmware target from {target_config.path}")
    commit_hash, commit_date = github_commit_info(
        target_config.source_repo,
        target_config.branch,
        target_config.path,
    )
    cleaned = inject_default_design(cleaned, "BTFL")
    cleaned = inject_target_info(
        cleaned,
        Path(target_config.path).name,
        target_config.target,
        target_config.manufacturer,
        commit_hash,
        commit_date,
    )
    target_config.config_text = cleaned
    target_config.bare_board = bare
    target_config.commit_hash = commit_hash
    target_config.commit_date = commit_date
    return target_config


def make_firmware_plan(board_info, log=lambda _msg: None):
    log("Loading Wingflight release list...")
    assets = load_release_assets()

    target_config = None
    lookup_targets = []
    if board_info.unified_target:
        log(f"Looking for Wingflight target config {board_info.unified_target}.config...")
        target_config = find_target_config(board_info.unified_target)
        if target_config:
            target_config = prepare_target_config(target_config)
            lookup_targets.append(target_config.bare_board)
            log(f"Matched {board_info.unified_target} to base target {target_config.bare_board}.")

    for candidate in (board_info.target_name, board_info.board_identifier, board_info.normalized_board_name):
        if candidate and candidate not in lookup_targets:
            lookup_targets.append(candidate)

    for target in lookup_targets:
        asset, fallback = newest_asset_for_target(assets, target)
        if not asset:
            continue
        kind = "snapshot/prerelease" if fallback else "official release"
        log(f"Selected Wingflight {kind}: {asset.file}")
        log(f"Downloading firmware from {asset.url}")
        firmware_hex = http_text(asset.url, timeout=60)
        if target_config and target.upper() == target_config.bare_board.upper():
            firmware_hex = insert_config_into_hex_text(firmware_hex, target_config.config_text)
            log("Injected Wingflight target defaults into firmware HEX.")
        return FirmwarePlan(
            board=board_info,
            asset=asset,
            firmware_hex=firmware_hex,
            target_config=target_config,
            used_snapshot_fallback=fallback,
        )

    tried = ", ".join(lookup_targets) or "(none)"
    raise RuntimeError(f"No Wingflight firmware asset found for detected board. Tried: {tried}")


def parse_intel_hex(text):
    segments = []
    upper = 0
    min_addr = None
    max_addr = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if not line.startswith(":"):
            raise ValueError("Invalid Intel HEX line")
        try:
            length = int(line[1:3], 16)
            offset = int(line[3:7], 16)
            record_type = int(line[7:9], 16)
            data = bytes.fromhex(line[9 : 9 + length * 2])
            checksum = int(line[9 + length * 2 : 11 + length * 2], 16)
        except Exception as e:
            raise ValueError(f"Invalid Intel HEX line: {line}") from e

        check = length + (offset >> 8) + (offset & 0xFF) + record_type + sum(data) + checksum
        if check & 0xFF:
            raise ValueError(f"Intel HEX checksum failed at address 0x{offset:04X}")

        if record_type == 0x00:
            address = upper + offset
            segments.append({"address": address, "data": bytearray(data)})
            min_addr = address if min_addr is None else min(min_addr, address)
            max_addr = address + length if max_addr is None else max(max_addr, address + length)
        elif record_type == 0x01:
            break
        elif record_type == 0x04:
            upper = int.from_bytes(data, "big") << 16

    segments.sort(key=lambda s: s["address"])
    return {"segments": segments, "min_addr": min_addr, "max_addr": max_addr}


def merge_contiguous_segments(segments):
    if not segments:
        return []
    merged = []
    for seg in sorted(segments, key=lambda s: s["address"]):
        if not merged:
            merged.append({"address": seg["address"], "data": bytearray(seg["data"])})
            continue
        prev = merged[-1]
        prev_end = prev["address"] + len(prev["data"])
        if seg["address"] == prev_end:
            prev["data"].extend(seg["data"])
        else:
            merged.append({"address": seg["address"], "data": bytearray(seg["data"])})
    return merged


def build_padded_flash_image(parsed):
    """Build one binary image from sparse Intel HEX records.

    dfu-util/DfuSe can erase a whole STM32 flash sector while downloading a
    small record. Flashing sparse records as separate processes can therefore
    erase data written by a previous record in the same sector. The configurator
    avoids this by erasing first and then writing all records in one DFU
    session; for dfu-util, a padded binary span gives the same practical safety.
    """
    segments = merge_contiguous_segments(parsed["segments"])
    if not segments:
        raise RuntimeError("Firmware HEX contained no data records.")

    start = segments[0]["address"]
    end = max(seg["address"] + len(seg["data"]) for seg in segments)
    size = end - start
    if size <= 0 or size > MAX_PADDED_DFU_IMAGE_BYTES:
        raise RuntimeError(
            f"Refusing to create a padded DFU image of {size} bytes. "
            "Firmware address range looks unsafe."
        )

    image = bytearray([0xFF]) * size
    for seg in segments:
        offset = seg["address"] - start
        image[offset : offset + len(seg["data"])] = seg["data"]

    return start, bytes(image), len(segments)


def find_bytes_at(parsed, address, count):
    result = bytearray()
    cursor = address
    for seg in parsed["segments"]:
        seg_start = seg["address"]
        seg_end = seg_start + len(seg["data"])
        if cursor < seg_start:
            break
        if seg_start <= cursor < seg_end:
            take = min(count - len(result), seg_end - cursor)
            result.extend(seg["data"][cursor - seg_start : cursor - seg_start + take])
            cursor += take
            if len(result) == count:
                return bytes(result)
    return None


def insert_config_into_hex_text(hex_text, config_text):
    parsed = parse_intel_hex(hex_text)
    pointer = find_bytes_at(parsed, CUSTOM_DEFAULTS_POINTER_ADDRESS, 8)
    if not pointer:
        return hex_text
    start, end = struct.unpack("<II", pointer)
    if end <= start:
        return hex_text

    payload = config_text.encode("ascii", errors="replace") + b"\0"
    if len(payload) >= (end - start):
        raise RuntimeError(
            f"Custom defaults area too small ({end - start} bytes), {len(payload)} bytes needed."
        )

    for seg in parsed["segments"]:
        seg_end = seg["address"] + len(seg["data"])
        if seg["address"] < start < seg_end or start <= seg["address"] < end:
            raise RuntimeError("Configuration area in firmware is not free.")

    parsed["segments"].append({"address": start, "data": bytearray(payload)})
    return write_intel_hex(merge_contiguous_segments(parsed["segments"]))


def write_intel_hex(segments):
    lines = []
    current_upper = None

    def emit_record(address, record_type, data):
        length = len(data)
        hi = (address >> 8) & 0xFF
        lo = address & 0xFF
        total = length + hi + lo + record_type + sum(data)
        checksum = ((~total + 1) & 0xFF)
        lines.append(f":{length:02X}{address:04X}{record_type:02X}{data.hex().upper()}{checksum:02X}")

    for seg in segments:
        data = bytes(seg["data"])
        address = seg["address"]
        while data:
            upper = address >> 16
            if upper != current_upper:
                current_upper = upper
                emit_record(0, 0x04, upper.to_bytes(2, "big"))
            chunk = data[:32]
            emit_record(address & 0xFFFF, 0x00, chunk)
            address += len(chunk)
            data = data[len(chunk) :]

    emit_record(0, 0x01, b"")
    return "\n".join(lines) + "\n"


def get_dfu_util_path():
    for candidate in dfu_util_search_paths():
        if candidate.is_file():
            return str(candidate)

    found = shutil.which("dfu-util.exe") or shutil.which("dfu-util")
    if found:
        return found

    raise FileNotFoundError("dfu-util not found.")


def dfu_util_search_paths():
    paths = [TOOLS_DIR / "dfu-util.exe"]
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        paths.append(Path(local_app_data) / "WingFlight" / "rotorflight-to-wingflight" / "tools" / "dfu-util.exe")
    return paths


def writable_tools_dir():
    if getattr(sys, "frozen", False):
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / "WingFlight" / "rotorflight-to-wingflight" / "tools"
    return TOOLS_DIR


def ensure_dfu_util(log=lambda _msg: None):
    try:
        return get_dfu_util_path()
    except FileNotFoundError:
        pass

    if sys.platform != "win32":
        raise FileNotFoundError("dfu-util not found. Install dfu-util or add it to PATH.")

    destination_dir = writable_tools_dir()
    destination = destination_dir / "dfu-util.exe"
    log("dfu-util was not found; downloading official Windows binary...")
    log(f"Source: {DFU_UTIL_WINDOWS_ZIP_URL}")

    try:
        destination_dir.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(
            urllib.request.Request(
                DFU_UTIL_WINDOWS_ZIP_URL,
                headers={"User-Agent": "rotorflight-to-wingflight"},
            ),
            timeout=60,
        ) as res:
            zip_data = res.read()

        with zipfile.ZipFile(io.BytesIO(zip_data)) as archive:
            member = next(
                (name for name in archive.namelist() if name.endswith("/dfu-util-static.exe")),
                None,
            )
            if not member:
                raise RuntimeError("dfu-util-static.exe was not found in the downloaded archive.")
            destination.write_bytes(archive.read(member))

        log(f"Installed dfu-util to {destination}")
        return str(destination)
    except Exception as e:
        raise RuntimeError(
            "Could not download dfu-util automatically. "
            "Put dfu-util.exe in src\\tools or add it to PATH."
        ) from e


def get_dfu_util_path_or_download(log=lambda _msg: None):
    try:
        return get_dfu_util_path()
    except FileNotFoundError:
        return ensure_dfu_util(log)


def flash_with_dfu_util(firmware_hex, dfu_key, log=lambda _msg: None):
    dfu_util = get_dfu_util_path_or_download(log)
    parsed = parse_intel_hex(firmware_hex)
    start_address, image, segment_count = build_padded_flash_image(parsed)

    vid, pid = dfu_key
    with tempfile.TemporaryDirectory(prefix="wf-flash-") as tmp:
        image_path = Path(tmp) / f"firmware-0x{start_address:08X}.bin"
        image_path.write_bytes(image)
        cmd = [
            dfu_util,
            "-d",
            f"{vid:04x}:{pid:04x}",
            "-a",
            "0",
            "-s",
            f"0x{start_address:08X}:leave",
            "-D",
            str(image_path),
        ]
        log(
            f"Flashing one padded image at 0x{start_address:08X} "
            f"({len(image)} bytes from {segment_count} HEX region(s))..."
        )
        result = _run_hidden(cmd, timeout=300)
        output = ((result.stdout or "") + (result.stderr or "")).strip()
        if result.returncode != 0:
            raise RuntimeError(output or f"dfu-util exited with code {result.returncode}")
        if output:
            for line in output.splitlines()[-8:]:
                log(f"dfu-util: {line}")


class ConverterApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Rotorflight to Wingflight")
        self.root.geometry("860x650")
        self.root.resizable(False, False)

        self.devices = []
        self.busy = False
        self.setup_ui()
        self.refresh_devices()

    def setup_ui(self):
        header_bg = "#1f1f1f"
        header_fg = "#f2f2f2"

        header = tk.Frame(self.root, bg=header_bg, height=84)
        header.pack(fill=tk.X)
        header.pack_propagate(False)

        text_frame = tk.Frame(header, bg=header_bg)
        text_frame.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=12, pady=10)

        tk.Label(
            text_frame,
            text="Rotorflight to Wingflight",
            font=("Arial", 16, "bold"),
            bg=header_bg,
            fg=header_fg,
        ).pack(anchor=tk.W)
        tk.Label(
            text_frame,
            text="Detect board, fetch matching Wingflight firmware, fix DFU driver, flash",
            font=("Arial", 10),
            bg=header_bg,
            fg=header_fg,
        ).pack(anchor=tk.W, pady=(2, 0))

        logo_path = RESOURCE_DIR / "logo.png"
        if logo_path.is_file():
            try:
                self.logo_image = tk.PhotoImage(file=str(logo_path))
                logo_frame = tk.Frame(header, bg=header_bg, width=160, height=60)
                logo_frame.pack(side=tk.RIGHT, padx=12, pady=10)
                logo_frame.pack_propagate(False)
                tk.Label(logo_frame, image=self.logo_image, bg=header_bg).pack(anchor=tk.E)
            except Exception:
                self.logo_image = None

        if not is_admin():
            warn = tk.Frame(self.root, bg="#5a3d00")
            warn.pack(fill=tk.X)
            tk.Label(
                warn,
                text="Not running as Administrator - DFU driver installation will fail. Re-launch as admin.",
                bg="#5a3d00",
                fg="#ffd479",
                font=("Arial", 9, "bold"),
                pady=4,
            ).pack()

        body = ttk.Frame(self.root, padding=10)
        body.pack(fill=tk.BOTH, expand=True)

        columns = ("port", "description", "vidpid", "status")
        self.tree = ttk.Treeview(body, columns=columns, show="headings", height=8, selectmode="browse")
        for col, text, width in (
            ("port", "Port", 110),
            ("description", "Board / Device", 330),
            ("vidpid", "VID:PID", 90),
            ("status", "Status", 280),
        ):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor=tk.W)
        self.tree.pack(fill=tk.X)
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self.update_button_states())

        primary_frame = ttk.Frame(self.root, padding=(10, 12, 10, 4))
        primary_frame.pack(fill=tk.X)
        style = ttk.Style()
        style.configure("Flash.TButton", font=("Arial", 13, "bold"), padding=10)
        self.flash_button = ttk.Button(
            primary_frame,
            text="Convert to Wingflight",
            style="Flash.TButton",
            command=self.on_flash_click,
            width=28,
        )
        self.flash_button.pack()

        button_frame = ttk.Frame(self.root, padding=(10, 0, 10, 10))
        button_frame.pack(fill=tk.X)
        self.refresh_button = ttk.Button(button_frame, text="Refresh", command=self.refresh_devices)
        self.refresh_button.pack(side=tk.LEFT)
        ttk.Button(button_frame, text="Open GitHub", command=lambda: webbrowser.open(APP_REPO_URL)).pack(side=tk.RIGHT)

        log_frame = ttk.Frame(self.root, padding=(10, 0, 10, 10))
        log_frame.pack(fill=tk.BOTH, expand=True)
        self.log_text = scrolledtext.ScrolledText(log_frame, height=17, state=tk.DISABLED, wrap=tk.WORD)
        self.log_text.pack(fill=tk.BOTH, expand=True)

    def log(self, message):
        def append():
            self.log_text.configure(state=tk.NORMAL)
            self.log_text.insert(tk.END, message + "\n")
            self.log_text.see(tk.END)
            self.log_text.configure(state=tk.DISABLED)

        self.root.after(0, append)

    def refresh_devices(self):
        previous = self.tree.selection()[0] if self.tree.selection() else None
        self.set_busy(True)
        self.log("Scanning USB and MSP devices...")

        def worker():
            devices = scan_identified_devices()

            def apply():
                self._apply_scan_result(devices, previous)
                self.set_busy(False)

            self.root.after(0, apply)

        threading.Thread(target=worker, daemon=True).start()

    @staticmethod
    def _status_for(row):
        if row.dfu_key:
            return "DFU mode - driver ready" if row.driver_ok else "DFU mode - driver missing"
        if row.board_info:
            if row.board_info.fc_variant == "RTFL":
                return "Rotorflight board detected"
            if row.board_info.fc_variant == "WGFL":
                return "Wingflight board detected"
            return f"MSP board detected ({row.board_info.fc_variant or 'unknown fork'})"
        return "MSP device detected"

    def _apply_scan_result(self, devices, previous):
        self.devices = devices
        self.tree.delete(*self.tree.get_children())
        for row in devices:
            self.tree.insert(
                "",
                tk.END,
                iid=row.id,
                values=(
                    row.id if row.is_serial else "(DFU)",
                    row.label,
                    row.vidpid_str,
                    self._status_for(row),
                ),
            )

        if previous and self.tree.exists(previous):
            self.tree.selection_set(previous)
        elif len(devices) == 1:
            self.tree.selection_set(devices[0].id)

        self.log(f"Scan complete: {len(devices)} candidate device(s) found.")

    def update_button_states(self):
        self.flash_button.configure(state=tk.DISABLED if self.busy else tk.NORMAL)

    def set_busy(self, busy):
        self.busy = busy
        self.refresh_button.configure(state=tk.DISABLED if busy else tk.NORMAL)
        self.update_button_states()

    @staticmethod
    def _pick_unambiguous(devices):
        serial_devices = [d for d in devices if d.is_serial and d.board_info]
        if len(serial_devices) == 1:
            return serial_devices[0]
        if len(devices) == 1:
            return devices[0]
        return None

    def on_flash_click(self):
        if not messagebox.askokcancel(
            "Confirm Flash",
            "This will overwrite the selected flight controller firmware.\n\n"
            "Make sure you can force the board into BOOT/DFU mode with its boot button or boot pads before continuing.",
        ):
            return
        selection = self.tree.selection()
        selected_id = selection[0] if selection else None
        self.set_busy(True)
        threading.Thread(target=self._flash_pipeline_worker, args=(selected_id,), daemon=True).start()

    def _flash_pipeline_worker(self, selected_id):
        try:
            self.log("Refreshing device identity...")
            devices = scan_identified_devices()
            self.root.after(0, lambda: self._apply_scan_result(devices, selected_id))

            row = next((d for d in devices if d.id == selected_id), None) if selected_id else None
            if not row:
                row = self._pick_unambiguous(devices)

            if not row:
                self.log("Select exactly one MSP board, then click Convert to Wingflight again.")
                return
            if row.dfu_key:
                self.log("This first bootstrap needs the board connected in normal MSP mode so it can detect the target.")
                return

            board = row.board_info or query_board_info(row.id)
            self.log(f"Detected board: {board.display_name}")
            if board.fc_variant and board.fc_variant != "RTFL":
                self.log(f"Note: firmware identifier is {board.fc_variant}, not RTFL. Continuing because target metadata is available.")

            plan = make_firmware_plan(board, self.log)
            self.log("Rebooting board to DFU/bootloader mode...")
            reboot_to_bootloader(row.id, board.has_flash_bootloader)
            dfu_key = self._wait_for_dfu()
            if not dfu_key:
                self.log("No DFU device appeared. Put the board in bootloader mode manually and try again.")
                return

            self._ensure_dfu_driver(dfu_key)
            flash_with_dfu_util(plan.firmware_hex, dfu_key, self.log)

            final_note = "Snapshot fallback was used." if plan.used_snapshot_fallback else "Official release was used."
            self.log(f"Done. Flashed {plan.asset.file}. {final_note}")
            self.root.after(
                0,
                lambda: messagebox.showinfo("Wingflight Flash Complete", f"Flashed {plan.asset.file}."),
            )
        except Exception as e:
            self.log(f"Failed: {e}")
            self.root.after(0, lambda: messagebox.showerror("Conversion Failed", str(e)))
        finally:
            self.root.after(0, self.refresh_devices)

    def _wait_for_dfu(self):
        deadline = time.time() + REBOOT_WAIT_SECONDS
        while time.time() < deadline:
            time.sleep(REBOOT_POLL_SECONDS)
            devices = scan_dfu_devices()
            if devices:
                dfu_key, _instance_id, friendly, driver_ok = devices[0]
                state = "driver OK" if driver_ok else "driver missing"
                self.log(f"DFU device detected: {friendly} ({state}).")
                return dfu_key
        return None

    def _ensure_dfu_driver(self, dfu_key):
        devices = scan_dfu_devices()
        needs_driver = True
        for key, _instance_id, _friendly, driver_ok in devices:
            if key == dfu_key and driver_ok:
                needs_driver = False
                break
        if not needs_driver:
            self.log("DFU driver is already installed.")
            return

        package = DRIVER_PACKAGES[dfu_key]
        self.log(f"Installing {package['label']} driver...")
        ok, message = install_driver(package["inf"])
        if not ok:
            raise RuntimeError(f"DFU driver install failed: {message}")
        self.log(message or "DFU driver installed.")
        time.sleep(1.0)


def main():
    root = tk.Tk()
    icon_path = RESOURCE_DIR / "icon.ico"
    if sys.platform == "win32" and icon_path.is_file():
        try:
            root.iconbitmap(str(icon_path))
        except Exception:
            pass
    ConverterApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
