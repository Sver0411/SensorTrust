# v0.2 physical evidence

This directory contains the ESP32-S3/SHT30 `temperature_C` evaluation. The immutable `raw/` serial log is the source for parsed samples and detector outputs. The measurement used the v0.1 C detector without semantic changes.

Clean baseline: 1,810 samples / 30m09; 0 observed false-positive samples. This single observation does not establish a universal zero false-positive rate.

Five target fault types × five episodes produced 25/25 detected target episodes. OFFSET is a non-target blind-spot probe.

| Fault | Episodes | Detected | Episode recall | Median detection | Observed range | Median recovery |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| RANGE | 5 | 5 | 100% | 0 s | 0 s–0 s | 0 s |
| STUCK | 5 | 5 | 100% | 18 s | 16 s–18 s | 0 s |
| SPIKE | 5 | 5 | 100% | 0 s | 0 s–0 s | 1 s |
| DRIFT | 5 | 5 | 100% | 30 s | 29 s–30 s | 0 s |
| MISSING | 5 | 5 | 100% | 2 s | 2 s–2 s | 0 s |

## Evaluation semantics

`injection_mode` records causal labels; `expected_observable_flags` is generated independently from the injected signal, validity, schedule, frozen configuration and published v0.1 detector definitions. Truth generation never reads detector outputs. Labels are multilabel, so RANGE and SPIKE may both be true for one sample.

The old 25 SPIKE false positives were observable transitions: SPIKE recovery 5, DRIFT recovery 5, OUT_OF_RANGE recovery 5, OFFSET entry 5, OFFSET removal 5. The old evaluator excluded 5 RANGE+SPIKE samples at OUT_OF_RANGE entry. Corrected SPIKE sample metrics: TP 35, FP 0, FN 0. The formal trace has no genuine SPIKE false positives.

| Cause | Phase | Expected SPIKE samples | Detected | Former FP |
| --- | --- | ---: | ---: | ---: |
| DRIFT | RECOVERY | 5 | 5 | 5 |
| OFFSET | INJECTION | 5 | 5 | 5 |
| OFFSET | RECOVERY | 5 | 5 | 5 |
| OUT_OF_RANGE | INJECTION | 5 | 5 | 0 |
| OUT_OF_RANGE | RECOVERY | 5 | 5 | 5 |
| SPIKE | INJECTION | 5 | 5 | 0 |
| SPIKE | RECOVERY | 5 | 5 | 5 |

| Fault | TP | FP | TN | FN | Precision | Recall | F1 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| RANGE | 25 | 0 | 4245 | 0 | 1.000 | 1.000 | 1.000 |
| STUCK | 113 | 0 | 4070 | 87 | 1.000 | 0.565 | 0.722 |
| SPIKE | 35 | 0 | 4235 | 0 | 1.000 | 1.000 | 1.000 |
| DRIFT | 302 | 0 | 3820 | 148 | 1.000 | 0.671 | 0.803 |
| MISSING | 20 | 0 | 4240 | 10 | 1.000 | 0.667 | 0.800 |

Sample-level recall for STUCK, DRIFT and MISSING includes the intentional pre-confirmation interval. In particular, the first two invalid samples with `missing_limit=3` remain sample-level FN, not episode misses.

OFFSET is excluded from target recall. Its constant +4 °C plateau has no dedicated detector; SPIKE was emitted on 5/5 entry and 5/5 removal boundaries.

## Hardware metadata

Physical flash: 16 MiB by `esptool.py flash_id`; configured and image-header size: 2 MiB; firmware-reported flash: 2 MiB. Chip/package identification reports 8 MiB embedded PSRAM (not a RAM test); firmware PSRAM enabled: False; runtime available: 0 bytes.

Measurement commit: `6e043c98544d8682371bad9b7b3a2e99c7189696` (`git_dirty=false`).
Config SHA-256: `9a1461beb37f50ae4958950222feea186b1f3509c4fcf60de19f5c3606c1da6e`; compiler: `xtensa-esp-elf-gcc (crosstool-NG esp-14.2.0_20260121) 14.2.0`.
Raw log: [`results/v0.2/raw/sht30_temperature_v02_20260923T111155Z.log`](results/v0.2/raw/sht30_temperature_v02_20260923T111155Z.log); SHA-256: `80f8a6d4744784e44f35b9c4f0f29d1cf41d764d6c399f99bb1fe71eae746044`.

`sample_results.csv` preserves physical and injected values, causal labels, independent observable labels and detector output. `fault_episodes.csv` reports episode detection and recovery latency; `metrics_per_fault.csv` reports the full per-sample confusion counts; `cross_fault_detections.csv` maps causal injection modes to co-detected observable labels; `hardware_metadata.json` and `hardware_probe.json` record firmware and independently probed capacity evidence.

The detection latency plot shows each of the five episode values with a median marker; it does not emphasize p95 for n=5. The timeline plot shows physical and injected traces, active intervals, first target detections and both SPIKE excursion/return markers. The clean baseline represents one observation and must not be generalized to other sensors, rooms or sample rates.

Figures: [fault timeline](plots/fault_timeline.png), [detection latency](plots/detection_latency.png), [detection performance](plots/detection_performance.png), [baseline false positives](plots/baseline_false_positives.png).

Humidity was not evaluated; BH1750 was not tested. `results/scenarios.csv` is separate v0.1 synthetic evidence.
