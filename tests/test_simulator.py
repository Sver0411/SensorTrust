"""Tests for the simulator: generator, host replay harness and the report.

These tests check the *plumbing* around the C core (that the scenarios are
built as documented, that the datasets are self-describing, that the report on
disk matches a fresh run, and that the C tests really pass). The detection
logic itself is tested in tests/test_core.c, and by running every scenario
through the real core here.

    python -m pytest tests/ -v
"""

from __future__ import annotations

import csv
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "simulator"))

import generate  # noqa: E402
import run as runner  # noqa: E402
import scenarios  # noqa: E402

EXPECTED_SCENARIOS = {
    "healthy": "NONE",
    "healthy_dynamic": "NONE",
    "stuck": "STUCK",
    "spike": "SPIKE",
    "drift": "DRIFT",
    "out_of_range": "RANGE",
    "missing_data": "MISSING",
}

_CACHED = {}


@pytest.fixture(scope="session")
def dataset_dir() -> Path:
    """Make sure the datasets on disk match the current scenario definitions."""
    runner.ensure_datasets()
    generate.write_datasets(runner.DATASET_DIR)
    return runner.DATASET_DIR


def replay_tool() -> Path:
    """Build the host replay harness once per session."""
    if "tool" not in _CACHED:
        _CACHED["tool"] = runner.build_replay_tool()
    return _CACHED["tool"]


def c_test_binary() -> Path:
    """Compile and run the C tests; returns the completed process."""
    if "c_tests" not in _CACHED:
        compiler = shutil.which("cc") or shutil.which("clang") or shutil.which("gcc")
        assert compiler is not None, "no C compiler available"
        binary = runner.BUILD_DIR / "test_core"
        runner.BUILD_DIR.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(
            [
                compiler,
                "-std=c11",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-O2",
                str(ROOT / "core" / "sensor_trust.c"),
                str(ROOT / "tests" / "test_core.c"),
                "-o",
                str(binary),
                "-lm",
            ],
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr
        _CACHED["c_tests"] = subprocess.run([str(binary)], capture_output=True, text=True)
    return _CACHED["c_tests"]


# --------------------------------------------------------------------------
# scenario definitions and datasets
# --------------------------------------------------------------------------


def test_scenario_set_matches_the_v0_1_spec():
    names = [scenario.name for scenario in scenarios.SCENARIOS]
    assert names == list(EXPECTED_SCENARIOS)
    for scenario in scenarios.SCENARIOS:
        assert scenario.expected == EXPECTED_SCENARIOS[scenario.name]
        assert scenario.samples, f"{scenario.name} has no samples"
        # 1 Hz, strictly increasing timestamps
        timestamps = [sample[0] for sample in scenario.samples]
        assert timestamps == sorted(timestamps)
        assert len(set(timestamps)) == len(timestamps)
        assert timestamps[0] == scenarios.SAMPLE_INTERVAL_MS
        # every configured window fits inside the core's fixed buffer
        assert scenario.config["stuck_window"] <= 64
        assert scenario.config["drift_window"] <= 64
        assert scenario.config["min_value"] < scenario.config["max_value"]


def test_datasets_are_self_describing_and_match_their_config(dataset_dir):
    for scenario in scenarios.SCENARIOS:
        path = dataset_dir / f"{scenario.name}.csv"
        assert path.is_file(), f"missing dataset for {scenario.name}"
        lines = path.read_text(encoding="utf-8").splitlines()
        assert f"# scenario: {scenario.name}" in lines
        assert f"# expected: {scenario.expected}" in lines

        config_line = next(line for line in lines if line.startswith("#config:"))
        parsed = {}
        for token in config_line[len("#config:") :].split():
            key, _, value = token.partition("=")
            parsed[key] = float(value) if "." in value else int(value)
        assert parsed == scenario.config, f"{scenario.name} config drift"

        # the same config is what the core accepts
        assert parsed["missing_limit"] >= 1

        rows = [line for line in lines if line and not line.startswith("#")]
        assert rows[0] == "timestamp_ms,value,valid"
        data_rows = rows[1:]
        assert len(data_rows) == scenario.row_count
        for index, (row, sample) in enumerate(zip(data_rows, scenario.samples)):
            timestamp_text, value_text, valid_text = row.split(",")
            assert int(timestamp_text) == sample[0]
            assert (valid_text == "1") is sample[2]
            if not sample[2]:
                assert value_text == "nan"
            else:
                assert float(value_text) == pytest.approx(sample[1], abs=1e-4)
            assert int(valid_text) in (0, 1)
            if index == 0:
                assert timestamp_text != ""


def test_scenario_streams_have_the_documented_shape():
    out_of_range = scenarios.scenario_by_name("out_of_range")
    config = out_of_range.config
    values = [value for _, value, valid in out_of_range.samples if valid]
    assert max(values) > config["max_value"]
    assert any(value <= config["max_value"] for value in values)

    missing = scenarios.scenario_by_name("missing_data")
    longest_run = current = 0
    for _, _, valid in missing.samples:
        current = 0 if valid else current + 1
        longest_run = max(longest_run, current)
    assert longest_run >= missing.config["missing_limit"]

    # the drift scenario ramps the whole way, the healthy one does not
    drift_values = [value for _, value, _ in scenarios.scenario_by_name("drift").samples]
    healthy_values = [
        value for _, value, _ in scenarios.scenario_by_name("healthy_dynamic").samples
    ]
    assert drift_values[-1] - drift_values[0] > 3.0
    assert healthy_values[-1] - healthy_values[0] < 2.0


# --------------------------------------------------------------------------
# end to end: real core, real datasets
# --------------------------------------------------------------------------


def test_every_scenario_detects_its_expected_fault(dataset_dir):
    replay_tool()
    report = {}

    for scenario in scenarios.SCENARIOS:
        rows = runner.replay_dataset(dataset_dir / f"{scenario.name}.csv")
        summary = runner.summarise(scenario, rows)
        report[scenario.name] = summary

        assert summary["matched"] == "yes", (
            f"{scenario.name}: expected {summary['expected']}, "
            f"detected {summary['detected']}"
        )
        assert 0 <= summary["health_score_min"] <= 100
        assert summary["final_state"] in {"HEALTHY", "DEGRADED", "FAULT"}

    # a healthy stream must produce no flag at all and keep a full score
    for name in ("healthy", "healthy_dynamic"):
        assert report[name]["detected"] == "NONE"
        assert report[name]["health_score_min"] == 100
        assert report[name]["final_state"] == "HEALTHY"

    # a confirmed fault always leaves HEALTHY behind
    for name in ("stuck", "spike", "drift", "out_of_range", "missing_data"):
        assert report[name]["detected"] != "NONE"
        assert report[name]["health_score_min"] < 100

    assert report["out_of_range"]["final_state"] == "FAULT"
    assert report["stuck"]["health_score_min"] < report["spike"]["health_score_min"]


def test_report_csv_matches_a_fresh_run(dataset_dir):
    path = runner.RESULTS_CSV
    assert path.is_file(), "results/scenarios.csv is missing (run simulator/run.py)"

    with path.open(encoding="utf-8", newline="") as handle:
        stored = list(csv.DictReader(handle))

    assert [row["scenario"] for row in stored] == list(EXPECTED_SCENARIOS)
    assert list(stored[0].keys()) == runner.REPORT_COLUMNS

    fresh = runner.run_scenarios()
    for stored_row, fresh_row in zip(stored, fresh):
        for column in runner.REPORT_COLUMNS:
            assert stored_row[column] == str(fresh_row[column]), (
                f"{stored_row['scenario']}/{column}: "
                f"file says {stored_row[column]}, fresh run says {fresh_row[column]}"
            )


def test_fault_bits_match_the_c_header():
    bits = runner.fault_bits_from_header()
    assert bits == runner.FAULT_BITS, (
        "the flag values in run.py no longer match core/sensor_trust.h"
    )
    assert runner.flags_to_text(0) == "NONE"
    assert runner.flags_to_text(bits["DRIFT"]) == "DRIFT"
    assert runner.flags_to_text(bits["RANGE"] | bits["MISSING"]) == "RANGE|MISSING"


def test_c_host_tests_pass():
    completed = c_test_binary()
    assert completed.returncode == 0, completed.stdout + completed.stderr
    match = re.search(r"(\d+) tests, (\d+) passed, (\d+) failed", completed.stdout)
    assert match, completed.stdout
    total, passed, failed = (int(group) for group in match.groups())
    assert failed == 0
    assert passed == total
    assert 15 <= total <= 25
