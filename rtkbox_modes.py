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

BASE_NTRIP_RTCM_MESSAGES = "1005(10),1077(1),1087(1),1097(1),1127(1),1230(10)"


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


def poll_tmode3_payload(ser):
    ser.reset_input_buffer()
    ser.write(ubx_frame(0x06, 0x71, b""))
    ser.flush()
    deadline = time.time() + 1.5
    while time.time() < deadline:
        message = read_ubx_message(ser, timeout_s=max(0.1, deadline - time.time()))
        if message is None:
            break
        msg_class, msg_id, payload = message
        if msg_class == 0x06 and msg_id == 0x71:
            return payload
    raise RuntimeError("No response to UBX-CFG-TMODE3 poll")


def parse_tmode3_payload(payload):
    if len(payload) < 32:
        raise RuntimeError("Invalid UBX-CFG-TMODE3 response payload length")

    flags = struct.unpack("<H", payload[2:4])[0]
    mode_code = flags & 0xFF
    mode = {0: "disabled", 1: "survey", 2: "fixed"}.get(mode_code, f"unknown({mode_code})")

    x_cm, y_cm, z_cm = struct.unpack("<iii", payload[4:16])
    x_hp, y_hp, z_hp = struct.unpack("<bbb", payload[16:19])

    return {
        "mode": mode,
        "ecef_x_m": (x_cm / 100.0) + (x_hp / 10000.0),
        "ecef_y_m": (y_cm / 100.0) + (y_hp / 10000.0),
        "ecef_z_m": (z_cm / 100.0) + (z_hp / 10000.0),
    }


def get_base_ntrip_station_position(cfg, runner):
    port = normalize_serial_port(get_required(cfg, "serial.port"))
    baud = int(get_required(cfg, "serial.baud"))

    with serial.Serial(port=port, baudrate=baud, timeout=0.2) as ser:
        tmode = parse_tmode3_payload(poll_tmode3_payload(ser))

    if tmode["mode"] != "fixed":
        runner.log(
            "Base NTRIP: receiver is not in fixed TMODE3, so no station coordinates were attached "
            "to the generated RTCM stream."
        )
        return []

    coords = [tmode["ecef_x_m"], tmode["ecef_y_m"], tmode["ecef_z_m"]]
    runner.log(
        "Base NTRIP: using receiver TMODE3 fixed coordinates "
        f"({coords[0]:.4f}, {coords[1]:.4f}, {coords[2]:.4f}) for RTCM station position."
    )
    return ["-px", str(coords[0]), str(coords[1]), str(coords[2])]


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
        runner.log("Base NTRIP: str2str conversion enabled (ubx -> rtcm3).")

    in_url, out_url = build_mode_streams(mode, cfg)
    log_friendly_info(mode, cfg, runner)
    cmd = ["str2str", "-in", in_url, "-out", out_url]
    if mode == "base-ntrip":
        cmd.extend(["-msg", BASE_NTRIP_RTCM_MESSAGES])
        try:
            cmd.extend(get_base_ntrip_station_position(cfg, runner))
        except Exception as exc:
            runner.log(f"Base NTRIP warning: could not read TMODE3 fixed coordinates ({exc}).")
    if mode == "receiver-bridge":
        cmd.extend(["-b", "1"])
    run_str2str_loop(cmd, reconnect_delay, runner)
