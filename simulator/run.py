#!/usr/bin/env python3
"""Replay the synthetic scenarios through the real C core and summarise what it
reported.

The Python side deliberately does not implement any detection logic: it
generates the datasets (generate.py), compiles simulator/replay_main.c against
core/sensor_trust.c, feeds every sample through that binary and only
aggregates the results into results/scenarios.csv.

    python simulator/run.py
"""

from __future__ import annotations

import csv
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "simulator"))

import generate  # noqa: E402
from scenarios import SCENARIOS, Scenario  # noqa: E402

CORE_DIR = ROOT / "core"
BUILD_DIR = ROOT / "build"
DATASET_DIR = ROOT / "results" / "dataset"
RESULTS_CSV = ROOT / "results" / "scenarios.csv"
REPLAY_BINARY = BUILD_DIR / "sensor_trust_replay"

# Flag bit values. tests/test_simulator.py checks these against the #defines in
# core/sensor_trust.h, so the two cannot drift apart silently.
FAULT_BITS: Dict[str, int] = {
    "RANGE": 1 << 0,
    "STUCK": 1 << 1,
    "SPIKE": 1 << 2,
    "DRIFT": 1 << 3,
    "MISSING": 1 << 4,
}
FAULT_ORDER = ["RANGE", "STUCK", "SPIKE", "DRIFT", "MISSING"]

REPORT_COLUMNS = [
    "scenario",
    "expected",
    "detected",
    "matched",
    "health_score_min",
    "final_state",
]


def flags_to_tokens(fault_flags: int) -> List[str]:
    return [name for name in FAULT_ORDER if fault_flags & FAULT_BITS[name]]


def flags_to_text(fault_flags: int) -> str:
    tokens = flags_to_tokens(fault_flags)
    return "|".join(tokens) if tokens else "NONE"


def fault_bits_from_header(header_path: Path | None = None) -> Dict[str, int]:
    """Read the FAULT_* bit definitions out of the public header."""
    header = (header_path or (CORE_DIR / "sensor_trust.h")).read_text(encoding="utf-8")
    bits: Dict[str, int] = {}
    for name, shift in re.findall(r"#define\s+FAULT_(\w+)\s+\(1u\s*<<\s*(\d+)\)", header):
        bits[name] = 1 << int(shift)
    return bits


def build_replay_tool() -> Path:
    compiler = shutil.which("cc") or shutil.which("clang") or shutil.which("gcc")
    if compiler is None:
        raise RuntimeError("no C compiler found (tried cc, clang, gcc)")

    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    command = [
        compiler,
        "-std=c11",
        "-Wall",
        "-Wextra",
        "-Werror",
        "-O2",
        "-I",
        str(CORE_DIR),
        "-o",
        str(REPLAY_BINARY),
        str(CORE_DIR / "sensor_trust.c"),
        str(ROOT / "simulator" / "replay_main.c"),
        "-lm",
    ]
    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.returncode != 0:
        raise RuntimeError(
            "failed to build the replay tool:\n"
            + " ".join(command)
            + "\n"
            + completed.stdout
            + completed.stderr
        )
    return REPLAY_BINARY


def ensure_datasets() -> None:
    missing = [
        scenario.name
        for scenario in SCENARIOS
        if not (DATASET_DIR / f"{scenario.name}.csv").is_file()
    ]
    if missing:
        print(f"dataset(s) missing ({', '.join(missing)}): regenerating")
        generate.write_datasets(DATASET_DIR)


def replay_dataset(dataset_path: Path) -> List[dict]:
    completed = subprocess.run(
        [str(REPLAY_BINARY), str(dataset_path)], capture_output=True, text=True
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"replay failed for {dataset_path.name}: {completed.stderr.strip()}"
        )

    rows: List[dict] = []
    reader = csv.DictReader(completed.stdout.splitlines())
    for row in reader:
        rows.append(
            {
                "sample_index": int(row["sample_index"]),
                "timestamp_ms": int(row["timestamp_ms"]),
                "value": float(row["value"]),
                "valid": int(row["valid"]) != 0,
                "health_score": int(row["health_score"]),
                "state": row["state"],
                "fault_flags": int(row["fault_flags"]),
            }
        )
    if not rows:
        raise RuntimeError(f"replay produced no rows for {dataset_path.name}")
    return rows


def summarise(scenario: Scenario, rows: List[dict]) -> dict:
    union_flags = 0
    first_detection = None
    weakest_row = rows[0]
    for row in rows:
        union_flags |= row["fault_flags"]
        if row["fault_flags"] and first_detection is None:
            first_detection = row["sample_index"]
        if row["health_score"] < weakest_row["health_score"]:
            weakest_row = row

    detected = flags_to_text(union_flags)
    detected_tokens = set(flags_to_tokens(union_flags))
    if scenario.expected == "NONE":
        # a healthy stream must produce no flag at all: any flag is a false positive
        matched = union_flags == 0
    else:
        # the expected fault has to be reported; extra flags are legitimate
        # (the detectors are independent and a stream can carry several faults)
        matched = set(scenario.expected.split("|")) <= detected_tokens

    return {
        "scenario": scenario.name,
        "expected": scenario.expected,
        "detected": detected,
        "matched": "yes" if matched else "no",
        "health_score_min": weakest_row["health_score"],
        "final_state": rows[-1]["state"],
        "samples": len(rows),
        "first_detection_sample": "" if first_detection is None else first_detection,
    }


def run_scenarios() -> List[dict]:
    ensure_datasets()
    build_replay_tool()
    report = []
    for scenario in SCENARIOS:
        rows = replay_dataset(DATASET_DIR / f"{scenario.name}.csv")
        report.append(summarise(scenario, rows))
    return report


def write_report(report: List[dict], path: Path = RESULTS_CSV) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REPORT_COLUMNS)
        writer.writeheader()
        for row in report:
            writer.writerow({column: row[column] for column in REPORT_COLUMNS})
    return path


def format_table(report: List[dict]) -> str:
    header = ("Scenario", "Expected", "Detected", "Health Score")
    rows = [
        (
            row["scenario"],
            row["expected"],
            row["detected"],
            str(row["health_score_min"]),
        )
        for row in report
    ]
    widths = [len(header[index]) for index in range(len(header))]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    def render(cells: tuple) -> str:
        return "| " + " | ".join(
            cell.ljust(widths[index]) for index, cell in enumerate(cells)
        ) + " |"

    lines = [render(header), "|" + "|".join("-" * (width + 2) for width in widths) + "|"]
    lines.extend(render(row) for row in rows)
    return "\n".join(lines)


def main() -> int:
    report = run_scenarios()
    path = write_report(report)
    print(format_table(report))
    print(f"\n{path.relative_to(ROOT)}  (health_score_min = worst score seen in the run)")
    mismatched = [row["scenario"] for row in report if row["matched"] != "yes"]
    if mismatched:
        print(f"scenarios whose detection did not match the expectation: {mismatched}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
