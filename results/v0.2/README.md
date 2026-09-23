# v0.2 physical evidence

This directory contains the committed ESP32-S3/SHT30 temperature evaluation. The immutable `raw/` serial log is the source for the parsed CSV and JSON files; the measurement firmware used the v0.1 C detector without semantic changes.

Clean baseline: 30 min 9 s, 1,810 samples, 0 false-positive samples, 0 physical read failures.

| Fault | Detected episodes | Recall | Median detection | p95 detection | Median recovery | Sample FP | Sample FN |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| RANGE | 5/5 | 100% | 0 ms | 0 ms | 0 ms | 0 | 0 |
| STUCK | 5/5 | 100% | 18,000 ms | 18,000 ms | 0 ms | 0 | 87 |
| SPIKE | 5/5 | 100% | 0 ms | 0 ms | 1,000 ms | 25 | 0 |
| DRIFT | 5/5 | 100% | 30,000 ms | 30,000 ms | 0 ms | 0 | 148 |
| MISSING | 5/5 | 100% | 2,000 ms | 2,000 ms | 0 ms | 0 | 10 |

OFFSET was a blind-spot probe, not a detector target. The sustained bias produced no OFFSET verdict; its entry and exit caused transient SPIKE flags. The core/API semantics were not changed after the run.

Measurement commit: `6e043c98544d8682371bad9b7b3a2e99c7189696` (`git_dirty=false`).
Config SHA-256: `9a1461beb37f50ae4958950222feea186b1f3509c4fcf60de19f5c3606c1da6e`.
Compiler: `xtensa-esp-elf-gcc (crosstool-NG esp-14.2.0_20260121) 14.2.0`.
Raw log SHA-256: `80f8a6d4744784e44f35b9c4f0f29d1cf41d764d6c399f99bb1fe71eae746044`.

`sample_results.csv` preserves each physical and injected value with sample ground truth. `fault_episodes.csv` records event latency and recovery; `metrics_per_fault.csv` reports raw per-sample confusion counts separately from episode recall. `hardware_metadata.json` records board, wiring, detector configuration and provenance.

The clean baseline is one roughly 30-minute observation from this board/environment. Humidity was not evaluated; BH1750 was not tested. Do not generalize this result to other sensors, rooms or deployment sample rates.

`results/scenarios.csv` is separate v0.1 synthetic evidence.
