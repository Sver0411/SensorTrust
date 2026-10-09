<h1 align="center">SensorTrust</h1>

<p align="center"><strong>Portable Sensor Health Assessment in C11</strong></p>

<p align="center">Fault Detection · Health Scoring · Physical Sensor Evidence</p>

---

<p align="center">
  <a href="#project-overview">Overview</a> ·
  <a href="#detection-method">Method</a> ·
  <a href="#implementation-and-hardware-status">Status</a> ·
  <a href="#relationship-to-smart-agriculture-edge-ai">Related System</a> ·
  <a href="README.zh-CN.md">简体中文</a>
</p>

---

<a id="project-overview"></a>

SensorTrust is a portable C11 core that checks the health of individual sensor channels. It turns timestamped readings into fault flags, a severity score, and a `HEALTHY`, `DEGRADED`, or `FAULT` state. v0.2 evaluates the frozen v0.1 detector on real ESP32-S3 + SHT30 temperature readings with deterministic software fault injection.

## Research question

Can a node identify suspicious readings before they enter a control or telemetry pipeline? A sensor can keep producing plausible values while its output is frozen, drifting, or biased. SensorTrust evaluates the readings supplied by the caller; sampling and upload decisions belong to the surrounding system.

## Detection method

```text
value + validity + timestamp
    → per-channel C11 detector
    → fault bitmask + health_score (0..100) + health state
```

| Flag | Bit | What it looks for | Default trigger |
| --- | --- | --- | --- |
| `FAULT_RANGE` | `1 << 0` | a physically impossible value | `value < min_value` or `value > max_value` |
| `FAULT_STUCK` | `1 << 1` | a channel that stopped moving | `max - min < stuck_epsilon` over the last `stuck_window` samples |
| `FAULT_SPIKE` | `1 << 2` | a sudden jump | `abs(value - previous valid value) > spike_threshold` |
| `FAULT_DRIFT` | `1 << 3` | a sustained one-directional trend | `abs(slope) > drift_threshold` in value units **per second**, for `drift_window` consecutive samples |
| `FAULT_MISSING` | `1 << 4` | no usable reading | `missing_limit` consecutive invalid samples |

The detectors operate independently, so one sample can carry multiple flags. STUCK checks a complete window rather than consecutive equality. SPIKE reports a sudden transition and can confirm a return to the prior level; a persistent step is subsequently accepted. DRIFT uses a least-squares slope against timestamps, in value units per second, but requires confirmation over consecutive samples. STUCK and confirmation windows remain sample-based, so their elapsed duration changes with the sampling interval.

The score starts at 100 and subtracts penalties for active faults: RANGE/MISSING 55 each, STUCK 35, SPIKE/DRIFT 25 each, clamped to 0..100. Scores 80..100 map to `HEALTHY`, 50..79 to `DEGRADED`, and 0..49 to `FAULT`. This is a heuristic severity score, not a calibrated probability or proof of physical sensor damage.

## Key findings

The 1 Hz laboratory evaluation used physical SHT30 readings followed by software injection, rather than physically damaging the sensor. All 25 target episodes were detected; their median detection latencies were:

| Fault | Detected episodes | Median detection latency |
| --- | ---: | ---: |
| RANGE | 5/5 | 0 s |
| STUCK | 5/5 | 18 s |
| SPIKE | 5/5 | 0 s |
| DRIFT | 5/5 | 30 s |
| MISSING | 5/5 | 2 s |

The clean baseline contained 1,810 samples over 30m09s, with 0 observed false-positive samples and 0 physical read failures. This single observation does not establish a universal zero false-positive rate. Episode detection also does not imply perfect sample-level recall: confirmation delays reduce STUCK, DRIFT, and MISSING recall.

**Measured blind spot:** the stable +4 °C OFFSET plateau is not identified. Its entry and removal generated SPIKE in 5/5 episodes each, but OFFSET is not a sixth detector and is excluded from target recall.

The original four rendered plots, sample metrics, and evaluation definitions are retained in the [detailed evaluation](#detailed-evaluation). See the [physical evidence report](results/v0.2/README.md) and [hardware protocol](hardware/README.md) for the full procedure.

### Experimental plots

![Physical SHT30 raw and injected values, active intervals and detector events](results/v0.2/plots/fault_timeline.png)

![One detection-latency point per episode and the median](results/v0.2/plots/detection_latency.png)

![Episode recall and per-sample precision, recall and F1](results/v0.2/plots/detection_performance.png)

![Clean-baseline observed false-positive summary](results/v0.2/plots/baseline_false_positives.png)

## Implementation and hardware status

- [core/sensor_trust.c](core/sensor_trust.c) is shared by host replay and ESP32 firmware. It uses a fixed 64-point valid-sample ring buffer, has `O(window)` update cost, allocates no heap, and depends on no RTOS, networking, stdio, or hardware headers.
- Each channel has its own configuration and context. Input is `value`, `valid`, and `timestamp_ms`; invalid readings are handled through MISSING. See the [public C API](core/sensor_trust.h).
- The ESP-IDF v5.4.4 firmware reads a physical SHT30 at I²C address `0x44`, SDA GPIO 8 / SCL GPIO 9, and passes temperature through a separate deterministic injector into the same core. Startup checks I²C, CRC, and reading sanity.
- Temperature was evaluated; humidity is read but was not evaluated, and BH1750 was not tested. The accelerated 1 Hz laboratory schedule is not a deployment interval.

## Quick start and reproduction

Host replay requires Python and a C11 compiler. From the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python simulator/generate.py
python simulator/run.py
python -m pytest tests/ -v
```

Python generates and aggregates scenarios; `simulator/run.py` compiles and replays the actual C core. The seven synthetic scenarios and their results remain in [results/scenarios.csv](results/scenarios.csv), separate from physical evidence.

For firmware, use an ESP-IDF shell and the SHT30 wiring documented in the [hardware guide](hardware/README.md):

```bash
cd firmware
idf.py set-target esp32s3
idf.py build
```

Serial capture, evaluation, and plot generation are documented in the hardware guide. `hardware/render_readme.py` updates the marked bilingual result blocks from the recorded CSV/JSON evidence.

## Relationship to Smart-Agriculture-Edge-AI

[Smart-Agriculture-Edge-AI](https://github.com/Sver0411/Smart-Agriculture-Edge-AI) reuses SensorTrust's sensor-health semantics and core implementation. SensorTrust remains an independent per-channel detector; the main system supplies scheduling, communication, and control decisions.

## Limitations and future work

- A single channel cannot reliably separate sensor faults from real environmental changes: a stable environment can resemble STUCK, and a real trend can resemble DRIFT. Thresholds are manually configured defaults.
- Constant in-range bias remains a blind spot. Reference sensors, cross-channel checks, and broader environmental validation are future work.
- Hardware evidence covers one temperature channel and one session. More sensors, deployment intervals, and longer clean baselines are needed to assess generalization.
- The push API cannot detect a device that stops calling `sensor_trust_update()`; the caller must monitor timeouts. Non-increasing timestamps suppress DRIFT evaluation for that window rather than creating another fault type.

## Detailed evaluation

This generated section preserves the original measurement tables and all rendered figures. Its start/end markers are used by the update script.

<details>
<summary>Expand generated metrics and original evaluation definitions</summary>

<!-- V0.2_RESULTS_START -->
## v0.2 physical evaluation (generated from the raw serial log)

Hardware: ESP32-S3 + SHT30. Evaluated channel: `temperature_C`; this experiment sampled at 1 Hz.
Clean real-sensor baseline: 1,810 samples / 30m09; 0 / 1,810 observed false-positive samples (0.000%); 0 physical read failures. This describes this observation and does not establish a universal zero false-positive rate.

Five target fault types were injected for five episodes each: 25/25 target episodes detected. OFFSET is a blind-spot probe and is excluded from the five target recalls.

| Fault | Episodes | Detected | Episode recall | Median detection latency | Observed range | Median recovery latency |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| RANGE | 5 | 5 | 100% | 0 s | 0 s–0 s | 0 s |
| STUCK | 5 | 5 | 100% | 18 s | 16 s–18 s | 0 s |
| SPIKE | 5 | 5 | 100% | 0 s | 0 s–0 s | 1 s |
| DRIFT | 5 | 5 | 100% | 30 s | 29 s–30 s | 0 s |
| MISSING | 5 | 5 | 100% | 2 s | 2 s–2 s | 0 s |

### Per-sample multilabel metrics

| Fault | TP | FP | TN | FN | Precision | Recall | F1 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| RANGE | 25 | 0 | 4245 | 0 | 1.000 | 1.000 | 1.000 |
| STUCK | 113 | 0 | 4070 | 87 | 1.000 | 0.565 | 0.722 |
| SPIKE | 35 | 0 | 4235 | 0 | 1.000 | 1.000 | 1.000 |
| DRIFT | 302 | 0 | 3820 | 148 | 1.000 | 0.671 | 0.803 |
| MISSING | 20 | 0 | 4240 | 10 | 1.000 | 0.667 | 0.800 |

`injection_mode` is the causal experiment label. `expected_observable_flags` is independent multilabel ground truth derived from injected signal values, validity, the experiment schedule, the frozen configuration and the published detector definitions. Detector output is not consulted while generating truth. A sample may correctly be both RANGE and SPIKE.

The old SPIKE confusion matrix's 25 false positives were all observable transitions: SPIKE recovery 5, DRIFT recovery 5, OUT_OF_RANGE recovery 5, OFFSET entry 5, OFFSET removal 5. The old rule also excluded 5 RANGE+SPIKE samples at OUT_OF_RANGE entry from the SPIKE confusion matrix. Corrected SPIKE metrics are `TP=35`, `FP=0`, `FN=0`; no genuine SPIKE false positive was observed in this formal dataset.

| Injection cause | Phase | Observable SPIKE samples | Detected | Former FP count |
| --- | --- | ---: | ---: | ---: |
| DRIFT | RECOVERY | 5 | 5 | 5 |
| OFFSET | INJECTION | 5 | 5 | 5 |
| OFFSET | RECOVERY | 5 | 5 | 5 |
| OUT_OF_RANGE | INJECTION | 5 | 5 | 0 |
| OUT_OF_RANGE | RECOVERY | 5 | 5 | 5 |
| SPIKE | INJECTION | 5 | 5 | 0 |
| SPIKE | RECOVERY | 5 | 5 | 5 |

OFFSET is a non-target blind-spot probe. The constant +4 °C plateau has no dedicated OFFSET detector; SPIKE was emitted on 5/5 entry boundaries and 5/5 removal boundaries. The stable bias itself is not identified.

Sample-level recall for STUCK, DRIFT and MISSING includes the intentional interval after fault onset but before the detector's confirmation condition is met. With `missing_limit=3`, the first two invalid samples are observable MISSING truth and count as sample-level FN; this is not an episode miss.

The clean baseline had 0 observed false-positive samples in 1,810 samples over 30m09s. Its observed sample rate was 0.000%; this does not establish a universal zero false-positive rate.

Hardware capacity: physical flash 16 MiB (`esptool.py flash_id`); firmware-configured capacity 2 MiB, image header 2 MiB, and firmware-reported size 2 MiB. ESP32-S3 chip/package identification reports 8 MiB embedded PSRAM (not a memory test); PSRAM is disabled in this firmware and runtime available PSRAM is 0 bytes.

Figures (generated from the CSV/JSON evidence):

![Physical SHT30 raw and injected values, active intervals and detector events](results/v0.2/plots/fault_timeline.png)

![One detection-latency point per episode and the median](results/v0.2/plots/detection_latency.png)

![Episode recall and per-sample precision, recall and F1](results/v0.2/plots/detection_performance.png)

![Clean-baseline observed false-positive summary](results/v0.2/plots/baseline_false_positives.png)

Measurement firmware commit `6e043c98544d8682371bad9b7b3a2e99c7189696` (`git_dirty=false`); config SHA-256 `9a1461beb37f50ae4958950222feea186b1f3509c4fcf60de19f5c3606c1da6e`; compiler `xtensa-esp-elf-gcc (crosstool-NG esp-14.2.0_20260121) 14.2.0`. Raw log `results/v0.2/raw/sht30_temperature_v02_20260923T111155Z.log`, SHA-256 `80f8a6d4744784e44f35b9c4f0f29d1cf41d764d6c399f99bb1fe71eae746044`.
Evidence files: [sample_results.csv](results/v0.2/sample_results.csv), [fault_episodes.csv](results/v0.2/fault_episodes.csv), [metrics_per_fault.csv](results/v0.2/metrics_per_fault.csv), [cross_fault_detections.csv](results/v0.2/cross_fault_detections.csv), [hardware_metadata.json](results/v0.2/hardware_metadata.json).
SHT30 `temperature_C` was evaluated; `humidity_percent` is read by the driver but was not evaluated; BH1750 was not tested.
<!-- V0.2_RESULTS_END -->

</details>

## License

MIT — see [LICENSE](LICENSE).
