# SensorTrust

SensorTrust is a small embedded sensor-health checker that detects stuck values, impossible ranges, sudden spikes, sustained drift and missing data, then converts those signals into a simple health state and score.

It answers one question about one sensor channel:

> Can the current reading be trusted?

```text
sensor is producing data
            !=
the data is trustworthy

SensorTrust checks the data on the node
            ->
fault flags (bitmask) + health_score (0..100) + state (HEALTHY / DEGRADED / FAULT)
```

What it does **not** do: decide when to sample, how often to collect, or when to upload. That belongs to the sampling/scheduling side of the node. SensorTrust only judges the readings it is given.

## What it does

Input: a stream of samples from one channel.

```c
typedef struct {
    float    value;
    bool     valid;         /* false = read failure, timeout, stale register, ... */
    uint64_t timestamp_ms;
} sensor_sample_t;
```

Output: for every sample, one small result.

```c
typedef struct {
    int                   health_score;  /* 0..100, severity */
    sensor_health_state_t state;         /* HEALTHY / DEGRADED / FAULT */
    uint32_t              fault_flags;   /* bitmask of FAULT_* */
} sensor_health_result_t;
```

The core is portable C11: no dynamic memory, no RTOS, no Wi-Fi, no MQTT, no
hardware headers. The same `core/sensor_trust.c` runs on the ESP32 and on a PC,
which is what makes the host tests and the scenario runs below meaningful.

## Faults it detects

v0.1 detects exactly five things. Nothing else.

| Flag | Bit | What it looks for | Default trigger |
| --- | --- | --- | --- |
| `FAULT_RANGE` | `1 << 0` | a physically impossible value | `value < min_value` or `value > max_value` |
| `FAULT_STUCK` | `1 << 1` | a channel that stopped moving | `max - min < stuck_epsilon` over the last `stuck_window` samples |
| `FAULT_SPIKE` | `1 << 2` | a sudden jump | `abs(value - previous valid value) > spike_threshold` |
| `FAULT_DRIFT` | `1 << 3` | a sustained one-directional trend | `abs(slope) > drift_threshold` for `drift_window` consecutive samples |
| `FAULT_MISSING` | `1 << 4` | no usable reading | `missing_limit` consecutive invalid samples |

Notes on the three that need care:

- **STUCK** is not "two equal readings". A stable environment is normal, so the
  check is the spread of a whole window: the reading is stuck when the whole
  window moves less than `stuck_epsilon`.
- **SPIKE** confirms itself. A jump away from the previous value is reported;
  if the next reading comes back near the value it jumped from, the excursion
  is counted as a confirmed spike. A jump that never comes back (a real level
  change) is reported once and then accepted as the new level.
- **DRIFT** is reported as *suspected* drift, never as proof of a broken
  sensor: a real environment change looks the same to one channel. To avoid
  calling every transient a drift, the trend has to hold for `drift_window`
  consecutive samples, so a ramp must last roughly two windows before it is
  flagged.

## How it works

Every channel has its own configuration and its own context.

```c
typedef struct {
    float min_value;
    float max_value;
    float stuck_epsilon;
    int   stuck_window;
    float spike_threshold;
    int   drift_window;
    float drift_threshold;
    int   missing_limit;
} sensor_trust_config_t;
```

Nothing about a specific sensor is hard-coded in the detection logic: the
ranges and thresholds above are the only place a physical channel is described,
so the same core covers a temperature channel (-40..85 C) and a humidity
channel (0..100 %) without changing a line of the algorithm.

Implementation facts:

- the history is a fixed ring buffer of the most recent **valid** values,
  `SENSOR_TRUST_MAX_WINDOW` (64) samples, statically allocated;
- `sensor_trust_update()` is `O(window)` and allocation-free, so it is safe to
  call on every reading;
- the windows are counted in **samples, not seconds**. The caller's sampling
  interval defines the time scale (1 Hz everywhere in this repository);
- an invalid sample reports `FAULT_MISSING` only: it carries no value, so it
  neither triggers nor clears the value-based detectors, and it restarts the
  drift counter;
- the detectors are independent, so one sample can carry several flags (a
  reading that jumps into impossible territory is both `RANGE` and `SPIKE`).

### Health score

The score starts at 100 and subtracts one penalty per active fault, clamped to
0..100.

| Fault | Penalty | Score if it is the only fault | State alone |
| --- | ---: | ---: | --- |
| `FAULT_RANGE` | 55 | 45 | FAULT |
| `FAULT_MISSING` | 55 | 45 | FAULT |
| `FAULT_STUCK` | 35 | 65 | DEGRADED |
| `FAULT_SPIKE` | 25 | 75 | DEGRADED |
| `FAULT_DRIFT` | 25 | 75 | DEGRADED |

The weights are chosen so that one confirmed fault never leaves a channel in
`HEALTHY`. They are compile-time overridable (`SENSOR_TRUST_PENALTY_*`).

State mapping:

| Score | State |
| --- | --- |
| 80..100 | `HEALTHY` |
| 50..79 | `DEGRADED` |
| 0..49 | `FAULT` |

**The health score is a heuristic severity score, not a calibrated
probability.** `health_score = 72` does not mean "72 % likely to be healthy"; it
means "100 minus the penalties of the faults that are currently visible".

## Example

The ESP32 demo feeds 60 samples of one temperature channel (30 plausible
readings, one 36 C jump, then a register that stops moving) and prints one line
per sample plus a verdict:

```text
SensorTrust v0.1 demo, one temperature channel, 1 Hz
  1    25.000  HEALTHY   score=100  flags=NONE
...
 29    25.000  HEALTHY   score=100  flags=NONE
 30    24.950  HEALTHY   score=100  flags=NONE
 31    61.000  DEGRADED  score= 75  flags=SPIKE
 32    24.950  DEGRADED  score= 75  flags=SPIKE
 33    25.000  HEALTHY   score=100  flags=NONE
...
 59    25.213  HEALTHY   score=100  flags=NONE
 60    25.213  DEGRADED  score= 65  flags=STUCK

sensor state: DEGRADED
score: 65
flags: STUCK
```

Two things to read out of that output:

- samples 55..59 still say `HEALTHY / score=100` while the value is frozen. The
  detector only calls it `STUCK` once a full window shows no movement, which is
  what keeps a genuinely stable environment from being reported as a fault.
- the fault type is in `flags`. The state alone does not tell you *what* is
  wrong, and a transient fault can be gone again by the last sample.

That output comes from compiling `firmware/main/main.c` together with
`core/sensor_trust.c` on a PC - the file uses nothing but stdio and the core, so
the demo can be checked without hardware.

## Scenario results

Seven synthetic scenarios are replayed through the real core by
`simulator/run.py`. `Detected` is the union of the flags seen at any point in
the run, and `Health Score` is the lowest score reached during the run.

| Scenario | Expected | Detected | Health Score |
| --- | --- | --- | ---: |
| healthy | NONE | NONE | 100 |
| healthy_dynamic | NONE | NONE | 100 |
| stuck | STUCK | STUCK | 65 |
| spike | SPIKE | SPIKE | 75 |
| drift | DRIFT | DRIFT | 75 |
| out_of_range | RANGE | RANGE, STUCK, SPIKE | 10 |
| missing_data | MISSING | MISSING | 45 |

Machine-readable, with the end state of each run: `results/scenarios.csv`.

Two rows deserve a comment:

- `healthy_dynamic` is the false-positive control. The room warms up slowly and
  a draft adds a step that decays away: moving data, score stays 100. It is
  there to show that not every change is a fault.
- `out_of_range` reports three flags, not one, and that is honest rather than
  tidy: the humidity channel walks into an impossible value and stays there, so
  it is out of range (`RANGE`), the jump into it is a `SPIKE`, and the frozen
  garbage value is also `STUCK`. Score 10, state `FAULT`.

## ESP32 status

```text
Board:                  not connected
Sensor:                 none (no BME280 driver yet, by design)
Real sensor validation: Not measured yet
```

`firmware/` is an ESP-IDF project that only demonstrates the core: it builds
synthetic samples, feeds them to SensorTrust, prints the result. It does not
talk to a real sensor, and it does not include a driver for one - the core
takes generic `sensor_sample_t` values, so a driver can be added later without
touching the detection logic.

Build status:

```text
ESP-IDF:         v5.4.4, target esp32s3
Command:         idf.py set-target esp32s3 && idf.py build
Result:          Project build complete, 0 compiler warnings
App binary:      205,376 bytes (80% of the 1 MB app partition free)
Core in firmware: core/sensor_trust.c is compiled into the main component
```

The firmware and the host tests therefore run the same `core/sensor_trust.c`,
not two copies of it. What has *not* happened is running it on hardware: the
demo's printed output above comes from compiling the same
`firmware/main/main.c` on the host, because that file uses nothing but stdio
(and no sensor is attached).

## Run it

```bash
# 1. synthetic scenarios -> results/dataset/*.csv
python simulator/generate.py

# 2. replay them through the real C core -> results/scenarios.csv + the table above
python simulator/run.py

# 3. tests (C core + simulator)
python -m pytest tests/ -v

# 4. C core tests on their own, no Python needed
cc core/sensor_trust.c tests/test_core.c -o test_core -lm
./test_core

# 5. ESP-IDF demo (needs an installed ESP-IDF)
cd firmware
idf.py set-target esp32s3
idf.py build
```

`simulator/run.py` compiles `core/sensor_trust.c` together with
`simulator/replay_main.c` into a small host binary and feeds the datasets
through it, so the Python side never re-implements the detection logic. The
Python code only generates streams and aggregates what the C core reported.

## Project structure

```text
SensorTrust/
├── core/
│   ├── sensor_trust.c        # detection logic, portable C11
│   └── sensor_trust.h        # types, config, API
├── simulator/
│   ├── scenarios.py          # the 7 synthetic streams + channel configs
│   ├── generate.py           # writes results/dataset/*.csv
│   ├── replay_main.c         # host driver: feeds a dataset into the core
│   └── run.py                # builds the driver, replays, writes the report
├── firmware/
│   ├── CMakeLists.txt
│   ├── sdkconfig.defaults
│   └── main/
│       ├── CMakeLists.txt
│       └── main.c            # ESP32 demo (synthetic samples, prints verdicts)
├── results/
│   ├── dataset/*.csv         # generated scenario streams (self-describing)
│   └── scenarios.csv         # generated summary
├── tests/
│   ├── test_core.c           # 16 C tests for the core
│   └── test_simulator.py     # 7 Python tests for the simulator pipeline
├── README.md
└── LICENSE
```

## Limitations

- **One channel per context, no fusion.** SensorTrust does not compare channels
  and does not vote across nodes. A single channel cannot tell a real
  environmental change from a sensor fault, which is why drift is reported as
  *suspected*.
- **Synthetic data only.** All numbers in this README come from generated
  streams. Nothing here has been run against a real failing sensor yet.
- **A detected fault is a suspicion, not a verdict.** A frozen reading can be a
  genuinely constant environment; an impossible value can be a wiring problem
  rather than a dead sensor. SensorTrust reports data-quality suspicion, so the
  caller can decide what to do with the reading.
- **No gap/timeout detection.** The API is push-based: it sees the samples it is
  given. A sensor that stops sending entirely produces no sample at all, so the
  expected sampling interval has to be watched by the caller (that is the
  scheduler's job, not the health checker's).
- **Windows are in samples.** With a slower or faster sampling interval, the
  same `stuck_window` means a different duration. Set the thresholds for the
  rate you actually sample at.
- **The thresholds are hand-set defaults, not tuned.** They are not jointly
  optimised, and they are documented in one place (`scenarios.py` for the
  simulator, `sensor_trust_default_config()` for the generic default) so they
  can be reviewed and changed without hunting through the code.
- **Not implemented in v0.1:** real sensor validation, multi-sensor fusion, ML
  fault detection, cross-node voting, cloud diagnostics.

## License

MIT - see [LICENSE](LICENSE).
