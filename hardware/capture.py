"""Capture one complete physical-board protocol run without altering its records."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from evaluate import CONFIG, ROOT, load_config, parse_log


def git_output(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True)
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--timeout-seconds", type=int, default=5400)
    args = parser.parse_args()
    try:
        import serial
    except ImportError as exc:
        raise SystemExit("pyserial is required: use the ESP-IDF Python environment") from exc
    commit = git_output("rev-parse", "HEAD")
    if not args.allow_dirty and git_output("status", "--porcelain", "--untracked-files=all"):
        raise SystemExit("formal capture requires a clean Git tree")
    config, digest = load_config(CONFIG)
    started = datetime.now(timezone.utc)
    # Keep only the unmodified ST_* protocol lines. Boot chatter is not part of
    # the experiment and can contain device identifiers on some boards.
    lines = []
    with serial.Serial(args.port, args.baud, timeout=1) as port:
        port.dtr = False
        port.rts = True
        import time
        time.sleep(0.1)
        port.rts = False
        deadline = time.monotonic() + args.timeout_seconds
        while time.monotonic() < deadline:
            line = port.readline().decode("utf-8", errors="replace").strip()
            if not line.startswith("ST_"):
                continue
            if re.search(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", line):
                raise SystemExit("device identifier appeared in protocol")
            lines.append(line)
            if line.startswith("ST_BEGIN "):
                print("Experiment started", flush=True)
            if line.startswith(("ST_END ", "ST_ERROR ")):
                break
        else:
            raise SystemExit("capture timed out; no formal result saved")
    text = "\n".join(lines) + "\n"
    begin, _ = parse_log(text, config, digest, allow_dirty=args.allow_dirty)
    if begin["git_commit"] != commit:
        raise SystemExit("firmware commit differs from current code commit")
    root = ROOT / "results" / ("experimental" if args.allow_dirty else "v0.2")
    raw_dir = root / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    path = raw_dir / f"{config['experiment_id']}_{stamp}.log"
    if path.exists():
        raise SystemExit(f"refusing to overwrite {path}")
    path.write_text(text, encoding="utf-8")
    sidecar = path.with_suffix(".capture.json")
    sidecar.write_text(json.dumps({"capture_started_utc": started.isoformat(),
                                   "capture_finished_utc": datetime.now(timezone.utc).isoformat(),
                                   "baud": args.baud}, indent=2) + "\n")
    print(path)
    print("Run hardware/evaluate.py on this log to generate tables.")


if __name__ == "__main__":
    main()
