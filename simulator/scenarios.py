"""Synthetic sensor streams for the SensorTrust v0.1 scenarios.

This module is the single source of truth for the simulator: a scenario
carries the stream, the channel configuration the core must use, and the fault
token it is expected to detect. `generate.py`, `run.py` and the tests all read
it, so a scenario cannot be described one way and replayed another way.

Streams are deterministic (fixed seeds), 1 Hz.

Expected tokens
    NONE    nothing suspicious
    RANGE   value outside the configured physical range
    STUCK   value frozen for a long window
    SPIKE   sudden jump away from the previous value
    DRIFT   sustained one-directional trend
    MISSING invalid samples (read failure / NaN)
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

SAMPLE_INTERVAL_MS = 1000

# A BME280-like temperature channel. Nothing about this sensor is hard-coded
# in the core; it is just the configuration this scenario hands to it.
TEMPERATURE_CONFIG: Dict[str, float] = {
    "min_value": -40.0,
    "max_value": 85.0,
    "stuck_epsilon": 0.01,
    "stuck_window": 20,
    "spike_threshold": 3.0,
    "drift_window": 24,
    "drift_threshold": 0.01,
    "missing_limit": 3,
}

# A humidity channel: different range, different sensitivity.
HUMIDITY_CONFIG: Dict[str, float] = {
    "min_value": 0.0,
    "max_value": 100.0,
    "stuck_epsilon": 0.05,
    "stuck_window": 20,
    "spike_threshold": 20.0,
    "drift_window": 24,
    "drift_threshold": 0.05,
    "missing_limit": 3,
}

Sample = Tuple[int, float, bool]  # timestamp_ms, value, valid


@dataclass(frozen=True)
class Scenario:
    name: str
    expected: str
    channel: str
    config: Dict[str, float]
    samples: List[Sample] = field(default_factory=list)

    @property
    def row_count(self) -> int:
        return len(self.samples)


def _stamp(pairs: List[Tuple[float, bool]]) -> List[Sample]:
    return [
        ((index + 1) * SAMPLE_INTERVAL_MS, value, valid)
        for index, (value, valid) in enumerate(pairs)
    ]


def _valid(values: List[float]) -> List[Tuple[float, bool]]:
    return [(value, True) for value in values]


# --------------------------------------------------------------------------
# scenario streams
# --------------------------------------------------------------------------


def _healthy() -> List[Sample]:
    """25 C, small normal noise. Nothing should be reported."""
    rng = random.Random(11)
    values = [25.0 + rng.uniform(-0.10, 0.10) for _ in range(200)]
    return _stamp(_valid(values))


def _healthy_dynamic() -> List[Sample]:
    """A real environmental change, and no fault.

    The room warms up slowly (about 0.23 C per minute, i.e. below the drift
    sensitivity of 0.01 C per sample), and a draft adds a 0.4 C step that
    decays away. Moving data, but nothing here is a sensor fault, so the
    health score must stay at 100. A faster environmental change would be
    reported as suspected drift -- see README, Limitations."""
    rng = random.Random(12)
    values = []
    for index in range(500):
        base = 25.0 + 0.0038 * min(index, 390)
        if index >= 400:
            base += 0.4 * math.exp(-(index - 400) / 80.0)
        values.append(base + rng.uniform(-0.10, 0.10))
    return _stamp(_valid(values))


def _stuck() -> List[Sample]:
    """Normal readings, then the register freezes on one value."""
    rng = random.Random(13)
    values = [25.0 + rng.uniform(-0.10, 0.10) for _ in range(100)]
    values.extend([25.213] * 60)
    return _stamp(_valid(values))


def _spike() -> List[Sample]:
    """One isolated 36 C excursion in the middle of a normal stream."""
    rng = random.Random(14)
    values = [25.0 + rng.uniform(-0.05, 0.05) for _ in range(100)]
    values[50] = 61.0
    return _stamp(_valid(values))


def _drift() -> List[Sample]:
    """A slow, sustained climb: +0.02 C per sample for 200 samples."""
    rng = random.Random(15)
    values = [25.0 + 0.02 * index + rng.uniform(-0.02, 0.02) for index in range(200)]
    return _stamp(_valid(values))


def _out_of_range() -> List[Sample]:
    """Humidity that walks into an impossible value and stays there:
    a read failure returning a frozen garbage value."""
    rng = random.Random(16)
    values = [45.0 + rng.uniform(-1.0, 1.0) for _ in range(60)]
    values.extend([135.0] * 30)
    return _stamp(_valid(values))


def _missing_data() -> List[Sample]:
    """The read fails for a while, then recovers."""
    rng = random.Random(17)
    pairs: List[Tuple[float, bool]] = []
    pairs.extend((25.0 + rng.uniform(-0.10, 0.10), True) for _ in range(60))
    pairs.extend((0.0, False) for _ in range(15))  # invalid samples
    pairs.extend((25.0 + rng.uniform(-0.10, 0.10), True) for _ in range(25))
    return _stamp(pairs)


def build_scenarios() -> List[Scenario]:
    return [
        Scenario("healthy", "NONE", "temperature_C", TEMPERATURE_CONFIG, _healthy()),
        Scenario(
            "healthy_dynamic",
            "NONE",
            "temperature_C",
            TEMPERATURE_CONFIG,
            _healthy_dynamic(),
        ),
        Scenario("stuck", "STUCK", "temperature_C", TEMPERATURE_CONFIG, _stuck()),
        Scenario("spike", "SPIKE", "temperature_C", TEMPERATURE_CONFIG, _spike()),
        Scenario("drift", "DRIFT", "temperature_C", TEMPERATURE_CONFIG, _drift()),
        Scenario(
            "out_of_range",
            "RANGE",
            "humidity_percent",
            HUMIDITY_CONFIG,
            _out_of_range(),
        ),
        Scenario(
            "missing_data",
            "MISSING",
            "temperature_C",
            TEMPERATURE_CONFIG,
            _missing_data(),
        ),
    ]


SCENARIOS: List[Scenario] = build_scenarios()
SCENARIO_NAMES: List[str] = [scenario.name for scenario in SCENARIOS]


def scenario_by_name(name: str) -> Scenario:
    for scenario in SCENARIOS:
        if scenario.name == name:
            return scenario
    raise KeyError(name)
