"""Runtime helpers for rtkbox modes."""

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


def log_friendly_info(mode, cfg, runner):
    if mode == "base-local":
        port = get_required(cfg, "base_local.port")
        runner.log(f"LAN correction source should be reachable at {detect_local_ip()}:{port}")
    if mode == "receiver-bridge":
        port = get_required(cfg, "receiver_bridge.port")
        runner.log(f"u-center TCP target should be reachable at {detect_local_ip()}:{port}")


def run_mode(mode, cfg, runner=None):
    runner = runner or Runner()
    app = cfg.get("app", {})
    reconnect_delay = app.get("reconnect_delay", 5)

    if mode == "nmea":
        run_nmea_loop(cfg, reconnect_delay, runner)
        return

    if shutil.which("str2str") is None:
        raise RuntimeError("str2str not found in PATH. Install RTKLIB first.")

    in_url, out_url = build_mode_streams(mode, cfg)
    log_friendly_info(mode, cfg, runner)
    cmd = ["str2str", "-in", in_url, "-out", out_url]
    if mode == "receiver-bridge":
        cmd.extend(["-b", "1"])
    run_str2str_loop(cmd, reconnect_delay, runner)
