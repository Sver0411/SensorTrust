# v0.2 physical experiment

The formal v0.2 run reads SHT30 temperature on an ESP32-S3 at 1 Hz. The I²C
address is `0x44` on GPIO 8 (SDA) and GPIO 9 (SCL). The driver checks I²C,
SHT30 CRC, finite temperature and humidity, and plausible physical limits. A
physical read failure remains invalid during fault injection. This experiment
evaluates `temperature_C`; the driver also reads humidity, but humidity was not
evaluated. BH1750 was not tested.

The measurement firmware ran the v0.1 C detector without semantic changes:

```text
physical SHT30 -> raw sample -> fault_injector.c -> sensor_sample_t
             -> unchanged sensor_trust_update() -> ST_SAMPLE serial line
```

## Frozen configuration and schedule

`firmware/experiment_config.json` defines wiring, detector thresholds,
injection strengths and schedule. CMake generates the C constants from this
file and embeds its SHA-256, Git commit and dirty-tree bit. Do not tune
thresholds after inspecting formal results. A changed configuration requires a
new pilot and measurement commit.

The formal schedule is 1810 baseline samples, followed by five episodes each
of FREEZE, SPIKE, DRIFT, DROP, OUT_OF_RANGE and OFFSET. Every episode is
followed by 55 physical PASS samples. The fixed seed is `0`; timing is not
randomized. The 1 Hz interval is a laboratory choice, not a deployment
recommendation.

| Injection | Duration | Value transformation | Target role |
| --- | ---: | --- | --- |
| FREEZE | 40 samples | hold last physical value | STUCK target |
| SPIKE | 1 sample | physical + 10 °C | SPIKE target |
| DRIFT | 90 samples | physical + 0.05 °C/s × elapsed time | DRIFT target |
| DROP | 6 samples | `valid=false` | MISSING target |
| OUT_OF_RANGE | 5 samples | 150 °C | RANGE target |
| OFFSET | 20 samples | physical + 4 °C | non-target blind-spot probe |

## Reproduce analysis

Run these commands from the repository root. The analysis reads the committed
raw serial log; it does not communicate with the board or change raw evidence.

```bash
python -m pytest tests/ -v
cc -std=c11 -Wall -Wextra -Werror core/sensor_trust.c tests/test_core.c -o test_core -lm
./test_core
python hardware/evaluate.py results/v0.2/raw/sht30_temperature_v02_20260923T111155Z.log
python hardware/plot_results.py
python hardware/render_readme.py
```

`capture.py` is only needed for a new experiment. It records complete,
unmodified `ST_*` protocol lines, persists active captures under
`results/experimental/incomplete/`, and moves only validated complete traces
to `results/v0.2/raw/`. It strips boot chatter that may contain device
identifiers and never overwrites a raw log. It refuses dirty formal trees,
wrong measurement firmware commits, bad config hashes, incomplete schedules
and parser errors. Pilot captures use `--allow-dirty` and remain under
`results/experimental/`.

The formal raw log and its SHA-256 are recorded in `hardware_metadata.json`.
`hardware_probe.json` records the separate `esptool.py` capacity probe, the
effective firmware flash configuration, application image header and image
hash. Physical flash capacity is reported separately from the 2 MiB firmware
configuration. The chip/package probe reports embedded PSRAM capacity; the
firmware's PSRAM enablement and runtime bytes are separate fields. The probe
metadata omits the unique USB port and MAC address.

## Observable labels and metric meanings

`injection_mode` is the causal label for how the experiment altered its input.
`expected_observable_flags` is multi-label ground truth generated
independently from injected values, sample validity, the schedule, frozen
configuration and the published v0.1 detector semantics. Label generation
never reads `detected_flags`. For example, an OUT_OF_RANGE transition may
correctly have both RANGE and SPIKE labels, and an OFFSET transition may have a
SPIKE label even though the stable offset plateau has no dedicated detector.
OFFSET stays outside the five target episode recalls.

SPIKE truth marks an over-threshold change from the previous valid value. If
the next valid value returns within the configured threshold of the pre-jump
reference, that return sample is also a SPIKE. The same rule applies to
OUT_OF_RANGE and OFFSET entry/removal boundaries. The causal injector mode is
not a substitute for observable labels.

`sample_results.csv` keeps the raw and injected values, validity, causal mode,
expected observable flags and detector output. `metrics_per_fault.csv`
computes one-vs-rest TP/FP/TN/FN over all samples for each observable label;
other injection modes are not excluded. `cross_fault_detections.csv` maps
causal modes and phases to co-detected labels and records how the former SPIKE
FPs are reclassified. `fault_episodes.csv` records target episode detection
and recovery latency. The first injected sample defines episode start; the
first recovery sample defines fault end. Detection latency is measured to the
first correct detector flag within the injection window; recovery latency is
measured to the first recovery sample without that target bit. OFFSET is a
probe and does not enter the five target detector recalls.

Sample-level recall for STUCK, DRIFT and MISSING includes the detector's
intentional pre-confirmation interval. In particular, with
`missing_limit=3`, the first two invalid samples are still expected MISSING
and count as sample-level FN before the detector may report it. These FNs are
retained; they are not episode misses. The primary result is episode recall
and latency, with sample-level confusion metrics shown as detail.

The clean-baseline summary counts detector bits unsupported by observable
truth. It describes the 1810 samples in this one real-sensor trace and does
not establish a universal zero false-positive rate. A stable physical
environment may resemble a frozen sensor to a single-channel STUCK detector;
a sustained single-channel environmental trend may resemble DRIFT; constant
calibration bias remains a blind spot.

The plot script reads `sample_results.csv`, `fault_episodes.csv` and
`metrics_per_fault.csv`; the README renderer reads the generated CSV and JSON
files. Detection-latency plots show each episode and the median. The timeline
shows physical and injected values, the active interval, the first target
detection and both SPIKE excursion/return markers. No detector code or
measurement data is changed by regenerating these artifacts.
