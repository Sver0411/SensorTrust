"""Validate immutable ST_* evidence and derive observable-label metrics.

Expected labels are derived from injected signal values, validity, episode
schedule and the published v0.1 detector definitions. Detector output is only
read later for comparison; it never participates in label generation.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "firmware" / "experiment_config.json"
BITS = {"RANGE": 1, "STUCK": 2, "SPIKE": 4, "DRIFT": 8, "MISSING": 16}
MODES = {
    "FREEZE": "STUCK", "SPIKE": "SPIKE", "DRIFT": "DRIFT",
    "DROP": "MISSING", "OUT_OF_RANGE": "RANGE", "OFFSET": "NONE",
}
PLAN = tuple(MODES)
SAMPLE_COLUMNS = (
    "experiment_id", "run", "sample_index", "timestamp_ms", "sensor", "channel",
    "phase", "injection_mode", "episode_mode", "fault_active", "causal_target",
    "expected_observable_flags", "raw_value", "raw_valid", "injected_value",
    "injected_valid", "detected_flags", "active_flags", "health_score", "state",
)
EPISODE_COLUMNS = (
    "experiment_id", "sensor", "channel", "fault", "run", "fault_start_ms",
    "fault_end_ms", "detected", "first_detection_ms", "detection_latency_ms",
    "detection_latency_samples", "cleared", "first_clear_ms",
    "recovery_latency_ms", "recovery_latency_samples", "observable_fault_present",
    "detector_flag_seen",
)
CROSS_COLUMNS = (
    "injection_mode", "phase", "detected_fault", "expected_observable_samples",
    "detected_samples", "true_positive_samples", "false_positive_samples",
    "false_negative_samples", "previous_SPIKE_FP_samples",
)
METRIC_COLUMNS = (
    "fault", "episodes", "episodes_detected", "episode_recall",
    "median_detection_latency_ms", "p95_detection_latency_ms",
    "median_recovery_latency_ms", "sample_tp", "sample_fp", "sample_tn",
    "sample_fn", "precision", "recall", "f1", "false_positive_rate",
)


class EvidenceError(ValueError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise EvidenceError(message)


def load_config(path: Path = CONFIG) -> tuple[dict, str]:
    data = path.read_bytes()
    config = json.loads(data)
    return config, hashlib.sha256(data).hexdigest()


def _serial_records(text: str) -> tuple[dict, list[dict], dict]:
    begin = end = None
    samples: list[dict] = []
    for line_no, line in enumerate(text.splitlines(), 1):
        match = re.match(r"^(ST_BEGIN|ST_SAMPLE|ST_END|ST_ERROR) (\{.*\})$", line.strip())
        if not match:
            require(not line.strip().startswith("ST_"), f"malformed protocol line {line_no}")
            continue  # boot and console noise may precede the protocol
        kind, payload = match.groups()
        try:
            record = json.loads(payload, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError, json.JSONDecodeError) as exc:
            raise EvidenceError(f"invalid JSON on line {line_no}") from exc
        require(isinstance(record, dict), f"non-object record on line {line_no}")
        require(kind != "ST_ERROR", f"device reported error on line {line_no}")
        if kind == "ST_BEGIN":
            require(begin is None and not samples and end is None, "duplicate or late ST_BEGIN")
            begin = record
        elif kind == "ST_SAMPLE":
            require(begin is not None and end is None, "ST_SAMPLE outside run")
            samples.append(record)
        else:
            require(begin is not None and end is None, "duplicate or orphan ST_END")
            end = record
    require(begin is not None, "missing ST_BEGIN")
    require(end is not None, "missing ST_END")
    return begin, samples, end


def expected_schedule(config: dict):
    for _ in range(config["baseline_samples"]):
        yield "BASELINE", 0, "PASS", "NONE", False, "NONE"
    for mode in PLAN:
        for run in range(1, config["repetitions"] + 1):
            for _ in range(config["injection"][mode]["duration_samples"]):
                yield "INJECTION", run, mode, mode, True, MODES[mode]
            for _ in range(config["recovery_samples"]):
                yield "RECOVERY", run, "PASS", mode, False, "NONE"


def parse_log(text: str, config: dict, config_hash: str,
              *, allow_dirty: bool = False) -> tuple[dict, list[dict]]:
    begin, samples, end = _serial_records(text)
    require(begin.get("schema") == 1, "unsupported schema")
    for key in ("experiment_id", "sensor", "channel", "sensor_address",
                "sda_gpio", "scl_gpio", "sample_interval_ms", "baseline_samples",
                "repetitions", "recovery_samples", "seed"):
        require(begin.get(key) == config[key], f"BEGIN {key} differs from config")
    require(begin.get("config") == config["sensor_trust"], "BEGIN detector config mismatch")
    require(begin.get("config_sha256") == config_hash, "wrong config hash")
    require(re.fullmatch(r"[0-9a-f]{40}", str(begin.get("git_commit", ""))) is not None,
            "invalid git commit")
    require(isinstance(begin.get("git_dirty"), bool), "git_dirty must be boolean")
    require(allow_dirty or begin["git_dirty"] is False, "dirty formal run rejected")
    require(begin.get("chip") == "ESP32-S3", "wrong chip")
    require(isinstance(begin.get("esp_idf"), str) and begin["esp_idf"], "missing ESP-IDF")
    require(end.get("status") == "complete", "incomplete hardware run")
    require(end.get("samples") == len(samples), "END count mismatch")

    schedule = list(expected_schedule(config))
    require(len(samples) == len(schedule), "wrong sample count or incomplete schedule")
    previous_ts = -1
    last_valid_raw = None
    freeze_value = None
    injection_start_ms = None
    previous_mode = "PASS"
    for index, (sample, expected) in enumerate(zip(samples, schedule)):
        phase, run, mode, episode_mode, active, target = expected
        require(type(sample.get("index")) is int and sample["index"] == index,
                f"non-contiguous sample index at {index}")
        ts = sample.get("timestamp_ms")
        require(type(ts) is int and ts > previous_ts, f"non-increasing timestamp at {index}")
        previous_ts = ts
        for key in ("sensor", "channel"):
            require(sample.get(key) == config[key], f"sample {index} wrong {key}")
        require((sample.get("phase"), sample.get("run"), sample.get("injection"),
                 sample.get("fault_active")) == (phase, run, mode, active),
                f"sample {index} deviates from injection schedule")
        sample["episode_mode"] = episode_mode
        for valid_key, value_key in (("raw_valid", "raw_value"),
                                     ("injected_valid", "injected_value")):
            valid = sample.get(valid_key)
            value = sample.get(value_key)
            require(type(valid) is bool, f"sample {index} bad {valid_key}")
            require((type(value) in (int, float) and math.isfinite(value)) if valid else
                    value is None, f"sample {index} bad {value_key}")
        actual_target = target if sample["raw_valid"] else "MISSING"
        require(sample.get("expected_fault") == actual_target,
                f"sample {index} wrong ground truth token")
        if mode == "PASS" and sample["raw_valid"]:
            require(sample["injected_valid"] and
                    sample["raw_value"] == sample["injected_value"],
                    f"sample {index} PASS changed payload")
        if mode == "DROP":
            require(not sample["injected_valid"], f"sample {index} DROP stayed valid")
        if mode != "PASS" and mode != previous_mode:
            injection_start_ms = ts
            freeze_value = last_valid_raw
        if mode != "PASS" and mode != "DROP":
            if not sample["raw_valid"]:
                require(not sample["injected_valid"],
                        f"sample {index} invented data after physical read failure")
            else:
                raw = sample["raw_value"]
                settings = config["injection"][mode]
                if mode == "FREEZE":
                    require(freeze_value is not None, "FREEZE lacks prior real reading")
                    wanted = freeze_value
                elif mode == "SPIKE":
                    wanted = raw + settings["amplitude"]
                elif mode == "OFFSET":
                    wanted = raw + settings["offset"]
                elif mode == "DRIFT":
                    wanted = raw + settings["rate_per_second"] * (ts - injection_start_ms) / 1000
                else:
                    wanted = settings["value"]
                require(sample["injected_valid"] and
                        abs(sample["injected_value"] - wanted) <= 0.005,
                        f"sample {index} injection value mismatch")
        if sample["raw_valid"]:
            last_valid_raw = sample["raw_value"]
        previous_mode = mode
        flags = sample.get("detected_flags")
        require(type(flags) is int and 0 <= flags <= 31, f"sample {index} unknown flags")
        score = sample.get("health_score")
        require(type(score) is int and 0 <= score <= 100, f"sample {index} bad score")
        require(sample.get("state") in ("HEALTHY", "DEGRADED", "FAULT"),
                f"sample {index} bad state")
    require(end.get("end_monotonic_ms", previous_ts) >= previous_ts,
            "END timestamp precedes samples")
    if config["baseline_samples"] >= 1801:
        elapsed = samples[config["baseline_samples"] - 1]["timestamp_ms"] - samples[0]["timestamp_ms"]
        require(elapsed >= 1_800_000, "clean baseline shorter than 30 minutes")
    for sample, expected_mask in zip(samples, observable_flags(samples, config)):
        sample["injection_mode"] = sample["injection"]
        sample["causal_target"] = sample["expected_fault"]
        sample["_expected_observable_flags"] = expected_mask
        sample["expected_observable_flags"] = flag_tokens(expected_mask)
    return begin, samples


def observable_flags(samples: list[dict], config: dict) -> list[int]:
    """Generate multi-label observable truth without consulting detector output.

    Injection intervals define STUCK/DRIFT episodes and invalid samples define
    MISSING from the first unavailable sample. RANGE is derived from the
    injected value. SPIKE labels are derived from adjacent valid signal values:
    each over-threshold jump is observable, and a following return within the
    documented reference threshold is also an observable SPIKE sample.
    """
    config = config["sensor_trust"]
    labels: list[int] = []
    last_valid_value = None
    pending_spike_reference = None
    for sample in samples:
        current = BITS.copy()
        flags = 0
        valid = sample["injected_valid"]
        if not valid:
            flags |= current["MISSING"]
            labels.append(flags)
            # Invalid readings carry no value and do not clear spike history.
            continue

        value = float(sample["injected_value"])
        if value < config["min_value"] or value > config["max_value"]:
            flags |= current["RANGE"]

        # Preserve the episode's observable temporal cause for window faults.
        if sample["phase"] == "INJECTION" and sample["episode_mode"] == "FREEZE":
            flags |= current["STUCK"]
        if sample["phase"] == "INJECTION" and sample["episode_mode"] == "DRIFT":
            flags |= current["DRIFT"]

        returned = False
        if pending_spike_reference is not None:
            returned = abs(value - pending_spike_reference) <= config["spike_threshold"]
            pending_spike_reference = None
            if returned:
                flags |= current["SPIKE"]
        if not returned and last_valid_value is not None:
            if abs(value - last_valid_value) > config["spike_threshold"]:
                flags |= current["SPIKE"]
                pending_spike_reference = last_valid_value

        last_valid_value = value
        labels.append(flags)
    return labels


def flag_tokens(flags: int) -> str:
    names = [name for name, bit in BITS.items() if flags & bit]
    return "|".join(names) if names else "NONE"


def cross_fault_detections(samples: list[dict]) -> list[dict]:
    """Count expected labels and emitted bits by causal episode and phase."""
    grouped: dict[tuple[str, str, str], dict[str, int]] = {}
    for sample in samples:
        mode = sample["episode_mode"]
        if mode == "NONE":
            continue
        expected = sample["_expected_observable_flags"]
        detected = sample["detected_flags"]
        for fault, bit in BITS.items():
            if not (expected & bit or detected & bit):
                continue
            key = (mode, sample["phase"], fault)
            count = grouped.setdefault(key, {"expected": 0, "detected": 0,
                                             "tp": 0, "fp": 0, "fn": 0,
                                             "previous_spike_fp": 0})
            truth = bool(expected & bit)
            prediction = bool(detected & bit)
            count["expected"] += int(truth)
            count["detected"] += int(prediction)
            count["tp"] += int(truth and prediction)
            count["fp"] += int(not truth and prediction)
            count["fn"] += int(truth and not prediction)
            causal = sample.get("causal_target", sample.get("expected_fault", "NONE"))
            previous_spike_fp = (fault == "SPIKE" and prediction and causal != "SPIKE" and
                                 (not sample["fault_active"] or causal == "NONE"))
            count["previous_spike_fp"] += int(previous_spike_fp)
    return [{
        "injection_mode": mode, "phase": phase, "detected_fault": fault,
        "expected_observable_samples": counts["expected"],
        "detected_samples": counts["detected"],
        "true_positive_samples": counts["tp"],
        "false_positive_samples": counts["fp"],
        "false_negative_samples": counts["fn"],
        "previous_SPIKE_FP_samples": counts["previous_spike_fp"],
    } for (mode, phase, fault), counts in sorted(grouped.items())]


def _ratio(numerator: int, denominator: int):
    return numerator / denominator if denominator else None


def _percentile(values: list[int], percent: float):
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percent
    low, high = math.floor(position), math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def episodes(samples: list[dict], config: dict) -> list[dict]:
    rows = []
    offset = config["baseline_samples"]
    for mode in PLAN:
        duration = config["injection"][mode]["duration_samples"]
        for run in range(1, config["repetitions"] + 1):
            active = samples[offset:offset + duration]
            recovery = samples[offset + duration:offset + duration + config["recovery_samples"]]
            target = MODES[mode]
            bit = BITS.get(target, 0)
            first = next((row for row in active if
                          row["_expected_observable_flags"] & bit and
                          row["detected_flags"] & bit), None) if bit else None
            cleared = next((row for row in recovery if not row["detected_flags"] & bit),
                           None) if first is not None else None
            start_ms = active[0]["timestamp_ms"]
            end_ms = recovery[0]["timestamp_ms"]
            rows.append({
                "experiment_id": config["experiment_id"], "sensor": config["sensor"],
                "channel": config["channel"], "fault": target if bit else "OFFSET",
                "run": run, "fault_start_ms": start_ms, "fault_end_ms": end_ms,
                "detected": first is not None if bit else None,
                "first_detection_ms": first["timestamp_ms"] if first else None,
                "detection_latency_ms": first["timestamp_ms"] - start_ms if first else None,
                "detection_latency_samples": first["index"] - active[0]["index"] if first else None,
                "cleared": cleared is not None if bit and first is not None else None,
                "first_clear_ms": cleared["timestamp_ms"] if cleared else None,
                "recovery_latency_ms": cleared["timestamp_ms"] - end_ms if cleared else None,
                "recovery_latency_samples": cleared["index"] - recovery[0]["index"] if cleared else None,
                "observable_fault_present": any(row["_expected_observable_flags"]
                                                 for row in active),
                "detector_flag_seen": any(row["detected_flags"] for row in active),
            })
            offset += duration + config["recovery_samples"]
    return rows


def metrics(samples: list[dict], episode_rows: list[dict]) -> list[dict]:
    result = []
    for fault, bit in BITS.items():
        counts = {"tp": 0, "fp": 0, "tn": 0, "fn": 0}
        for sample in samples:
            is_true = bool(sample["_expected_observable_flags"] & bit)
            detected = bool(sample["detected_flags"] & bit)
            key = "tp" if is_true and detected else "fn" if is_true else \
                  "fp" if detected else "tn"
            counts[key] += 1
        selected = [row for row in episode_rows if row["fault"] == fault]
        detected_rows = [row for row in selected if row["detected"]]
        detection = [row["detection_latency_ms"] for row in detected_rows]
        recovery = [row["recovery_latency_ms"] for row in selected
                    if row["recovery_latency_ms"] is not None]
        precision = _ratio(counts["tp"], counts["tp"] + counts["fp"])
        recall = _ratio(counts["tp"], counts["tp"] + counts["fn"])
        result.append({
            "fault": fault, "episodes": len(selected),
            "episodes_detected": len(detected_rows),
            "episode_recall": _ratio(len(detected_rows), len(selected)),
            "median_detection_latency_ms": statistics.median(detection) if detection else None,
            "p95_detection_latency_ms": _percentile(detection, 0.95),
            "median_recovery_latency_ms": statistics.median(recovery) if recovery else None,
            "sample_tp": counts["tp"], "sample_fp": counts["fp"],
            "sample_tn": counts["tn"], "sample_fn": counts["fn"],
            "precision": precision, "recall": recall,
            "f1": (2 * precision * recall / (precision + recall))
                  if precision is not None and recall is not None and precision + recall else None,
            "false_positive_rate": _ratio(counts["fp"], counts["fp"] + counts["tn"]),
        })
    return result


def clean_baseline(samples: list[dict], config: dict) -> dict:
    clean = samples[:config["baseline_samples"]]
    false_counts = {name: sum(bool(row["detected_flags"] & bit) and
                              not row["_expected_observable_flags"] & bit for row in clean)
                    for name, bit in BITS.items()}
    flagged = sum(any(row["detected_flags"] & bit and
                      not row["_expected_observable_flags"] & bit
                      for name, bit in BITS.items()) for row in clean)
    return {
        "duration_ms": clean[-1]["timestamp_ms"] - clean[0]["timestamp_ms"],
        "samples": len(clean), "raw_read_failures": sum(not row["raw_valid"] for row in clean),
        "false_positive_samples": flagged,
        "false_positive_rate": _ratio(flagged, len(clean)),
        "false_positive_by_fault": false_counts,
    }


def _write_csv(path: Path, columns: tuple[str, ...], rows: list[dict],
               *, na: str = "") -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: na if row.get(key) is None else row[key] for key in columns})


def evaluate(log_path: Path, out_dir: Path, config_path: Path = CONFIG,
             *, allow_dirty: bool = False) -> dict:
    config, digest = load_config(config_path)
    begin, samples = parse_log(log_path.read_text(encoding="utf-8"), config, digest,
                               allow_dirty=allow_dirty)
    episode_rows = episodes(samples, config)
    metric_rows = metrics(samples, episode_rows)
    baseline = clean_baseline(samples, config)
    out_dir.mkdir(parents=True, exist_ok=True)
    sample_rows = [{
        "experiment_id": config["experiment_id"], "run": row["run"],
        "sample_index": row["index"], "timestamp_ms": row["timestamp_ms"],
        "sensor": row["sensor"], "channel": row["channel"], "phase": row["phase"],
        "injection_mode": row["injection_mode"], "episode_mode": row["episode_mode"],
        "fault_active": row["fault_active"], "causal_target": row["causal_target"],
        "expected_observable_flags": row["expected_observable_flags"],
        "raw_value": row["raw_value"], "raw_valid": row["raw_valid"],
        "injected_value": row["injected_value"], "injected_valid": row["injected_valid"],
        "detected_flags": row["detected_flags"], "active_flags": None,
        "health_score": row["health_score"], "state": row["state"],
    } for row in samples]
    _write_csv(out_dir / "sample_results.csv", SAMPLE_COLUMNS, sample_rows)
    _write_csv(out_dir / "fault_episodes.csv", EPISODE_COLUMNS, episode_rows, na="N/A")
    _write_csv(out_dir / "metrics_per_fault.csv", METRIC_COLUMNS, metric_rows, na="N/A")
    _write_csv(out_dir / "metrics_per_channel.csv",
               ("sensor", "channel", "duration_ms", "samples", "raw_read_failures",
                "false_positive_samples", "false_positive_rate"),
               [{"sensor": config["sensor"], "channel": config["channel"],
                 **{key: baseline[key] for key in ("duration_ms", "samples",
                                               "raw_read_failures", "false_positive_samples",
                                               "false_positive_rate")}}])
    _write_csv(out_dir / "cross_fault_detections.csv", CROSS_COLUMNS,
               cross_fault_detections(samples))
    (out_dir / "clean_baseline.json").write_text(json.dumps(baseline, indent=2) + "\n")
    metadata = {**begin, "injection_config": config["injection"],
                "raw_log": str(log_path.relative_to(ROOT)) if log_path.is_relative_to(ROOT)
                else str(log_path),
                "raw_log_sha256": hashlib.sha256(log_path.read_bytes()).hexdigest()}
    sidecar_path = log_path.with_suffix(".capture.json")
    if sidecar_path.is_file():
        capture = json.loads(sidecar_path.read_text(encoding="utf-8"))
        require(isinstance(capture, dict), "capture sidecar must be an object")
        metadata["capture"] = {key: capture[key] for key in
                               ("capture_started_utc", "capture_finished_utc", "baud")
                               if key in capture}
        if "toolchain" in capture:
            metadata["toolchain"] = capture["toolchain"]
    hardware_probe_path = out_dir / "hardware_probe.json"
    if hardware_probe_path.is_file():
        probe = json.loads(hardware_probe_path.read_text(encoding="utf-8"))
        metadata["hardware_probe"] = probe
        reported_flash = metadata.pop("flash_bytes", None)
        reported_psram = metadata.pop("psram_bytes", None)
        metadata["flash_physical_bytes"] = probe["flash"]["physical_bytes"]
        metadata["flash_configured_bytes"] = probe["flash"]["configured_bytes"]
        metadata["flash_image_header_bytes"] = probe["flash"]["image_header_bytes"]
        metadata["flash_firmware_reported_bytes"] = reported_flash
        metadata["psram_physical_bytes"] = probe["chip"]["psram_physical_capacity_bytes"]
        metadata["psram_physical_capacity_basis"] = probe["chip"]["psram_capacity_basis"]
        metadata["psram_enabled"] = probe["psram"]["enabled_in_firmware"]
        metadata["psram_runtime_available_bytes"] = reported_psram
    (out_dir / "hardware_metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    return {"baseline": baseline, "metrics": metric_rows, "episodes": episode_rows}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path)
    parser.add_argument("--config", type=Path, default=CONFIG)
    parser.add_argument("--out", type=Path, default=ROOT / "results" / "v0.2")
    parser.add_argument("--allow-dirty", action="store_true")
    args = parser.parse_args()
    if args.allow_dirty:
        require(args.out.resolve().is_relative_to((ROOT / "results" / "experimental").resolve()),
                "dirty results must go under results/experimental")
    else:
        require(args.out.resolve().is_relative_to((ROOT / "results" / "v0.2").resolve()),
                "formal results must go under results/v0.2")
    result = evaluate(args.log, args.out, args.config, allow_dirty=args.allow_dirty)
    print(json.dumps(result["baseline"], indent=2))
    for row in result["metrics"]:
        print(row["fault"], row["episodes_detected"], "/", row["episodes"])


if __name__ == "__main__":
    main()
