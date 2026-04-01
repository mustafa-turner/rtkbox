"""Config and stream URL helpers for rtkbox."""

import socket

import yaml


MODES = ["base-local", "base-ntrip", "rover-local", "rover-ntrip", "receiver-bridge", "record", "nmea"]


def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError("config root must be a YAML mapping")
    return data


def load_config_text(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def save_config(path, data):
    if not isinstance(data, dict):
        raise ValueError("config root must be a YAML mapping")
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False)


def save_config_text(path, text):
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise ValueError("config root must be a YAML mapping")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def get_required(cfg, path):
    cur = cfg
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            raise ValueError(f"missing config key: {path}")
        cur = cur[part]
    return cur


def normalize_serial_port(port):
    return port if str(port).startswith("/dev/") else f"/dev/{port}"


def normalize_str2str_serial_port(port):
    text = str(port)
    return text[5:] if text.startswith("/dev/") else text


def build_serial_url(cfg):
    port = normalize_str2str_serial_port(get_required(cfg, "serial.port"))
    baud = get_required(cfg, "serial.baud")
    return f"serial://{port}:{baud}:8:n:1:off"


def build_serial_url_from_values(port, baud):
    return f"serial://{normalize_str2str_serial_port(port)}:{baud}:8:n:1:off"


def build_ntrip_url(section, scheme):
    host = get_required(section, "host")
    port = get_required(section, "port")
    mountpoint = get_required(section, "mountpoint")
    user = section.get("user", "")
    password = section.get("password", "")
    return f"{scheme}://{user}:{password}@{host}:{port}/{mountpoint}"


def build_mode_streams(mode, cfg):
    serial_url = build_serial_url(cfg)

    if mode == "base-local":
        base_local = get_required(cfg, "base_local")
        bind_host = str(base_local.get("bind_host", ""))
        port = get_required(cfg, "base_local.port")
        out_url = f"tcpsvr://{bind_host}:{port}"
        fmt = base_local.get("format", "")
        if fmt:
            out_url = f"{out_url}#{fmt}"
        return serial_url, out_url

    if mode == "base-ntrip":
        caster = get_required(cfg, "caster")
        return serial_url, build_ntrip_url(caster, "ntrips")

    if mode == "rover-local":
        rover_local = get_required(cfg, "rover_local")
        host = get_required(cfg, "rover_local.host")
        port = get_required(cfg, "rover_local.port")
        return f"tcpcli://{host}:{port}", serial_url

    if mode == "rover-ntrip":
        rover_ntrip = get_required(cfg, "rover_ntrip")
        scheme = rover_ntrip.get("scheme", "ntrip")
        if scheme not in {"ntrip", "ntripc"}:
            raise ValueError("rover_ntrip.scheme must be 'ntrip' or 'ntripc'")
        return build_ntrip_url(rover_ntrip, scheme), serial_url

    if mode == "receiver-bridge":
        receiver_bridge = get_required(cfg, "receiver_bridge")
        bind_host = str(receiver_bridge.get("bind_host", ""))
        port = get_required(cfg, "receiver_bridge.port")
        serial_port = receiver_bridge.get("serial_port", get_required(cfg, "serial.port"))
        baud = receiver_bridge.get("baud", get_required(cfg, "serial.baud"))
        in_url = build_serial_url_from_values(serial_port, baud)
        return in_url, f"tcpsvr://{bind_host}:{port}"

    raise ValueError(f"unsupported mode: {mode}")


def detect_local_ip():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()
