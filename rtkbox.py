"""CLI entrypoint for rtkbox."""

import argparse
import sys

from rtkbox_config import MODES, load_config
from rtkbox_modes import run_mode
from rtkbox_portal import run_portal


def parse_args():
    parser = argparse.ArgumentParser(description="RTK box CLI")
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to YAML config file (default: config.yaml)",
    )
    parser.add_argument(
        "mode",
        choices=MODES + ["portal"],
        help="Run mode",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    try:
        if args.mode == "portal":
            run_portal(args.config)
            return
        cfg = load_config(args.config)
        run_mode(args.mode, cfg)
    except (ValueError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
