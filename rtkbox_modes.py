"""Runtime helpers for rtkbox modes."""

from datetime import datetime
from pathlib import Path
import shutil
import struct
import subprocess
import threading
import time

import serial

from rtkbox_config import build_mode_streams, detect_local_ip, get_required, normalize_serial_port


class Runner:
    def __init__(self):
        self.stop_event = threading.Event()
        self._lock = threading.Lock()
        self.process = None
        self.runtime = {"recording": None}

    def log(self, message):
        print(message, flush=True)

    def set_process(self, process):
        with self._lock:
            self.process = process

    def clear_process(self, process):
        with self._lock:
            if self.process is process:
                self.process = None

    def request_stop(self):
        self.stop_event.set()
        with self._lock:
            if self.process is not None and self.process.poll() is None:
                self.process.terminate()

    def set_recording(self, info):
        with self._lock:
            self.runtime["recording"] = info

    def clear_recording(self):
        with self._lock:
            self.runtime["recording"] = None


def sleep_or_stop(seconds, runner):
    end = time.time() + seconds
    while time.time() < end:
        if runner.stop_event.wait(timeout=0.2):
            return False
    return True


def forward_process_output(process, runner):
    stream = process.stdout
    if stream is None:
        return

    for line in stream:
        text = line.rstrip()
        if text:
            runner.log(text)


def run_str2str_loop(cmd, reconnect_delay, runner):
    runner.log(f"Command: {' '.join(cmd)}")
    while not runner.stop_event.is_set():
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        runner.set_process(proc)
        output_thread = threading.Thread(
            target=forward_process_output,
            args=(proc, runner),
            daemon=True,
        )
        output_thread.start()
        try:
            while proc.poll() is None:
                if runner.stop_event.wait(timeout=0.5):
                    proc.terminate()
                    break
            code = proc.wait()
        finally:
            if proc.stdout is not None:
                proc.stdout.close()
            output_thread.join(timeout=1)
            runner.clear_process(proc)

        if runner.stop_event.is_set():
            runner.log("Stopped.")
            return

        runner.log(f"str2str exited with code {code}. Restarting in {reconnect_delay}s...")
        if not sleep_or_stop(reconnect_delay, runner):
            runner.log("Stopped.")
            return


def run_nmea_loop(cfg, reconnect_delay, runner):
    port = normalize_serial_port(get_required(cfg, "serial.port"))
    baud = get_required(cfg, "serial.baud")

    while not runner.stop_event.is_set():
        try:
            with serial.Serial(port=port, baudrate=baud, timeout=1) as ser:
                runner.log(f"Reading NMEA from {port} @ {baud}.")
                while not runner.stop_event.is_set():
                    line = ser.readline().decode("ascii", errors="replace").strip()
                    if line.startswith("$"):
                        runner.log(line)
        except Exception as exc:
            if runner.stop_event.is_set():
                runner.log("Stopped.")
                return
            runner.log(f"Serial error: {exc}. Reconnecting in {reconnect_delay}s...")
            if not sleep_or_stop(reconnect_delay, runner):
                runner.log("Stopped.")
                return

    runner.log("Stopped.")


def parse_rtcm_frame_end(data, start_index):
    if start_index + 3 > len(data) or data[start_index] != 0xD3:
        return None
    payload_len = ((data[start_index + 1] & 0x03) << 8) | data[start_index + 2]
    frame_end = start_index + 3 + payload_len + 3
    if frame_end > len(data):
        return None
    return frame_end


def rtcm_crc24q(data):
    crc = 0
    poly = 0x1864CFB
    for byte in data:
        crc ^= byte << 16
        for _ in range(8):
            crc <<= 1
            if crc & 0x1000000:
                crc ^= poly
    return crc & 0xFFFFFF


def is_valid_rtcm_frame(frame):
    if len(frame) < 6:
        return False
    body = frame[:-3]
    crc_expected = (frame[-3] << 16) | (frame[-2] << 8) | frame[-1]
    return rtcm_crc24q(body) == crc_expected


def parse_rtcm_message_id(frame):
    # RTCM message number is the first 12 bits of payload.
    # frame includes: 0xD3 + 2-byte length + payload + 3-byte CRC.
    if len(frame) < 6:
        return None
    payload = frame[3:-3]
    if len(payload) < 2:
        return None
    return (payload[0] << 4) | (payload[1] >> 4)


def log_serial_rtcm_messages_for_base_ntrip(cfg, runner, sample_seconds=8):
    port = normalize_serial_port(get_required(cfg, "serial.port"))
    baud = int(get_required(cfg, "serial.baud"))
    runner.log(
        f"Base NTRIP RTCM check: sampling receiver serial {port} @ {baud} for {sample_seconds}s..."
    )
    counts = {}
    bytes_total = 0
    valid_frames = 0
    invalid_frames = 0
    buffer = b""
    deadline = time.time() + max(1, int(sample_seconds))

    with serial.Serial(port=port, baudrate=baud, timeout=0.2) as ser:
        while time.time() < deadline and not runner.stop_event.is_set():
            chunk = ser.read(4096)
            if not chunk:
                continue
            bytes_total += len(chunk)
            buffer += chunk
            i = 0
            while i < len(buffer):
                if buffer[i] != 0xD3:
                    i += 1
                    continue
                frame_end = parse_rtcm_frame_end(buffer, i)
                if frame_end is None:
                    break
                frame = buffer[i:frame_end]
                if is_valid_rtcm_frame(frame):
                    valid_frames += 1
                    msg_id = parse_rtcm_message_id(frame)
                    if msg_id is not None:
                        counts[msg_id] = counts.get(msg_id, 0) + 1
                else:
                    invalid_frames += 1
                i = frame_end
            if i >= len(buffer):
                buffer = b""
            else:
                buffer = buffer[i:]

    if bytes_total == 0:
        runner.log("Base NTRIP RTCM check warning: no serial bytes seen.")
        return
    if valid_frames == 0:
        runner.log("Base NTRIP RTCM check warning: no valid RTCM3 frames parsed (CRC failed).")
        return
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    summary = ", ".join([f"{mid}:{cnt}" for mid, cnt in ordered[:20]])
    runner.log(
        "Base NTRIP RTCM check summary: "
        f"valid_frames={valid_frames}, invalid_frames={invalid_frames}"
    )
    runner.log(f"Base NTRIP RTCM IDs (top): {summary}")


def _set_usb_rate_for_message(ser, runner, msg_class, msg_id, label, desired_rate):
    rates = [0, 0, 0, 0, 0, 0]
    try:
        rates = poll_cfg_msg_rates(ser, msg_class, msg_id)
    except Exception as exc:
        runner.log(f"Base NTRIP warning: could not poll {label} ({exc}); forcing USB rate={desired_rate}.")
    if len(rates) < 6:
        rates = (rates + [0, 0, 0, 0, 0, 0])[:6]
    before = int(rates[3])
    if before == int(desired_rate):
        return False
    rates[3] = int(desired_rate)
    set_cfg_msg_rates(ser, msg_class, msg_id, rates)
    runner.log(f"Base NTRIP: set {label} USB rate {before} -> {desired_rate}.")
    return True


def ensure_base_rtcm_messages_enabled(cfg, runner):
    port = normalize_serial_port(get_required(cfg, "serial.port"))
    baud = int(get_required(cfg, "serial.baud"))

    # Compatibility profile:
    # - keep MSM4 + MSM7 enabled (receiver/firmware dependent)
    # - disable legacy 1004/1012
    # - keep ARP + GLONASS bias
    enable_targets = [
        (0xF5, 0x05, "RTCM 1005", 1),
        (0xF5, 0x4A, "RTCM 1074", 1),
        (0xF5, 0x54, "RTCM 1084", 1),
        (0xF5, 0x5E, "RTCM 1094", 1),
        (0xF5, 0x7C, "RTCM 1124", 1),
        (0xF5, 0x4D, "RTCM 1077", 1),
        (0xF5, 0x57, "RTCM 1087", 1),
        (0xF5, 0x61, "RTCM 1097", 1),
        (0xF5, 0x7F, "RTCM 1127", 1),
        (0xF5, 0xE6, "RTCM 1230", 1),
    ]
    disable_targets = [
        (0xF5, 0x04, "RTCM 1004", 0),
        (0xF5, 0x0C, "RTCM 1012", 0),
        (0xF5, 0x06, "RTCM 1006", 0),
    ]

    runner.log(f"Base NTRIP: enforcing compatibility RTCM profile on {port} @ {baud}.")
    changed = 0
    with serial.Serial(port=port, baudrate=baud, timeout=1) as ser:
        for msg_class, msg_id, label, rate in enable_targets:
            changed += int(_set_usb_rate_for_message(ser, runner, msg_class, msg_id, label, rate))
        for msg_class, msg_id, label, rate in disable_targets:
            changed += int(_set_usb_rate_for_message(ser, runner, msg_class, msg_id, label, rate))

        # Verification snapshot after apply.
        verify = [
            (0xF5, 0x05, "1005"),
            (0xF5, 0x04, "1004"),
            (0xF5, 0x0C, "1012"),
            (0xF5, 0x4A, "1074"),
            (0xF5, 0x54, "1084"),
            (0xF5, 0x5E, "1094"),
            (0xF5, 0x7C, "1124"),
            (0xF5, 0x4D, "1077"),
            (0xF5, 0x57, "1087"),
            (0xF5, 0x61, "1097"),
            (0xF5, 0x7F, "1127"),
            (0xF5, 0xE6, "1230"),
        ]
        entries = []
        for cls, mid, label in verify:
            try:
                rates = poll_cfg_msg_rates(ser, cls, mid)
                usb_rate = int((rates + [0, 0, 0, 0])[:4][3])
                entries.append(f"{label}:{usb_rate}")
            except Exception:
                entries.append(f"{label}:NA")
        runner.log("Base NTRIP: USB RTCM rates -> " + ", ".join(entries))
    if changed == 0:
        runner.log("Base NTRIP: RTCM profile already applied.")
    else:
        runner.log(f"Base NTRIP: applied {changed} RTCM profile updates.")
def build_recording_path(cfg):
    record_cfg = get_required(cfg, "record")
    output_dir = Path(str(record_cfg.get("output_dir", "recordings")))
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"ppp-{timestamp}.ubx"
    return output_dir / filename


def ubx_checksum(data):
    ck_a = 0
    ck_b = 0
    for byte in data:
        ck_a = (ck_a + byte) & 0xFF
        ck_b = (ck_b + ck_a) & 0xFF
    return bytes([ck_a, ck_b])


def ubx_frame(msg_class, msg_id, payload=b""):
    header = bytes([msg_class, msg_id]) + struct.pack("<H", len(payload))
    return b"\xB5\x62" + header + payload + ubx_checksum(header + payload)


def read_ubx_message(ser, timeout_s=1.5):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        start = ser.read(1)
        if start != b"\xB5":
            continue
        if ser.read(1) != b"\x62":
            continue
        header = ser.read(4)
        if len(header) != 4:
            continue
        msg_class, msg_id, payload_len = header[0], header[1], struct.unpack("<H", header[2:4])[0]
        payload = ser.read(payload_len)
        checksum = ser.read(2)
        if len(payload) != payload_len or len(checksum) != 2:
            continue
        if ubx_checksum(header + payload) != checksum:
            continue
        return msg_class, msg_id, payload
    return None


def send_ubx_and_wait_ack(ser, msg_class, msg_id, payload=b"", timeout_s=1.5):
    ser.reset_input_buffer()
    ser.write(ubx_frame(msg_class, msg_id, payload))
    ser.flush()
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        message = read_ubx_message(ser, timeout_s=max(0.1, deadline - time.time()))
        if message is None:
            break
        cls, mid, pl = message
        if cls == 0x05 and mid == 0x01 and len(pl) >= 2 and pl[0] == msg_class and pl[1] == msg_id:
            return
        if cls == 0x05 and mid == 0x00 and len(pl) >= 2 and pl[0] == msg_class and pl[1] == msg_id:
            raise RuntimeError(f"Receiver NAK for UBX-{msg_class:02X}-{msg_id:02X}")
    raise RuntimeError(f"No ACK for UBX-{msg_class:02X}-{msg_id:02X}")


def poll_cfg_msg_rates(ser, target_class, target_id):
    ser.reset_input_buffer()
    ser.write(ubx_frame(0x06, 0x01, bytes([target_class, target_id])))
    ser.flush()
    deadline = time.time() + 1.5
    while time.time() < deadline:
        message = read_ubx_message(ser, timeout_s=max(0.1, deadline - time.time()))
        if message is None:
            break
        msg_class, msg_id, payload = message
        if msg_class == 0x06 and msg_id == 0x01 and len(payload) >= 8 and payload[0] == target_class and payload[1] == target_id:
            return list(payload[2:8])
    raise RuntimeError(f"No UBX-CFG-MSG poll response for {target_class:02X}-{target_id:02X}")


def set_cfg_msg_rates(ser, target_class, target_id, rates):
    payload = bytes([target_class, target_id] + [int(v) & 0xFF for v in rates[:6]])
    send_ubx_and_wait_ack(ser, 0x06, 0x01, payload)


def ensure_ppp_messages_enabled(ser, runner):
    targets = [
        (0x02, 0x15, "UBX-RXM-RAWX"),
        (0x02, 0x13, "UBX-RXM-SFRBX"),
    ]

    for msg_class, msg_id, label in targets:
        rates = [0, 0, 0, 0, 0, 0]
        try:
            rates = poll_cfg_msg_rates(ser, msg_class, msg_id)
        except Exception as exc:
            runner.log(f"Could not poll {label} rates ({exc}). Applying USB rate=1.")

        if len(rates) < 6:
            rates = (rates + [0, 0, 0, 0, 0, 0])[:6]

        usb_rate_before = rates[3]
        if usb_rate_before == 1:
            runner.log(f"{label} already enabled on USB.")
            continue

        rates[3] = 1
        set_cfg_msg_rates(ser, msg_class, msg_id, rates)
        runner.log(f"Enabled {label} on USB (rate {usb_rate_before} -> 1).")


def run_record_loop(cfg, reconnect_delay, runner):
    record_cfg = get_required(cfg, "record")
    port = normalize_serial_port(record_cfg.get("serial_port", get_required(cfg, "receiver_bridge.serial_port")))
    baud = int(record_cfg.get("baud", get_required(cfg, "receiver_bridge.baud")))

    while not runner.stop_event.is_set():
        output_path = build_recording_path(cfg)
        start_time = time.time()
        runner.set_recording(
            {
                "path": str(output_path),
                "name": output_path.name,
                "started_at": start_time,
                "bytes_written": 0,
            }
        )
        try:
            with serial.Serial(port=port, baudrate=baud, timeout=1) as ser, output_path.open("wb") as fh:
                ensure_ppp_messages_enabled(ser, runner)
                runner.log(f"Recording raw UBX from {port} @ {baud} to {output_path}")
                while not runner.stop_event.is_set():
                    chunk = ser.read(4096)
                    if not chunk:
                        continue
                    fh.write(chunk)
                    fh.flush()
                    with runner._lock:
                        if runner.runtime["recording"] is not None:
                            runner.runtime["recording"]["bytes_written"] += len(chunk)
        except Exception as exc:
            if runner.stop_event.is_set():
                break
            runner.log(f"Record error: {exc}. Reconnecting in {reconnect_delay}s...")
            runner.clear_recording()
            if not sleep_or_stop(reconnect_delay, runner):
                break
            continue

        break

    runner.clear_recording()
    runner.log("Stopped.")


def log_friendly_info(mode, cfg, runner):
    if mode == "base-local":
        port = get_required(cfg, "base_local.port")
        runner.log(f"LAN correction source should be reachable at {detect_local_ip()}:{port}")
    if mode == "receiver-bridge":
        port = get_required(cfg, "receiver_bridge.port")
        runner.log(f"u-center TCP target should be reachable at {detect_local_ip()}:{port}")
    if mode == "record":
        record_cfg = get_required(cfg, "record")
        runner.log(f"PPP record mode will save raw UBX files under {record_cfg.get('output_dir', 'recordings')}")


def run_mode(mode, cfg, runner=None):
    runner = runner or Runner()
    app = cfg.get("app", {})
    reconnect_delay = app.get("reconnect_delay", 5)

    if mode == "nmea":
        run_nmea_loop(cfg, reconnect_delay, runner)
        return

    if mode == "record":
        run_record_loop(cfg, reconnect_delay, runner)
        return

    if shutil.which("str2str") is None:
        raise RuntimeError("str2str not found in PATH. Install RTKLIB first.")

    if mode == "base-ntrip":
        runner.log("Base NTRIP: pass-through forwarding enabled (receiver stream -> caster).")
        try:
            ensure_base_rtcm_messages_enabled(cfg, runner)
        except Exception as exc:
            runner.log(f"Base NTRIP warning: failed to apply RTCM message profile ({exc}).")
        try:
            log_serial_rtcm_messages_for_base_ntrip(cfg, runner, sample_seconds=8)
        except Exception as exc:
            runner.log(f"Base NTRIP RTCM check warning: {exc}")

    in_url, out_url = build_mode_streams(mode, cfg)
    log_friendly_info(mode, cfg, runner)
    cmd = ["str2str", "-in", in_url, "-out", out_url]
    if mode == "receiver-bridge":
        cmd.extend(["-b", "1"])
    run_str2str_loop(cmd, reconnect_delay, runner)
