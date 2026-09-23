"""Host-only checks of the evidence boundary; no fake log is shipped as data."""
from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hardware"))
from evaluate import (BITS, CONFIG, EvidenceError, clean_baseline, episodes,
                      expected_schedule, load_config, metrics, parse_log)


@pytest.fixture
def fake_run():
    config, digest = load_config(CONFIG)
    config = copy.deepcopy(config)
    config["baseline_samples"] = 2
    config["repetitions"] = 1
    config["recovery_samples"] = 2
    for mode in config["injection"]:
        config["injection"][mode]["duration_samples"] = 2
    # The hash in this fixture represents a frozen test manifest.
    digest = "a" * 64
    begin = {
        "schema": 1, "experiment_id": config["experiment_id"],
        "git_commit": "b" * 40, "git_dirty": False,
        "config_sha256": digest, "chip": "ESP32-S3", "esp_idf": "v5.4.4",
        "config": config["sensor_trust"],
        **{key: config[key] for key in (
            "sensor", "channel", "sensor_address", "sda_gpio", "scl_gpio",
            "sample_interval_ms", "baseline_samples", "repetitions",
            "recovery_samples", "seed")},
    }
    samples = []
    schedule = list(expected_schedule(config))
    for index, (phase, run, mode, active, fault) in enumerate(schedule):
        raw = 25.0
        start_index = index
        while start_index > 0 and schedule[start_index - 1][2] == mode:
            start_index -= 1
        value = {
            "PASS": raw, "FREEZE": 25.0, "SPIKE": 35.0,
            "DRIFT": raw + 0.05 * (index - start_index),
            "DROP": raw, "OUT_OF_RANGE": 150.0, "OFFSET": 29.0,
        }[mode]
        valid = mode != "DROP"
        samples.append({
            "index": index, "timestamp_ms": (index + 1) * 1000,
            "sensor": config["sensor"], "channel": config["channel"],
            "phase": phase, "run": run, "raw_value": raw,
            "raw_valid": True, "injected_value": value if valid else None,
            "injected_valid": valid, "injection": mode,
            "fault_active": active, "expected_fault": fault,
            "detected_flags": BITS.get(fault, 0) if active else 0,
            "health_score": 100, "state": "HEALTHY",
        })
    end = {"status": "complete", "samples": len(samples),
           "end_monotonic_ms": samples[-1]["timestamp_ms"] + 1}
    return config, digest, begin, samples, end


def render(begin, samples, end):
    return "\n".join(["boot text", "ST_BEGIN " + json.dumps(begin),
                      *("ST_SAMPLE " + json.dumps(s) for s in samples),
                      "ST_END " + json.dumps(end)]) + "\n"


def test_valid_run_and_metrics(fake_run):
    config, digest, begin, samples, end = fake_run
    parsed_begin, parsed = parse_log(render(begin, samples, end), config, digest)
    assert parsed_begin["git_dirty"] is False
    assert parsed == samples
    rows = episodes(parsed, config)
    assert len(rows) == len(config["injection"])
    assert all(row["detection_latency_ms"] == 0 for row in rows if row["fault"] != "OFFSET")
    assert all(row["recovery_latency_ms"] == 0 for row in rows if row["fault"] != "OFFSET")
    assert next(row for row in rows if row["fault"] == "OFFSET")["detected"] is None
    assert clean_baseline(parsed, config)["false_positive_rate"] == 0
    assert all(row["episode_recall"] == 1 for row in metrics(parsed, rows))


def test_miss_false_alarm_and_real_read_failure_are_distinct(fake_run):
    config, digest, begin, samples, end = copy.deepcopy(fake_run)
    samples[0]["detected_flags"] = BITS["STUCK"]  # clean false alarm
    samples[1].update(raw_valid=False, raw_value=None, injected_valid=False,
                      injected_value=None, expected_fault="MISSING",
                      detected_flags=BITS["MISSING"])  # actual read failure
    for sample in samples:
        if sample["injection"] == "FREEZE":
            sample["detected_flags"] = 0  # injected fault missed
    _, parsed = parse_log(render(begin, samples, end), config, digest)
    baseline = clean_baseline(parsed, config)
    assert baseline["false_positive_samples"] == 1
    assert baseline["false_positive_by_fault"]["MISSING"] == 0
    stuck = next(row for row in metrics(parsed, episodes(parsed, config))
                 if row["fault"] == "STUCK")
    assert stuck["episode_recall"] == 0
    assert stuck["median_detection_latency_ms"] is None
    assert stuck["sample_fn"] == 2


def test_rejects_fabricated_injected_value(fake_run):
    config, digest, begin, samples, end = copy.deepcopy(fake_run)
    spike = next(row for row in samples if row["injection"] == "SPIKE")
    spike["injected_value"] = 24.0
    with pytest.raises(EvidenceError, match="injection value mismatch"):
        parse_log(render(begin, samples, end), config, digest)


@pytest.mark.parametrize("mutation,reason", [
    ("missing_begin", "ST_SAMPLE outside run"),
    ("missing_end", "missing ST_END"),
    ("mixed_commit", "duplicate or late ST_BEGIN"),
    ("dirty", "dirty formal run"),
    ("wrong_hash", "wrong config hash"),
    ("unknown_fault", "ground truth token"),
    ("negative_time", "non-increasing timestamp"),
    ("missing_sample", "wrong sample count"),
])
def test_rejects_invalid_evidence(fake_run, mutation, reason):
    config, digest, begin, samples, end = copy.deepcopy(fake_run)
    if mutation == "missing_begin":
        text = "\n".join("ST_SAMPLE " + json.dumps(s) for s in samples)
    elif mutation == "missing_end":
        text = "ST_BEGIN " + json.dumps(begin) + "\n"
    elif mutation == "mixed_commit":
        text = render(begin, samples, end) + "ST_BEGIN " + json.dumps({**begin, "git_commit": "c" * 40})
    else:
        if mutation == "dirty":
            begin["git_dirty"] = True
        elif mutation == "wrong_hash":
            begin["config_sha256"] = "0" * 64
        elif mutation == "unknown_fault":
            samples[2]["expected_fault"] = "UNKNOWN"
        elif mutation == "negative_time":
            samples[3]["timestamp_ms"] = samples[2]["timestamp_ms"] - 1
        elif mutation == "missing_sample":
            samples.pop()
            end["samples"] -= 1
        text = render(begin, samples, end)
    with pytest.raises(EvidenceError, match=reason):
        parse_log(text, config, digest)


def test_injector_compiles_and_runs_on_host(tmp_path):
    compiler = shutil.which("cc")
    assert compiler
    binary = tmp_path / "test_injector"
    built = subprocess.run([
        compiler, "-std=c11", "-Wall", "-Wextra", "-Werror",
        "-I", str(ROOT / "core"), "-I", str(ROOT / "firmware" / "main"),
        str(ROOT / "firmware" / "main" / "fault_injector.c"),
        str(ROOT / "tests" / "test_injector.c"), "-o", str(binary), "-lm",
    ], capture_output=True, text=True)
    assert built.returncode == 0, built.stderr
    ran = subprocess.run([str(binary)], capture_output=True, text=True)
    assert ran.returncode == 0, ran.stderr


def test_v01_bits_and_config_unchanged():
    header = (ROOT / "core" / "sensor_trust.h").read_text()
    for name, bit in BITS.items():
        assert f"#define FAULT_{name} (1u << {bit.bit_length() - 1})" in header
    config, _ = load_config(CONFIG)
    assert config["sensor_trust"] == {
        "min_value": -40.0, "max_value": 85.0, "stuck_epsilon": 0.01,
        "stuck_window": 20, "spike_threshold": 3.0, "drift_window": 24,
        "drift_threshold": 0.01, "missing_limit": 3,
    }
