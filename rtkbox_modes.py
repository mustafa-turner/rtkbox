"""Runtime helpers for rtkbox modes."""

from datetime import datetime
from pathlib import Path
import shutil
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


def build_recording_path(cfg):
    record_cfg = get_required(cfg, "record")
    output_dir = Path(str(record_cfg.get("output_dir", "recordings")))
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"ppp-{timestamp}.ubx"
    return output_dir / filename


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

    in_url, out_url = build_mode_streams(mode, cfg)
    log_friendly_info(mode, cfg, runner)
    cmd = ["str2str", "-in", in_url, "-out", out_url]
    if mode == "receiver-bridge":
        cmd.extend(["-b", "1"])
    run_str2str_loop(cmd, reconnect_delay, runner)
