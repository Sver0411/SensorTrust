# v0.2 physical experiment

The firmware reads SHT30 at address `0x44` on ESP32-S3 GPIO 8 (SDA) and GPIO 9
(SCL). `sensor_reader.c` checks I²C, SHT30 CRC, finite temperature and humidity,
and plausible physical limits. Startup emits `ST_ERROR` and stops if the
sensor cannot be read. The source value for every injected sample remains a
physical reading. The driver also exposes humidity, but this experiment
evaluates **temperature only**. BH1750 has not been tested.

The core in `core/` has no ESP-IDF dependency. The acquisition path is:

```text
physical SHT30 -> raw sample -> fault_injector.c -> sensor_sample_t
             -> unchanged sensor_trust_update() -> ST_SAMPLE serial line
```

## Frozen configuration

`firmware/experiment_config.json` defines the wiring, detector thresholds,
injection strengths, and schedule. CMake generates the C constants from this
file and embeds its SHA-256, the Git commit, and the dirty-tree bit. Rebuild
with `idf.py reconfigure build` after a code commit or manifest edit so the
firmware carries current provenance. Do not tune thresholds after inspecting
the formal results. A changed configuration requires a new pilot and a new
measurement commit.

The formal schedule is 1 Hz: 1810 clean samples (at least 30 minutes from
first to last), followed by five episodes each of FREEZE, SPIKE, DRIFT, DROP,
OUT_OF_RANGE and OFFSET, with 55 physical PASS samples after every episode.
The fixed seed is `0`; no randomized timing is used. The accelerated 1 Hz
schedule is a laboratory choice, not a deployment interval.

| Injection | Duration | Value transformation | Primary target |
| --- | ---: | --- | --- |
| FREEZE | 40 samples | hold last physical value | STUCK |
| SPIKE | 1 sample | physical + 10 °C | SPIKE |
| DRIFT | 90 samples | physical + 0.05 °C/s × real elapsed time | DRIFT |
| DROP | 6 samples | `valid=false` | MISSING |
| OUT_OF_RANGE | 5 samples | 150 °C | RANGE |
| OFFSET | 20 samples | physical + 4 °C | no target; blind-spot probe |

The injector does not read detector flags. A physical read failure remains
invalid even during an injection. If ten consecutive physical reads fail, the
board emits `ST_END` with `hardware_error`; the parser rejects that run.

## Reproduce

From the repository root, with the ESP-IDF 5.4.4 environment activated:

```bash
python -m pytest tests/ -v
cc -std=c11 -Wall -Wextra -Werror core/sensor_trust.c tests/test_core.c -o test_core -lm
./test_core

git status --porcelain             # must be empty for formal capture
cd firmware
idf.py reconfigure build
idf.py -p /dev/cu.YOUR_PORT flash
cd ..
python hardware/capture.py --port /dev/cu.YOUR_PORT
python hardware/evaluate.py results/v0.2/raw/YOUR_LOG.log
```

`capture.py` resets the board and records complete, unmodified `ST_*` protocol
lines. It persists each line to `results/experimental/incomplete/` while the
run is active, then moves the fully validated trace to `results/v0.2/raw/`.
An interrupted trace stays incomplete and cannot be mistaken for formal data.
It strips boot chatter, which may contain device identifiers. It
refuses a dirty formal tree, a dirty firmware, a wrong Git commit, a bad config
hash, an incomplete schedule, and any parser error. It never overwrites a raw
log. The protocol includes chip revision, flash, PSRAM, ESP-IDF version,
sensor address, wiring, monotonic timestamp, configuration and injection truth;
it omits MAC and unique USB serial numbers.

For a pilot, use `--allow-dirty` with `capture.py`; output goes under
`results/experimental/raw/`. Evaluate pilots with:

```bash
python hardware/evaluate.py results/experimental/raw/YOUR_LOG.log \
  --allow-dirty --out results/experimental/YOUR_RUN
```

`evaluate.py` regenerates `sample_results.csv`, `fault_episodes.csv`,
`metrics_per_fault.csv`, `metrics_per_channel.csv`, `clean_baseline.json`, and
`hardware_metadata.json` from the raw log and its `.capture.json` sidecar. The
sidecar records capture time, baud rate, and the compiler target/version. The
raw protocol log remains the source of truth for samples and detector output.

To regenerate the four figures and the metric tables embedded in both top-level
READMEs, install the optional plotting dependency and run:

```bash
.venv/bin/python -m pip install -r requirements-analysis.txt
.venv/bin/python hardware/plot_results.py
.venv/bin/python hardware/render_readme.py
```

All reported values are read from the CSV/JSON results; neither README is the
source of metric values. The figures cover one representative trace for each
injection mode, per-episode detection latency, episode recall versus raw sample
precision, and clean-baseline false positives.

## Metric meanings

`sample_results.csv` keeps every raw value, validity bit, injected value,
injection mode, expected primary fault, raw detector bitmask, score and state.
There is no lifecycle smoothing in this version, so `active_flags` is blank.
`fault_episodes.csv` uses the
first injected sample as the start; the first recovery sample is the fault end.
Detection latency is the difference from the start to the first correct
detector flag within the injection window. Recovery latency is the difference
from the fault end to the first recovery sample without that target flag.
Both are also reported in sample steps (zero means detection or clearance on
the first relevant sample). Undetected episodes have `N/A` latencies.

`metrics_per_fault.csv` contains raw per-sample TP/FP/TN/FN and episode
recall. For each bit, samples injected with a *different* primary fault are
excluded from its confusion matrix: an out-of-range jump can legitimately
also produce SPIKE, so that extra flag is not automatically called a false
positive. Recovery tail flags do count as raw per-sample false positives and
are also shown by recovery latency. Undefined ratios are blank (`N/A`), never
fabricated zero. p95 uses linear interpolation between ordered episode
latencies. OFFSET is reported as a probe in the episode file and does not
contribute to five-detector episode recall.

The clean baseline's false-positive count only counts flags unsupported by
its physical-read truth. A real I²C read failure is `MISSING` ground truth,
not a false alarm. Stable physical surroundings can still look frozen to a
single-channel STUCK detector. Actual failures and limitations belong in the
results, not in threshold changes made after measurement.
