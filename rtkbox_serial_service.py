"""Centralized serial receiver service for UBX poll/apply operations."""

from contextlib import contextmanager
from collections import deque
import socket
import struct
import threading
import time

import serial


class ReceiverSerialService:
    def __init__(self):
        self._lock = threading.Lock()
        self._owner = None
        self._stream_lock = threading.Lock()
        self._direct_mode_lock = threading.Lock()
        self._stream_stop_event = threading.Event()
        self._stream_thread = None
        self._stream_cfg = None
        self._stream_clients = []
        self._stream_client_count = 0
        self._runtime_state = {
            "available": False,
            "message": "Receiver stream not started.",
            "lat_deg": None,
            "lon_deg": None,
            "h_acc_m": None,
            "tmode_mode": None,
            "svin_duration_s": None,
            "svin_accuracy_m": None,
            "svin_valid": None,
            "svin_active": None,
            "updated_at": 0.0,
        }
        self._nmea_lines = deque(maxlen=120)

    def receiver_serial_target(self, cfg):
        receiver_bridge = cfg.get("receiver_bridge", {})
        serial_cfg = cfg.get("serial", {})
        port = str(receiver_bridge.get("serial_port") or serial_cfg.get("port") or "/dev/ttyACM0")
        baud = int(receiver_bridge.get("baud") or serial_cfg.get("baud") or 115200)
        if not port.startswith("/dev/"):
            port = f"/dev/{port}"
        return port, baud

    def record_serial_target(self, cfg):
        record_cfg = cfg.get("record", {})
        receiver_bridge = cfg.get("receiver_bridge", {})
        serial_cfg = cfg.get("serial", {})
        port = str(
            record_cfg.get("serial_port")
            or receiver_bridge.get("serial_port")
            or serial_cfg.get("port")
            or "/dev/ttyACM0"
        )
        baud = int(
            record_cfg.get("baud")
            or receiver_bridge.get("baud")
            or serial_cfg.get("baud")
            or 115200
        )
        if not port.startswith("/dev/"):
            port = f"/dev/{port}"
        return port, baud

    def stream_endpoint(self, cfg):
        app_cfg = cfg.get("app", {})
        host = str(app_cfg.get("serial_service_host", "127.0.0.1"))
        port = int(app_cfg.get("serial_service_port", 15555))
        return host, port

    def ensure_stream(self, cfg):
        if not self._direct_mode_lock.acquire(blocking=False):
            return
        self._direct_mode_lock.release()

        serial_port, serial_baud = self.receiver_serial_target(cfg)
        stream_host, stream_port = self.stream_endpoint(cfg)
        target = (serial_port, serial_baud, stream_host, stream_port)

        with self._stream_lock:
            running = self._stream_thread is not None and self._stream_thread.is_alive()
            if running and self._stream_cfg == target:
                return
            if running:
                self._stream_stop_event.set()
                thread = self._stream_thread
                self._stream_lock.release()
                try:
                    thread.join(timeout=2)
                finally:
                    self._stream_lock.acquire()
            self._stream_stop_event = threading.Event()
            self._stream_cfg = target
            self._stream_thread = threading.Thread(
                target=self._stream_loop,
                args=(serial_port, serial_baud, stream_host, stream_port),
                daemon=True,
            )
            self._stream_thread.start()

    def stop_stream(self):
        with self._stream_lock:
            self._stream_stop_event.set()
            thread = self._stream_thread
            self._stream_thread = None
            self._stream_cfg = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)

    def _stream_running(self):
        with self._stream_lock:
            return self._stream_thread is not None and self._stream_thread.is_alive()

    def _stream_has_clients(self):
        with self._stream_lock:
            return self._stream_client_count > 0

    @contextmanager
    def direct_serial_session(self, cfg, owner="direct", timeout_s=1.5, ser_timeout=0.2):
        self._direct_mode_lock.acquire()
        restart_stream = False
        try:
            if self._stream_running():
                if self._stream_has_clients():
                    raise RuntimeError("Receiver stream is busy. Stop active mode before changing receiver config.")
                self.stop_stream()
                restart_stream = True
            with self.open_receiver_serial(cfg, timeout_s=timeout_s, owner=owner, ser_timeout=ser_timeout) as ser:
                yield ser
        finally:
            if restart_stream:
                self.ensure_stream(cfg)
            self._direct_mode_lock.release()

    def get_nmea_snapshot(self):
        with self._stream_lock:
            return list(self._nmea_lines)

    def get_runtime_snapshot(self, cfg):
        self.ensure_stream(cfg)
        serial_port, serial_baud = self.receiver_serial_target(cfg)
        with self._stream_lock:
            runtime = dict(self._runtime_state)
        runtime["serial_port"] = serial_port
        runtime["baud"] = serial_baud
        age_s = time.time() - float(runtime.get("updated_at") or 0.0)
        runtime["stale"] = (not runtime.get("available")) or age_s > 3.0
        runtime["age_s"] = age_s if runtime.get("updated_at") else None
        return runtime

    def read_runtime(self, cfg):
        return self.get_runtime_snapshot(cfg)

    def read_tmode3(self, cfg):
        port, baud = self.receiver_serial_target(cfg)
        with self.direct_serial_session(cfg, owner="tmode3_read", timeout_s=1.5, ser_timeout=0.2) as ser:
            payload = self._poll_tmode3_payload(ser)
        status = self._parse_tmode3_payload(payload)
        status["serial_port"] = port
        status["baud"] = baud
        return status

    def apply_tmode3(
        self,
        cfg,
        mode,
        survey_min_dur=600,
        survey_acc_limit=5000,
        fixed_ecef_x_m=None,
        fixed_ecef_y_m=None,
        fixed_ecef_z_m=None,
    ):
        mode = str(mode).strip().lower()
        if mode not in {"survey", "fixed"}:
            raise ValueError("mode must be 'survey' or 'fixed'")
        if mode == "fixed":
            provided = [fixed_ecef_x_m, fixed_ecef_y_m, fixed_ecef_z_m]
            if any(value is not None for value in provided) and not all(value is not None for value in provided):
                raise ValueError("fixed mode requires all of fixed_ecef_x_m, fixed_ecef_y_m, fixed_ecef_z_m")

        port, baud = self.receiver_serial_target(cfg)
        with self.direct_serial_session(cfg, owner="tmode3_apply", timeout_s=1.5, ser_timeout=0.2) as ser:
            existing = self._poll_tmode3_payload(ser)
            payload = self._build_tmode3_payload_for_mode(
                existing_payload=existing,
                mode=mode,
                survey_min_dur=survey_min_dur,
                survey_acc_limit=survey_acc_limit,
                fixed_ecef_x_m=fixed_ecef_x_m,
                fixed_ecef_y_m=fixed_ecef_y_m,
                fixed_ecef_z_m=fixed_ecef_z_m,
            )
            self._send_ubx_and_wait_ack(ser, 0x06, 0x71, payload)
            confirmed = self._poll_tmode3_payload(ser)

        status = self._parse_tmode3_payload(confirmed)
        status["serial_port"] = port
        status["baud"] = baud
        return status

    def save_to_bbr_flash(self, cfg):
        port, baud = self.receiver_serial_target(cfg)
        save_mask = 0x0000FFFF
        payload = struct.pack("<IIIB", 0, save_mask, 0, 0x03)
        with self.direct_serial_session(cfg, owner="cfg_save", timeout_s=1.5, ser_timeout=0.2) as ser:
            self._send_ubx_and_wait_ack(ser, 0x06, 0x09, payload)
        return {"serial_port": port, "baud": baud, "saved": True}

    def ensure_ppp_messages_enabled_for_record(self, cfg, log_fn=None):
        self._direct_mode_lock.acquire()
        port, baud = self.record_serial_target(cfg)
        restart_stream = False
        try:
            if self._stream_running():
                if self._stream_has_clients():
                    raise RuntimeError("Receiver stream is busy. Stop active mode before changing receiver config.")
                self.stop_stream()
                restart_stream = True
            with self.open_record_serial(cfg, timeout_s=1.5, owner="record_cfg", ser_timeout=0.2) as ser:
                self._ensure_ppp_messages_enabled(ser, log_fn=log_fn)
        finally:
            if restart_stream:
                self.ensure_stream(cfg)
            self._direct_mode_lock.release()
        return {"serial_port": port, "baud": baud, "ok": True}

    @contextmanager
    def open_stream_client(self, cfg, timeout_s=2.0):
        self.ensure_stream(cfg)
        host, port = self.stream_endpoint(cfg)
        sock = socket.create_connection((host, port), timeout=timeout_s)
        sock.settimeout(1.0)
        try:
            yield sock
        finally:
            try:
                sock.close()
            except Exception:
                pass

    @contextmanager
    def open_receiver_serial(self, cfg, timeout_s=1.0, owner="receiver", ser_timeout=0.2):
        port, baud = self.receiver_serial_target(cfg)
        with self._acquire_owner(owner=owner, timeout_s=timeout_s):
            with serial.Serial(port=port, baudrate=baud, timeout=ser_timeout) as ser:
                yield ser

    @contextmanager
    def open_record_serial(self, cfg, timeout_s=1.0, owner="record", ser_timeout=0.2):
        port, baud = self.record_serial_target(cfg)
        with self._acquire_owner(owner=owner, timeout_s=timeout_s):
            with serial.Serial(port=port, baudrate=baud, timeout=ser_timeout) as ser:
                yield ser

    @contextmanager
    def _acquire_owner(self, owner, timeout_s=1.0):
        acquired = self._lock.acquire(timeout=max(0.0, float(timeout_s)))
        if not acquired:
            active = self._owner or "unknown"
            raise RuntimeError(f"Receiver serial busy (active owner: {active})")
        try:
            self._owner = owner
            yield
        finally:
            self._owner = None
            self._lock.release()

    def _set_runtime_error(self, message):
        with self._stream_lock:
            state = dict(self._runtime_state)
            state["available"] = False
            state["message"] = str(message)
            self._runtime_state = state

    def _update_runtime_state(self, updates):
        with self._stream_lock:
            state = dict(self._runtime_state)
            state.update(updates)
            state["updated_at"] = time.time()
            state["available"] = True
            state["message"] = ""
            self._runtime_state = state

    def _stream_loop(self, serial_port, serial_baud, stream_host, stream_port):
        server = None
        try:
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((stream_host, stream_port))
            server.listen(5)
            server.setblocking(False)
        except Exception as exc:
            if server is not None:
                try:
                    server.close()
                except Exception:
                    pass
            self._set_runtime_error(f"Stream bind failed: {exc}")
            return

        clients = []
        ubx_buffer = bytearray()
        nmea_buffer = bytearray()
        next_poll_at = 0.0
        self._set_runtime_error("Waiting for receiver data.")

        while not self._stream_stop_event.is_set():
            try:
                with serial.Serial(port=serial_port, baudrate=serial_baud, timeout=0.2) as ser:
                    self._set_runtime_error("Connected. Waiting for GNSS messages.")
                    while not self._stream_stop_event.is_set():
                        self._accept_stream_clients(server, clients)
                        self._drain_client_writes(clients, ser)

                        now = time.time()
                        if now >= next_poll_at:
                            try:
                                ser.write(self._ubx_frame(0x01, 0x07, b""))  # NAV-PVT poll
                                ser.write(self._ubx_frame(0x01, 0x3B, b""))  # NAV-SVIN poll
                                ser.write(self._ubx_frame(0x06, 0x71, b""))  # CFG-TMODE3 poll
                                ser.flush()
                            except Exception:
                                pass
                            next_poll_at = now + 1.0

                        chunk = ser.read(4096)
                        if chunk:
                            self._broadcast_chunk(clients, chunk)
                            self._consume_nmea_chunk(nmea_buffer, chunk)
                            self._consume_ubx_chunk(ubx_buffer, chunk)
            except Exception as exc:
                self._set_runtime_error(str(exc))
                if self._stream_stop_event.wait(timeout=1.0):
                    break
            finally:
                self._close_all_clients(clients)

        self._close_all_clients(clients)
        try:
            server.close()
        except Exception:
            pass

    def _accept_stream_clients(self, server, clients):
        while True:
            try:
                conn, _ = server.accept()
            except BlockingIOError:
                break
            except Exception:
                break
            conn.setblocking(False)
            clients.append(conn)
            with self._stream_lock:
                self._stream_client_count = len(clients)

    def _close_all_clients(self, clients):
        while clients:
            sock = clients.pop()
            try:
                sock.close()
            except Exception:
                pass
        with self._stream_lock:
            self._stream_client_count = 0

    def _drain_client_writes(self, clients, ser):
        dead = []
        for sock in clients:
            try:
                data = sock.recv(4096)
                if not data:
                    dead.append(sock)
                    continue
                ser.write(data)
            except BlockingIOError:
                continue
            except Exception:
                dead.append(sock)
        for sock in dead:
            try:
                clients.remove(sock)
            except ValueError:
                pass
            try:
                sock.close()
            except Exception:
                pass
        if dead:
            with self._stream_lock:
                self._stream_client_count = len(clients)

    def _broadcast_chunk(self, clients, chunk):
        dead = []
        for sock in clients:
            try:
                sock.sendall(chunk)
            except Exception:
                dead.append(sock)
        for sock in dead:
            try:
                clients.remove(sock)
            except ValueError:
                pass
            try:
                sock.close()
            except Exception:
                pass
        if dead:
            with self._stream_lock:
                self._stream_client_count = len(clients)

    def _consume_nmea_chunk(self, nmea_buffer, chunk):
        nmea_buffer.extend(chunk)
        while True:
            idx = nmea_buffer.find(b"\n")
            if idx < 0:
                break
            raw = bytes(nmea_buffer[:idx + 1])
            del nmea_buffer[:idx + 1]
            text = raw.decode("ascii", errors="ignore").strip()
            if text.startswith("$"):
                with self._stream_lock:
                    self._nmea_lines.append(text)

    def _consume_ubx_chunk(self, ubx_buffer, chunk):
        ubx_buffer.extend(chunk)
        for msg_class, msg_id, payload in self._extract_ubx_messages(ubx_buffer):
            self._handle_ubx_message(msg_class, msg_id, payload)

    def _extract_ubx_messages(self, buffer):
        out = []
        while True:
            start = buffer.find(b"\xB5\x62")
            if start < 0:
                if len(buffer) > 2:
                    del buffer[:-2]
                break
            if start > 0:
                del buffer[:start]
            if len(buffer) < 8:
                break
            payload_len = struct.unpack("<H", bytes(buffer[4:6]))[0]
            total = 2 + 4 + payload_len + 2
            if len(buffer) < total:
                break
            frame = bytes(buffer[:total])
            del buffer[:total]
            header = frame[2:6]
            payload = frame[6:6 + payload_len]
            checksum = frame[6 + payload_len:6 + payload_len + 2]
            if self._ubx_checksum(header + payload) != checksum:
                continue
            out.append((header[0], header[1], payload))
        return out

    def _handle_ubx_message(self, msg_class, msg_id, payload):
        if msg_class == 0x01 and msg_id == 0x07:
            try:
                pvt = self._parse_nav_pvt_payload(payload)
                self._update_runtime_state(pvt)
            except Exception:
                return
            return
        if msg_class == 0x01 and msg_id == 0x3B:
            try:
                svin = self._parse_nav_svin_payload(payload)
                self._update_runtime_state(svin)
            except Exception:
                return
            return
        if msg_class == 0x06 and msg_id == 0x71:
            try:
                tmode = self._parse_tmode3_payload(payload)
                self._update_runtime_state({"tmode_mode": tmode.get("mode")})
            except Exception:
                return

    def _ubx_checksum(self, data):
        ck_a = 0
        ck_b = 0
        for byte in data:
            ck_a = (ck_a + byte) & 0xFF
            ck_b = (ck_b + ck_a) & 0xFF
        return bytes([ck_a, ck_b])

    def _ubx_frame(self, msg_class, msg_id, payload=b""):
        header = bytes([msg_class, msg_id]) + struct.pack("<H", len(payload))
        return b"\xB5\x62" + header + payload + self._ubx_checksum(header + payload)

    def _read_ubx_message(self, ser, timeout_s=1.5):
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
            if self._ubx_checksum(header + payload) != checksum:
                continue
            return msg_class, msg_id, payload
        return None

    def _send_ubx_and_wait_ack(self, ser, msg_class, msg_id, payload=b"", timeout_s=1.5):
        ser.reset_input_buffer()
        ser.write(self._ubx_frame(msg_class, msg_id, payload))
        ser.flush()
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            message = self._read_ubx_message(ser, timeout_s=max(0.1, deadline - time.time()))
            if message is None:
                break
            cls, mid, pl = message
            if cls == 0x05 and mid == 0x01 and len(pl) >= 2 and pl[0] == msg_class and pl[1] == msg_id:
                return
            if cls == 0x05 and mid == 0x00 and len(pl) >= 2 and pl[0] == msg_class and pl[1] == msg_id:
                raise RuntimeError(f"Receiver NAK for UBX-{msg_class:02X}-{msg_id:02X}")
        raise RuntimeError(f"No ACK for UBX-{msg_class:02X}-{msg_id:02X}")

    def _poll_tmode3_payload(self, ser):
        ser.reset_input_buffer()
        ser.write(self._ubx_frame(0x06, 0x71, b""))
        ser.flush()
        deadline = time.time() + 1.5
        while time.time() < deadline:
            message = self._read_ubx_message(ser, timeout_s=max(0.1, deadline - time.time()))
            if message is None:
                break
            msg_class, msg_id, payload = message
            if msg_class == 0x06 and msg_id == 0x71:
                return payload
        raise RuntimeError("No response to UBX-CFG-TMODE3 poll")

    def _poll_nav_pvt_payload(self, ser):
        ser.reset_input_buffer()
        ser.write(self._ubx_frame(0x01, 0x07, b""))
        ser.flush()
        deadline = time.time() + 1.5
        while time.time() < deadline:
            message = self._read_ubx_message(ser, timeout_s=max(0.1, deadline - time.time()))
            if message is None:
                break
            msg_class, msg_id, payload = message
            if msg_class == 0x01 and msg_id == 0x07:
                return payload
        raise RuntimeError("No response to UBX-NAV-PVT poll")

    def _poll_nav_svin_payload(self, ser):
        ser.reset_input_buffer()
        ser.write(self._ubx_frame(0x01, 0x3B, b""))
        ser.flush()
        deadline = time.time() + 1.5
        while time.time() < deadline:
            message = self._read_ubx_message(ser, timeout_s=max(0.1, deadline - time.time()))
            if message is None:
                break
            msg_class, msg_id, payload = message
            if msg_class == 0x01 and msg_id == 0x3B:
                return payload
        raise RuntimeError("No response to UBX-NAV-SVIN poll")

    def _poll_cfg_msg_rates(self, ser, target_class, target_id):
        ser.reset_input_buffer()
        ser.write(self._ubx_frame(0x06, 0x01, bytes([target_class, target_id])))
        ser.flush()
        deadline = time.time() + 1.5
        while time.time() < deadline:
            message = self._read_ubx_message(ser, timeout_s=max(0.1, deadline - time.time()))
            if message is None:
                break
            msg_class, msg_id, payload = message
            if msg_class == 0x06 and msg_id == 0x01 and len(payload) >= 8 and payload[0] == target_class and payload[1] == target_id:
                return list(payload[2:8])
        raise RuntimeError(f"No UBX-CFG-MSG poll response for {target_class:02X}-{target_id:02X}")

    def _set_cfg_msg_rates(self, ser, target_class, target_id, rates):
        payload = bytes([target_class, target_id] + [int(v) & 0xFF for v in rates[:6]])
        self._send_ubx_and_wait_ack(ser, 0x06, 0x01, payload)

    def _log(self, log_fn, message):
        if callable(log_fn):
            log_fn(message)

    def _ensure_ppp_messages_enabled(self, ser, log_fn=None):
        targets = [
            (0x02, 0x15, "UBX-RXM-RAWX"),
            (0x02, 0x13, "UBX-RXM-SFRBX"),
        ]

        for msg_class, msg_id, label in targets:
            rates = [0, 0, 0, 0, 0, 0]
            try:
                rates = self._poll_cfg_msg_rates(ser, msg_class, msg_id)
            except Exception as exc:
                self._log(log_fn, f"Could not poll {label} rates ({exc}). Applying USB rate=1.")

            if len(rates) < 6:
                rates = (rates + [0, 0, 0, 0, 0, 0])[:6]

            usb_rate_before = rates[3]
            if usb_rate_before == 1:
                self._log(log_fn, f"{label} already enabled on USB.")
                continue

            rates[3] = 1
            self._set_cfg_msg_rates(ser, msg_class, msg_id, rates)
            self._log(log_fn, f"Enabled {label} on USB (rate {usb_rate_before} -> 1).")

    def _parse_tmode3_payload(self, payload):
        if len(payload) < 32:
            raise RuntimeError("Invalid UBX-CFG-TMODE3 response payload length")
        version = payload[0]
        flags = struct.unpack("<H", payload[2:4])[0]
        mode_code = flags & 0xFF
        mode = {0: "disabled", 1: "survey", 2: "fixed"}.get(mode_code, f"unknown({mode_code})")
        lla = bool(flags & 0x100)

        x_cm, y_cm, z_cm = struct.unpack("<iii", payload[4:16])
        x_hp, y_hp, z_hp = struct.unpack("<bbb", payload[16:19])
        fixed_pos_acc, svin_min_dur, svin_acc_limit = struct.unpack("<III", payload[20:32])

        return {
            "version": int(version),
            "mode": mode,
            "flags": int(flags),
            "lla": lla,
            "ecef_x_m": (x_cm / 100.0) + (x_hp / 10000.0),
            "ecef_y_m": (y_cm / 100.0) + (y_hp / 10000.0),
            "ecef_z_m": (z_cm / 100.0) + (z_hp / 10000.0),
            "fixed_pos_acc_0_1mm": int(fixed_pos_acc),
            "survey_min_dur_s": int(svin_min_dur),
            "survey_acc_limit_0_1mm": int(svin_acc_limit),
        }

    def _parse_nav_pvt_payload(self, payload):
        if len(payload) < 44:
            raise RuntimeError("Invalid UBX-NAV-PVT payload length")
        lon = struct.unpack("<i", payload[24:28])[0] * 1e-7
        lat = struct.unpack("<i", payload[28:32])[0] * 1e-7
        h_acc_mm = struct.unpack("<I", payload[40:44])[0]
        return {
            "lat_deg": lat,
            "lon_deg": lon,
            "h_acc_m": h_acc_mm / 1000.0,
        }

    def _parse_nav_svin_payload(self, payload):
        if len(payload) < 34:
            raise RuntimeError("Invalid UBX-NAV-SVIN payload length")
        if len(payload) >= 40 and payload[1:4] == b"\x00\x00\x00":
            dur = struct.unpack("<I", payload[8:12])[0]
            mean_acc_0_1mm = struct.unpack("<I", payload[28:32])[0]
            valid = bool(payload[36])
            active = bool(payload[37])
            return {
                "svin_duration_s": int(dur),
                "svin_accuracy_m": mean_acc_0_1mm * 0.0001,
                "svin_valid": valid,
                "svin_active": active,
            }

        dur = struct.unpack("<I", payload[4:8])[0]
        mean_acc_0_1mm = struct.unpack("<I", payload[24:28])[0]
        valid = bool(payload[32])
        active = bool(payload[33])
        return {
            "svin_duration_s": int(dur),
            "svin_accuracy_m": mean_acc_0_1mm * 0.0001,
            "svin_valid": valid,
            "svin_active": active,
        }

    def _meters_to_cm_and_hp(self, meters):
        total_0_1mm = int(round(float(meters) * 10000.0))
        cm = int(total_0_1mm / 100)
        hp = total_0_1mm - (cm * 100)
        if hp < -99:
            hp = -99
        if hp > 99:
            hp = 99
        return cm, hp

    def _build_tmode3_payload_for_mode(
        self,
        existing_payload,
        mode,
        survey_min_dur,
        survey_acc_limit,
        fixed_ecef_x_m=None,
        fixed_ecef_y_m=None,
        fixed_ecef_z_m=None,
    ):
        payload = bytearray(existing_payload)
        if len(payload) < 40:
            payload = bytearray(payload.ljust(40, b"\x00"))

        flags = struct.unpack("<H", payload[2:4])[0]
        flags = flags & 0xFE00
        if mode == "survey":
            flags |= 1
            payload[24:28] = struct.pack("<I", max(1, survey_min_dur))
            payload[28:32] = struct.pack("<I", max(1, survey_acc_limit))
        elif mode == "fixed":
            flags |= 2
            if fixed_ecef_x_m is not None and fixed_ecef_y_m is not None and fixed_ecef_z_m is not None:
                x_cm, x_hp = self._meters_to_cm_and_hp(fixed_ecef_x_m)
                y_cm, y_hp = self._meters_to_cm_and_hp(fixed_ecef_y_m)
                z_cm, z_hp = self._meters_to_cm_and_hp(fixed_ecef_z_m)
                payload[4:8] = struct.pack("<i", x_cm)
                payload[8:12] = struct.pack("<i", y_cm)
                payload[12:16] = struct.pack("<i", z_cm)
                payload[16:17] = struct.pack("<b", x_hp)
                payload[17:18] = struct.pack("<b", y_hp)
                payload[18:19] = struct.pack("<b", z_hp)
        else:
            raise ValueError("mode must be 'survey' or 'fixed'")
        payload[2:4] = struct.pack("<H", flags)
        return bytes(payload)


receiver_service = ReceiverSerialService()
