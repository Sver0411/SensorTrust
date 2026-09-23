"""Ground-truth labels describe observable signal semantics, not detector output."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hardware"))
from evaluate import BITS, CONFIG, cross_fault_detections, load_config, metrics, observable_flags


def sample(index, value, *, mode="PASS", episode_mode="NONE", phase="BASELINE",
           active=False, valid=True, detected=0):
    return {
        "index": index, "timestamp_ms": index * 1000,
        "injected_value": value if valid else None, "injected_valid": valid,
        "phase": phase, "episode_mode": episode_mode, "injection": mode,
        "fault_active": active, "detected_flags": detected,
    }


def attach_truth(rows, config):
    for row, mask in zip(rows, observable_flags(rows, config)):
        row["_expected_observable_flags"] = mask
    return rows


def test_spike_labels_cover_isolated_excursion_and_other_mode_boundaries():
    config, _ = load_config(CONFIG)
    rows = [
        sample(0, 25.0),
        sample(1, 150.0, mode="OUT_OF_RANGE", episode_mode="OUT_OF_RANGE",
               phase="INJECTION", active=True),
        sample(2, 150.0, mode="OUT_OF_RANGE", episode_mode="OUT_OF_RANGE",
               phase="INJECTION", active=True),
        sample(3, 25.0, episode_mode="OUT_OF_RANGE", phase="RECOVERY"),
        sample(4, 25.0),
        sample(5, 35.0, mode="SPIKE", episode_mode="SPIKE",
               phase="INJECTION", active=True),
        sample(6, 25.0, episode_mode="SPIKE", phase="RECOVERY"),
        sample(7, 25.0),
        sample(8, 29.0, mode="OFFSET", episode_mode="OFFSET",
               phase="INJECTION", active=True),
        sample(9, 29.1, mode="OFFSET", episode_mode="OFFSET",
               phase="INJECTION", active=True),
        sample(10, 25.0, episode_mode="OFFSET", phase="RECOVERY"),
    ]
    labels = observable_flags(rows, config)
    assert labels[1] == BITS["RANGE"] | BITS["SPIKE"]
    assert labels[2] == BITS["RANGE"]
    assert labels[3] == BITS["SPIKE"]  # return from OUT_OF_RANGE
    assert labels[5] == BITS["SPIKE"]  # excursion
    assert labels[6] == BITS["SPIKE"]  # confirmed return
    assert labels[8] == BITS["SPIKE"]  # OFFSET entry
    assert labels[9] == 0               # stable OFFSET plateau is not a fault type
    assert labels[10] == BITS["SPIKE"]  # OFFSET exit


def test_observable_truth_is_independent_of_detector_output():
    config, _ = load_config(CONFIG)
    rows = [sample(0, 25.0), sample(1, 150.0, mode="OUT_OF_RANGE",
                                    episode_mode="OUT_OF_RANGE", phase="INJECTION",
                                    active=True)]
    expected = observable_flags(rows, config)
    for row in rows:
        row["detected_flags"] = BITS["MISSING"]
    assert observable_flags(rows, config) == expected


def test_multi_label_metrics_count_range_and_spike_as_true_positives():
    config, _ = load_config(CONFIG)
    rows = [sample(0, 25.0),
            sample(1, 150.0, mode="OUT_OF_RANGE", episode_mode="OUT_OF_RANGE",
                   phase="INJECTION", active=True,
                   detected=BITS["RANGE"] | BITS["SPIKE"])]
    attach_truth(rows, config)
    values = {row["fault"]: row for row in metrics(rows, [])}
    for fault in ("RANGE", "SPIKE"):
        assert values[fault]["sample_tp"] == 1
        assert values[fault]["sample_fp"] == 0
        assert values[fault]["sample_fn"] == 0


def test_missing_confirmation_interval_remains_sample_level_false_negative():
    config, _ = load_config(CONFIG)
    rows = [sample(0, 25.0),
            sample(1, None, mode="DROP", episode_mode="DROP", phase="INJECTION",
                   active=True, valid=False),
            sample(2, None, mode="DROP", episode_mode="DROP", phase="INJECTION",
                   active=True, valid=False),
            sample(3, None, mode="DROP", episode_mode="DROP", phase="INJECTION",
                   active=True, valid=False, detected=BITS["MISSING"])]
    attach_truth(rows, config)
    missing = next(row for row in metrics(rows, []) if row["fault"] == "MISSING")
    assert missing["sample_tp"] == 1
    assert missing["sample_fn"] == 2


def test_cross_fault_table_attributes_recovery_edges_to_their_episode():
    config, _ = load_config(CONFIG)
    rows = [sample(0, 25.0),
            sample(1, 150.0, mode="OUT_OF_RANGE", episode_mode="OUT_OF_RANGE",
                   phase="INJECTION", active=True,
                   detected=BITS["RANGE"] | BITS["SPIKE"]),
            sample(2, 150.0, mode="OUT_OF_RANGE", episode_mode="OUT_OF_RANGE",
                   phase="INJECTION", active=True, detected=BITS["RANGE"]),
            sample(3, 25.0, episode_mode="OUT_OF_RANGE", phase="RECOVERY",
                   detected=BITS["SPIKE"])]
    attach_truth(rows, config)
    result = {(row["injection_mode"], row["phase"], row["detected_fault"]): row
              for row in cross_fault_detections(rows)}
    assert result[("OUT_OF_RANGE", "INJECTION", "SPIKE")]["true_positive_samples"] == 1
    assert result[("OUT_OF_RANGE", "RECOVERY", "SPIKE")]["true_positive_samples"] == 1
    assert result[("OUT_OF_RANGE", "INJECTION", "RANGE")]["true_positive_samples"] == 2
