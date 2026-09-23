# SensorTrust v0.2

[English](README.md) | [简体中文](README.zh-CN.md)

SensorTrust is a portable C11 sensor-health detector. Its five v0.1 fault semantics are frozen; v0.2 adds a real ESP32-S3/SHT30 experiment, deterministic software fault injection, and an auditable measurement pipeline.

**Physical link verified:** an ESP32-S3 read a real SHT30 over I²C and emitted a valid sample. The full clean baseline and fault experiment are still being collected; no detection rate is claimed until `results/v0.2/` contains a complete clean-tree log and generated tables. BH1750 has not been tested.

Hardware validation uses an accelerated 1 Hz laboratory sampling schedule. It is not the deployment interval. The health score is a heuristic severity score, not a calibrated probability; a detected fault means suspicious data, not proof of a broken sensor.

The [hardware protocol and experiment procedure](hardware/README.md) explain exactly how raw readings, injected readings, ground truth, and metrics are recorded.

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
stdio, no hardware headers. The same `core/sensor_trust.c` runs on the ESP32
and on a PC, which is what makes the host tests and the scenario runs below
meaningful.

## Faults it detects

v0.1 detects exactly five things. Nothing else.

| Flag | Bit | What it looks for | Default trigger |
| --- | --- | --- | --- |
| `FAULT_RANGE` | `1 << 0` | a physically impossible value | `value < min_value` or `value > max_value` |
| `FAULT_STUCK` | `1 << 1` | a channel that stopped moving | `max - min < stuck_epsilon` over the last `stuck_window` samples |
| `FAULT_SPIKE` | `1 << 2` | a sudden jump | `abs(value - previous valid value) > spike_threshold` |
| `FAULT_DRIFT` | `1 << 3` | a sustained one-directional trend | `abs(slope) > drift_threshold` in value units **per second**, for `drift_window` consecutive samples |
| `FAULT_MISSING` | `1 << 4` | no usable reading | `missing_limit` consecutive invalid samples |

Notes on the three that need care:

- **STUCK** is not "two equal readings". A stable environment is normal, so the
  check is the spread of a whole window: the reading is stuck when the whole
  window moves less than `stuck_epsilon`. A window-wide spread check is far
  less false-positive prone than comparing consecutive samples, but it still
  cannot prove the difference between a genuinely constant environment and a
  frozen sensor - see [Limitations](#limitations).
- **SPIKE** confirms itself. A jump away from the previous value is reported;
  if the next reading comes back near the value it jumped from, the excursion
  is counted as a confirmed spike. A jump that never comes back (a real level
  change) is reported once and then accepted as the new level.
- **DRIFT** is measured against real time. `drift_threshold` is a slope in
  value units per **second** - 0.01 C/s and 0.05 %RH/s in the shipped
  configurations - taken from the sample timestamps, not from the sample index.
  The same physical trend is therefore judged the same way at 1 Hz and at
  0.2 Hz, which matters as soon as a scheduler is free to change the interval
  at runtime. What is *not* time-based is the confirmation: the verdict has to
  hold for `drift_window` consecutive samples, so a ramp must last roughly two
  windows before it is flagged.

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

- the history is a fixed ring buffer of the most recent **valid** samples,
  `SENSOR_TRUST_MAX_WINDOW` (64) points, statically allocated; each point keeps
  its value **and** its `timestamp_ms`, which is what lets DRIFT measure time;
- `sensor_trust_update()` is `O(window)` and allocation-free, so it is safe to
  call on every reading;
- STUCK's window is counted in **samples, not seconds**: "the reading has not
  moved" is a statement about the readings, and the wall clock is irrelevant to
  it;
- DRIFT's slope is counted in **seconds**: it is a least-squares fit against
  the sample timestamps, reported in value units per second;
- an invalid sample reports `FAULT_MISSING` only: it carries no value, so it
  neither triggers nor clears the value-based detectors, and it restarts the
  drift counter;
- a configuration is rejected outright unless **every** float field is finite.
  NaN and +-Inf are not thresholds, so `min_value`, `max_value`,
  `stuck_epsilon`, `spike_threshold` and `drift_threshold` all have to be real
  numbers;
- `sensor_trust_reset()` only restarts a channel that was initialised
  successfully and whose configuration is still valid. Resetting a context that
  never came up leaves it uninitialised and unusable, rather than fabricating a
  working channel out of an unconfigured struct;
- `sensor_trust_format_flags()` follows the `snprintf` contract - it returns the
  length the complete string needs, however little of it fitted - and never
  writes outside the buffer it was given;
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

The archived v0.1 synthetic example feeds 60 samples of one temperature channel (30 plausible
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
  what keeps a short flat stretch from being reported as a fault. A long flat
  stretch is reported, and it is reported as a *suspicion*: see
  [Limitations](#limitations).
- the fault type is in `flags`. The state alone does not tell you *what* is
  wrong, and a transient fault can be gone again by the last sample.

That output is historical synthetic evidence. The current firmware reads an
SHT30; the seven reproducible synthetic scenarios remain in `simulator/`.

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
Board:                  physical ESP32-S3, connected by USB
Sensor:                 SHT30 at I2C address 0x44, SDA GPIO 8, SCL GPIO 9
Channels from driver:   temperature_C and humidity_percent
Evaluated channel:      temperature_C
BH1750:                 not tested
Real clean/fault metrics: pending complete formal run
```

`firmware/` reads physical SHT30 temperature and humidity over I²C. The
temperature channel passes through a separate deterministic injector and then
the unchanged `sensor_trust_update()` API. Startup fails if SHT30 I²C, CRC or
sanity checks fail. No generated value substitutes for a physical read.

Build status:

```text
ESP-IDF:         v5.4.4, target esp32s3
Command:         idf.py build
Result:          Project build complete
App binary:      see current build output
Core in firmware: core/sensor_trust.c is compiled into the main component
```

The flag formatter does bounded copies instead of calling `snprintf`, so the
core links none of the printf family: no `snprintf`, `vsnprintf` or `sprintf`
symbol is present in the image, and the app binary is 13,120 bytes smaller than
it was before that change. (The demo itself still uses `printf`; the *core* does
not, which is the part that matters when this is dropped into someone else's
firmware.)

The firmware and host tests run the same `core/sensor_trust.c`. The physical
sanity check has passed. It is distinct from the planned ≥30-minute clean
baseline and five repeated episodes of each fault.

## Run it

On macOS, create the test environment inside this checkout. ESP-IDF itself
can stay in `~/esp/esp-idf` with its Python environment in `~/.espressif`;
neither path needs an external drive:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/ -v

export IDF_PYTHON_ENV_PATH="$HOME/.espressif/python_env/idf5.4_py3.13_env"
export ESP_PYTHON="$IDF_PYTHON_ENV_PATH/bin/python"
source "$HOME/esp/esp-idf/export.sh"
cd firmware && idf.py build
```

```bash
# 1. synthetic scenarios -> results/dataset/*.csv
python simulator/generate.py

# 2. replay them through the real C core -> results/scenarios.csv + the table above
python simulator/run.py

# 3. tests (C core + simulator)
python -m pytest tests/ -v

# 4. C core tests on their own, no Python needed. The core has to stay
#    warning-free under the strict flags, so the -Werror build is the real command.
cc -std=c11 -Wall -Wextra -Werror core/sensor_trust.c tests/test_core.c -o test_core -lm
./test_core

# 5. ESP-IDF hardware experiment (requires physical SHT30 wiring)
cd firmware
idf.py set-target esp32s3
idf.py build
```

Current status on the development machine:

```text
C core tests:     21 passed, 0 failed, 0 compiler warnings
                  (-std=c11 -Wall -Wextra -Werror)
Python tests:      22 passed (v0.1 simulator + v0.2 parser/injector)
CI:               .github/workflows/tests.yml, one job, pytest only
```

CI is one job running `python -m pytest tests/ -v` on Ubuntu.
`test_c_host_tests_pass` compiles and runs `tests/test_core.c` inside that same
run, so one step covers both sides. There is no build matrix, no Docker, no
ESP-IDF CI and no coverage upload.

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
│   ├── experiment_config.json  # reviewed thresholds, wiring and episode schedule
│   └── main/
│       ├── CMakeLists.txt
│       ├── sensor_reader.c    # real SHT30 I2C + CRC/sanity checks
│       ├── fault_injector.c   # hardware-independent deterministic injection
│       └── experiment.c       # periodic schedule + ST_* serial protocol
├── hardware/
│   ├── capture.py            # clean-tree serial capture
│   └── evaluate.py           # strict parser + CSV metrics
├── results/
│   ├── dataset/*.csv         # generated scenario streams (self-describing)
│   └── scenarios.csv         # generated summary
├── tests/
│   ├── test_core.c           # 21 C tests for the core
│   ├── test_simulator.py      # 9 Python simulator tests
│   └── test_hardware.py       # parser and injector host tests
├── .github/workflows/tests.yml  # one CI job: pytest (which also runs the C tests)
├── README.md
├── README.zh-CN.md
└── LICENSE
```

## Limitations

- **One channel per context, no fusion.** SensorTrust does not compare channels
  and does not vote across nodes. A single channel cannot tell a real
  environmental change from a sensor fault, which is why drift is reported as
  *suspected*.
- **Quantitative hardware results pending.** The SHT30 link has produced a
  physical reading, but a complete baseline and repeated injection episodes
  have not yet been accepted as formal evidence. The table above remains v0.1
  synthetic evidence.
- **A detected fault is a suspicion, not a verdict.** A frozen reading can be a
  genuinely constant environment; an impossible value can be a wiring problem
  rather than a dead sensor. SensorTrust reports data-quality suspicion, so the
  caller can decide what to do with the reading.
- **STUCK can still confuse a truly constant environment with a frozen
  sensor.** Checking the spread of a full window removes most of that
  confusion - it does not remove all of it. A sensor with coarse resolution in
  a room that really is not changing still looks frozen, and one value channel
  cannot prove which of the two it is looking at.
- **DRIFT is suspected drift, not proof of a sensor failure.** A real,
  sustained environmental change produces exactly the same slope in one
  channel. The multi-window confirmation filters short transients; it does not
  turn a real trend into a fault.
- **A broken clock degrades to silence, not to a new fault.** If the timestamps
  in a window do not all move forward, that window cannot produce a DRIFT
  verdict at all. RANGE, SPIKE and STUCK are unaffected, because none of them
  uses time. v0.1 has five fault types and does not add one for timestamps.
- **No gap/timeout detection.** The API is push-based: it sees the samples it is
  given. A device that stops calling `sensor_trust_update()` entirely produces
  no sample at all, so that case cannot be seen from inside the core - it has to
  be watched by the caller (that is the scheduler's job, not the health
  checker's).
- **The STUCK window is still sample-based.** With a slower or faster sampling
  interval, the same `stuck_window` means a different duration; only the DRIFT
  slope is time-based, so only the DRIFT threshold survives an interval change
  unchanged.
- **The thresholds are hand-set defaults, not tuned.** They are not jointly
  optimised, and they are documented in one place (`scenarios.py` for the
  simulator, `sensor_trust_default_config()` for the generic default) so they
  can be reviewed and changed without hunting through the code.
- **Constant bias is a blind spot to probe.** OFFSET is an injection mode, not
  a sixth detector. A single-channel temporal detector may miss it entirely.

v0.1 is frozen at these five detectors.

## License

MIT - see [LICENSE](LICENSE).
