#!/usr/bin/env python3
"""Write the synthetic scenarios to results/dataset/*.csv.

Each dataset file is self-describing: it carries the scenario name, the
expected fault token and the exact channel configuration the core must use,
so the replay step cannot accidentally run a scenario with a different
configuration than the one it was generated for.

    python simulator/generate.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "simulator"))

from scenarios import SCENARIOS, SAMPLE_INTERVAL_MS, Scenario  # noqa: E402

DATASET_DIR = ROOT / "results" / "dataset"
CONFIG_KEY_ORDER = [
    "min_value",
    "max_value",
    "stuck_epsilon",
    "stuck_window",
    "spike_threshold",
    "drift_window",
    "drift_threshold",
    "missing_limit",
]


def render_dataset(scenario: Scenario) -> str:
    config_text = " ".join(
        f"{key}={scenario.config[key]!r}" for key in CONFIG_KEY_ORDER
    )
    lines = [
        "# SensorTrust v0.1 scenario dataset",
        f"# scenario: {scenario.name}",
        f"# expected: {scenario.expected}",
        f"# channel: {scenario.channel}",
        f"# sample_interval_ms: {SAMPLE_INTERVAL_MS}",
        f"#config: {config_text}",
        "timestamp_ms,value,valid",
    ]
    for timestamp_ms, value, valid in scenario.samples:
        value_text = f"{value:.4f}" if valid else "nan"
        lines.append(f"{timestamp_ms},{value_text},{1 if valid else 0}")
    return "\n".join(lines) + "\n"


def write_datasets(dataset_dir: Path = DATASET_DIR) -> list[Path]:
    dataset_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for scenario in SCENARIOS:
        path = dataset_dir / f"{scenario.name}.csv"
        path.write_text(render_dataset(scenario), encoding="utf-8")
        written.append(path)
    return written


def main() -> int:
    written = write_datasets()
    print(f"wrote {len(written)} datasets to {DATASET_DIR.relative_to(ROOT)}")
    for scenario in SCENARIOS:
        invalid = sum(1 for _, _, valid in scenario.samples if not valid)
        print(
            f"  {scenario.name:<16} channel={scenario.channel:<16} "
            f"samples={scenario.row_count:<4} invalid={invalid:<3} "
            f"expected={scenario.expected}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
