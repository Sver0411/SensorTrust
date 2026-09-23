"""Render README hardware metrics from machine-readable v0.2 evidence."""
from __future__ import annotations

import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "results" / "v0.2"
START = "<!-- V0.2_RESULTS_START -->"
END = "<!-- V0.2_RESULTS_END -->"
ORDER = ("RANGE", "STUCK", "SPIKE", "DRIFT", "MISSING")


def csv_rows(name: str) -> list[dict[str, str]]:
    with (RESULTS / name).open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def ms(value: str | int | float | None) -> str:
    if value in (None, "", "N/A"):
        return "N/A"
    return f"{float(value) / 1000:,.0f} s"


def ratio(value: str) -> str:
    return "N/A" if not value or value == "N/A" else f"{float(value):.3f}"


def latency_range(rows: list[dict[str, str]], fault: str) -> str:
    values = [int(row["detection_latency_ms"]) for row in rows
              if row["fault"] == fault and row["detection_latency_ms"] not in ("", "N/A")]
    return f"{ms(min(values))}–{ms(max(values))}" if values else "N/A"


def spike_reclassification(cross: list[dict[str, str]]) -> tuple[list[dict[str, str]], int]:
    rows = [row for row in cross if row["detected_fault"] == "SPIKE" and
            int(row["previous_SPIKE_FP_samples"]) > 0]
    return rows, sum(int(row["previous_SPIKE_FP_samples"]) for row in rows)


def spike_co_detections(cross: list[dict[str, str]]) -> list[dict[str, str]]:
    return [row for row in cross if row["detected_fault"] == "SPIKE" and
            (int(row["expected_observable_samples"]) > 0 or
             int(row["detected_samples"]) > 0)]


def former_spike_summary(rows: list[dict[str, str]]) -> str:
    order = (("SPIKE", "RECOVERY", "SPIKE recovery"),
             ("DRIFT", "RECOVERY", "DRIFT recovery"),
             ("OUT_OF_RANGE", "RECOVERY", "OUT_OF_RANGE recovery"),
             ("OFFSET", "INJECTION", "OFFSET entry"),
             ("OFFSET", "RECOVERY", "OFFSET removal"))
    counts = {(row["injection_mode"], row["phase"]):
              int(row["previous_SPIKE_FP_samples"]) for row in rows}
    return ", ".join(f"{label} {counts.get((mode, phase), 0)}"
                     for mode, phase, label in order)


def common_data():
    baseline = json.loads((RESULTS / "clean_baseline.json").read_text(encoding="utf-8"))
    metrics = {row["fault"]: row for row in csv_rows("metrics_per_fault.csv")}
    episodes = csv_rows("fault_episodes.csv")
    samples = csv_rows("sample_results.csv")
    metadata = json.loads((RESULTS / "hardware_metadata.json").read_text(encoding="utf-8"))
    cross = csv_rows("cross_fault_detections.csv")
    offset_episodes = [row for row in episodes if row["fault"] == "OFFSET"]
    offset_entry = next((row for row in cross if row["injection_mode"] == "OFFSET" and
                         row["phase"] == "INJECTION" and row["detected_fault"] == "SPIKE"), None)
    offset_exit = next((row for row in cross if row["injection_mode"] == "OFFSET" and
                        row["phase"] == "RECOVERY" and row["detected_fault"] == "SPIKE"), None)
    return baseline, metrics, episodes, samples, metadata, cross, offset_episodes, offset_entry, offset_exit


def make_block(*, chinese: bool) -> str:
    (baseline, metrics, episodes, _samples, metadata, cross, offset_episodes,
     offset_entry, offset_exit) = common_data()
    duration_min, duration_sec = divmod(baseline["duration_ms"] // 1000, 60)
    rate = baseline["false_positive_rate"] * 100
    old_spike_rows, prior_spike_fp = spike_reclassification(cross)
    spike_rows = spike_co_detections(cross)
    former_spike_detail = former_spike_summary(old_spike_rows)
    oor_entry = next((row for row in cross if row["injection_mode"] == "OUT_OF_RANGE" and
                      row["phase"] == "INJECTION" and row["detected_fault"] == "SPIKE"), None)
    flash_mib = metadata["flash_physical_bytes"] / (1024 * 1024)
    flash_cfg_mib = metadata["flash_configured_bytes"] / (1024 * 1024)
    psram_mib = metadata["psram_physical_bytes"] / (1024 * 1024)
    raw_hash = metadata["raw_log_sha256"]
    compiler = metadata.get("toolchain", {}).get("compiler_version", "not recorded")
    meta_commit = metadata["git_commit"]
    config_hash = metadata["config_sha256"]
    raw_log = metadata["raw_log"]

    if chinese:
        lines = [
            "## v0.2 真机评估（由原始串口日志生成）",
            "",
            "硬件：ESP32-S3 + SHT30；评估通道：`temperature_C`；采样率：本实验 1 Hz。",
            f"正常真实数据基线：{baseline['samples']:,} 个样本 / {duration_min}m{duration_sec:02d}s；"
            f"本次观察到误报样本 {baseline['false_positive_samples']} / {baseline['samples']:,}"
            f"（{rate:.3f}%），物理读取失败 {baseline['raw_read_failures']}。"
            "这只描述本次观察，不代表普遍的零误报率。",
            "",
            "注入 5 类目标故障，每类 5 轮：目标 episode 共 25/25 检出；OFFSET 是盲点探针，不计入五类目标召回率。",
            "",
            "| 故障 | Episodes | 检出 | Episode recall | 中位检测延迟 | 观测范围 | 中位恢复延迟 |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
        for fault in ORDER:
            row = metrics[fault]
            lines.append(f"| {fault} | {row['episodes']} | {row['episodes_detected']} | "
                         f"{float(row['episode_recall']):.0%} | "
                         f"{ms(row['median_detection_latency_ms'])} | "
                         f"{latency_range(episodes, fault)} | "
                         f"{ms(row['median_recovery_latency_ms'])} |")
        lines += [
            "",
            "### 逐样本多标签指标",
            "",
            "| 故障 | TP | FP | TN | FN | Precision | Recall | F1 |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
        for fault in ORDER:
            row = metrics[fault]
            lines.append(f"| {fault} | {row['sample_tp']} | {row['sample_fp']} | "
                         f"{row['sample_tn']} | {row['sample_fn']} | "
                         f"{ratio(row['precision'])} | {ratio(row['recall'])} | {ratio(row['f1'])} |")
        lines += [
            "",
            "`injection_mode` 表示实验施加原因；`expected_observable_flags` 是依据注入信号、有效性、实验 schedule、冻结配置和已公开 detector 定义独立生成的多标签 truth。生成 truth 时不读取 detector 输出。一个样本可以同时为 RANGE 和 SPIKE。",
            "",
            f"旧版 SPIKE 的 {prior_spike_fp} 个 FP 均是合法可观测边界：{former_spike_detail}。旧口径还把 OUT_OF_RANGE 进入边界的 {oor_entry['expected_observable_samples'] if oor_entry else 0} 个 RANGE+SPIKE 样本排除在 SPIKE confusion matrix 外。修正后 SPIKE 为 `TP={metrics['SPIKE']['sample_tp']}`、`FP={metrics['SPIKE']['sample_fp']}`、`FN={metrics['SPIKE']['sample_fn']}`；本组正式数据没有 genuine SPIKE false positive。",
            "",
            "| 原注入原因 | 阶段 | 可观测 SPIKE 样本 | 检出 | 旧口径 FP 数 |",
            "| --- | --- | ---: | ---: | ---: |",
        ]
        for row in spike_rows:
            lines.append(f"| {row['injection_mode']} | {row['phase']} | "
                         f"{row['expected_observable_samples']} | {row['detected_samples']} | "
                         f"{row['previous_SPIKE_FP_samples']} |")
        lines += [
            "",
            f"OFFSET 是 non-target blind-spot probe：恒定 +4 °C 平台没有 OFFSET detector；进入边界 {offset_entry['detected_samples'] if offset_entry else 0}/{len(offset_episodes)}、撤除边界 {offset_exit['detected_samples'] if offset_exit else 0}/{len(offset_episodes)} 触发 SPIKE。稳定偏移本身不被识别。",
            "",
            "STUCK、DRIFT 和 MISSING 的 sample-level recall 包含故障开始后、达到确认条件前的刻意确认窗口。MISSING `missing_limit=3`，前三个 invalid 样本都按可观测 MISSING truth 计；前两个尚未确认的样本因此是 FN，不是 episode miss。",
            "",
            "Clean baseline 观察到 0 / 1,810 个误报样本（30m09s）。Observed sample rate 为 0.000%；这不能证明普遍的零误报率。",
            "",
            f"硬件容量：物理 Flash {flash_mib:g} MiB（`esptool.py flash_id`）；固件配置 {flash_cfg_mib:g} MiB、image header {metadata['flash_image_header_bytes'] // (1024 * 1024)} MiB、固件报告 {metadata['flash_firmware_reported_bytes'] // (1024 * 1024)} MiB。ESP32-S3 芯片/封装识别报告 embedded PSRAM {psram_mib:g} MiB（不是内存测试）；本固件未启用 PSRAM，运行时可用 {metadata['psram_runtime_available_bytes']} bytes。",
            "",
            "图表（由 CSV/JSON 生成）：",
            "",
            "![真机 SHT30 原始值、注入值、活动区间和检测事件](results/v0.2/plots/fault_timeline.png)",
            "",
            "![每轮一个检测延迟点和中位数](results/v0.2/plots/detection_latency.png)",
            "",
            "![Episode recall 与逐样本 precision、recall、F1](results/v0.2/plots/detection_performance.png)",
            "",
            "![Clean baseline 误报样本摘要](results/v0.2/plots/baseline_false_positives.png)",
            "",
            f"测量固件 commit `{meta_commit}`（git_dirty=false）；配置 SHA-256 `{config_hash}`；编译器 `{compiler}`。原始日志 `{raw_log}`，SHA-256 `{raw_hash}`。",
            "结果文件：[sample_results.csv](results/v0.2/sample_results.csv)、[fault_episodes.csv](results/v0.2/fault_episodes.csv)、[metrics_per_fault.csv](results/v0.2/metrics_per_fault.csv)、[cross_fault_detections.csv](results/v0.2/cross_fault_detections.csv)、[hardware_metadata.json](results/v0.2/hardware_metadata.json)。",
            "SHT30 `temperature_C` 已评估；`humidity_percent` 由驱动读取但未评估；BH1750 未测试。",
        ]
    else:
        lines = [
            "## v0.2 physical evaluation (generated from the raw serial log)",
            "",
            "Hardware: ESP32-S3 + SHT30. Evaluated channel: `temperature_C`; this experiment sampled at 1 Hz.",
            f"Clean real-sensor baseline: {baseline['samples']:,} samples / {duration_min}m{duration_sec:02d}; "
            f"{baseline['false_positive_samples']} / {baseline['samples']:,} observed false-positive samples "
            f"({rate:.3f}%); {baseline['raw_read_failures']} physical read failures. "
            "This describes this observation and does not establish a universal zero false-positive rate.",
            "",
            "Five target fault types were injected for five episodes each: 25/25 target episodes detected. OFFSET is a blind-spot probe and is excluded from the five target recalls.",
            "",
            "| Fault | Episodes | Detected | Episode recall | Median detection latency | Observed range | Median recovery latency |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
        for fault in ORDER:
            row = metrics[fault]
            lines.append(f"| {fault} | {row['episodes']} | {row['episodes_detected']} | "
                         f"{float(row['episode_recall']):.0%} | "
                         f"{ms(row['median_detection_latency_ms'])} | "
                         f"{latency_range(episodes, fault)} | "
                         f"{ms(row['median_recovery_latency_ms'])} |")
        lines += [
            "",
            "### Per-sample multilabel metrics",
            "",
            "| Fault | TP | FP | TN | FN | Precision | Recall | F1 |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
        for fault in ORDER:
            row = metrics[fault]
            lines.append(f"| {fault} | {row['sample_tp']} | {row['sample_fp']} | "
                         f"{row['sample_tn']} | {row['sample_fn']} | "
                         f"{ratio(row['precision'])} | {ratio(row['recall'])} | {ratio(row['f1'])} |")
        lines += [
            "",
            "`injection_mode` is the causal experiment label. `expected_observable_flags` is independent multilabel ground truth derived from injected signal values, validity, the experiment schedule, the frozen configuration and the published detector definitions. Detector output is not consulted while generating truth. A sample may correctly be both RANGE and SPIKE.",
            "",
            f"The old SPIKE confusion matrix's {prior_spike_fp} false positives were all observable transitions: {former_spike_detail}. The old rule also excluded {oor_entry['expected_observable_samples'] if oor_entry else 0} RANGE+SPIKE samples at OUT_OF_RANGE entry from the SPIKE confusion matrix. Corrected SPIKE metrics are `TP={metrics['SPIKE']['sample_tp']}`, `FP={metrics['SPIKE']['sample_fp']}`, `FN={metrics['SPIKE']['sample_fn']}`; no genuine SPIKE false positive was observed in this formal dataset.",
            "",
            "| Injection cause | Phase | Observable SPIKE samples | Detected | Former FP count |",
            "| --- | --- | ---: | ---: | ---: |",
        ]
        for row in spike_rows:
            lines.append(f"| {row['injection_mode']} | {row['phase']} | "
                         f"{row['expected_observable_samples']} | {row['detected_samples']} | "
                         f"{row['previous_SPIKE_FP_samples']} |")
        lines += [
            "",
            f"OFFSET is a non-target blind-spot probe. The constant +4 °C plateau has no dedicated OFFSET detector; SPIKE was emitted on {offset_entry['detected_samples'] if offset_entry else 0}/{len(offset_episodes)} entry boundaries and {offset_exit['detected_samples'] if offset_exit else 0}/{len(offset_episodes)} removal boundaries. The stable bias itself is not identified.",
            "",
            "Sample-level recall for STUCK, DRIFT and MISSING includes the intentional interval after fault onset but before the detector's confirmation condition is met. With `missing_limit=3`, the first two invalid samples are observable MISSING truth and count as sample-level FN; this is not an episode miss.",
            "",
            "The clean baseline had 0 observed false-positive samples in 1,810 samples over 30m09s. Its observed sample rate was 0.000%; this does not establish a universal zero false-positive rate.",
            "",
            f"Hardware capacity: physical flash {flash_mib:g} MiB (`esptool.py flash_id`); firmware-configured capacity {flash_cfg_mib:g} MiB, image header {metadata['flash_image_header_bytes'] // (1024 * 1024)} MiB, and firmware-reported size {metadata['flash_firmware_reported_bytes'] // (1024 * 1024)} MiB. ESP32-S3 chip/package identification reports {psram_mib:g} MiB embedded PSRAM (not a memory test); PSRAM is disabled in this firmware and runtime available PSRAM is {metadata['psram_runtime_available_bytes']} bytes.",
            "",
            "Figures (generated from the CSV/JSON evidence):",
            "",
            "![Physical SHT30 raw and injected values, active intervals and detector events](results/v0.2/plots/fault_timeline.png)",
            "",
            "![One detection-latency point per episode and the median](results/v0.2/plots/detection_latency.png)",
            "",
            "![Episode recall and per-sample precision, recall and F1](results/v0.2/plots/detection_performance.png)",
            "",
            "![Clean-baseline observed false-positive summary](results/v0.2/plots/baseline_false_positives.png)",
            "",
            f"Measurement firmware commit `{meta_commit}` (`git_dirty=false`); config SHA-256 `{config_hash}`; compiler `{compiler}`. Raw log `{raw_log}`, SHA-256 `{raw_hash}`.",
            "Evidence files: [sample_results.csv](results/v0.2/sample_results.csv), [fault_episodes.csv](results/v0.2/fault_episodes.csv), [metrics_per_fault.csv](results/v0.2/metrics_per_fault.csv), [cross_fault_detections.csv](results/v0.2/cross_fault_detections.csv), [hardware_metadata.json](results/v0.2/hardware_metadata.json).",
            "SHT30 `temperature_C` was evaluated; `humidity_percent` is read by the driver but was not evaluated; BH1750 was not tested.",
        ]
    return "\n".join([START, *lines, END])


def update(path: Path, block: str) -> None:
    text = path.read_text(encoding="utf-8")
    if START in text and END in text:
        start = text.index(START)
        end = text.index(END, start) + len(END)
        text = text[:start] + block + text[end:]
    else:
        insertion = text.find("\n\n", text.find("\n\n", text.find("# ") + 2) + 2)
        text = text[:insertion + 2] + block + "\n\n" + text[insertion + 2:]
    path.write_text(text, encoding="utf-8")


def make_results_index() -> str:
    (baseline, metrics, episodes, _samples, metadata, cross, offset_episodes,
     offset_entry, offset_exit) = common_data()
    duration_min, duration_sec = divmod(baseline["duration_ms"] // 1000, 60)
    old_spike_rows, old_total = spike_reclassification(cross)
    spike_rows = spike_co_detections(cross)
    former_spike_detail = former_spike_summary(old_spike_rows)
    oor_entry = next((row for row in cross if row["injection_mode"] == "OUT_OF_RANGE" and
                      row["phase"] == "INJECTION" and row["detected_fault"] == "SPIKE"), None)
    psram_mib = metadata["psram_physical_bytes"] / (1024 * 1024)
    lines = [
        "# v0.2 physical evidence",
        "",
        "This directory contains the ESP32-S3/SHT30 `temperature_C` evaluation. The immutable `raw/` serial log is the source for parsed samples and detector outputs. The measurement used the v0.1 C detector without semantic changes.",
        "",
        f"Clean baseline: {baseline['samples']:,} samples / {duration_min}m{duration_sec:02d}; {baseline['false_positive_samples']} observed false-positive samples. This single observation does not establish a universal zero false-positive rate.",
        "",
        "Five target fault types × five episodes produced 25/25 detected target episodes. OFFSET is a non-target blind-spot probe.",
        "",
        "| Fault | Episodes | Detected | Episode recall | Median detection | Observed range | Median recovery |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for fault in ORDER:
        row = metrics[fault]
        lines.append(f"| {fault} | {row['episodes']} | {row['episodes_detected']} | "
                     f"{float(row['episode_recall']):.0%} | "
                     f"{ms(row['median_detection_latency_ms'])} | "
                     f"{latency_range(episodes, fault)} | "
                     f"{ms(row['median_recovery_latency_ms'])} |")
    lines += [
        "",
        "## Evaluation semantics",
        "",
        "`injection_mode` records causal labels; `expected_observable_flags` is generated independently from the injected signal, validity, schedule, frozen configuration and published v0.1 detector definitions. Truth generation never reads detector outputs. Labels are multilabel, so RANGE and SPIKE may both be true for one sample.",
        "",
        f"The old {old_total} SPIKE false positives were observable transitions: {former_spike_detail}. "
        f"The old evaluator excluded {oor_entry['expected_observable_samples'] if oor_entry else 0} RANGE+SPIKE samples at OUT_OF_RANGE entry. Corrected SPIKE sample metrics: TP {metrics['SPIKE']['sample_tp']}, FP {metrics['SPIKE']['sample_fp']}, FN {metrics['SPIKE']['sample_fn']}. The formal trace has no genuine SPIKE false positives.",
        "",
        "| Cause | Phase | Expected SPIKE samples | Detected | Former FP |",
        "| --- | --- | ---: | ---: | ---: |",
    ]
    for row in spike_rows:
        lines.append(f"| {row['injection_mode']} | {row['phase']} | "
                     f"{row['expected_observable_samples']} | {row['detected_samples']} | "
                     f"{row['previous_SPIKE_FP_samples']} |")
    lines += [
        "",
        "| Fault | TP | FP | TN | FN | Precision | Recall | F1 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for fault in ORDER:
        row = metrics[fault]
        lines.append(f"| {fault} | {row['sample_tp']} | {row['sample_fp']} | {row['sample_tn']} | "
                     f"{row['sample_fn']} | {ratio(row['precision'])} | "
                     f"{ratio(row['recall'])} | {ratio(row['f1'])} |")
    lines += [
        "",
        "Sample-level recall for STUCK, DRIFT and MISSING includes the intentional pre-confirmation interval. In particular, the first two invalid samples with `missing_limit=3` remain sample-level FN, not episode misses.",
        "",
        f"OFFSET is excluded from target recall. Its constant +4 °C plateau has no dedicated detector; SPIKE was emitted on {offset_entry['detected_samples'] if offset_entry else 0}/{len(offset_episodes)} entry and {offset_exit['detected_samples'] if offset_exit else 0}/{len(offset_episodes)} removal boundaries.",
        "",
        "## Hardware metadata",
        "",
        f"Physical flash: {metadata['flash_physical_bytes'] // (1024 * 1024)} MiB by `esptool.py flash_id`; configured and image-header size: {metadata['flash_configured_bytes'] // (1024 * 1024)} MiB; firmware-reported flash: {metadata['flash_firmware_reported_bytes'] // (1024 * 1024)} MiB. Chip/package identification reports {psram_mib:g} MiB embedded PSRAM (not a RAM test); firmware PSRAM enabled: {metadata['psram_enabled']}; runtime available: {metadata['psram_runtime_available_bytes']} bytes.",
        "",
        f"Measurement commit: `{metadata['git_commit']}` (`git_dirty=false`).",
        f"Config SHA-256: `{metadata['config_sha256']}`; compiler: `{metadata.get('toolchain', {}).get('compiler_version', 'not recorded')}`.",
        f"Raw log: [`{metadata['raw_log']}`]({metadata['raw_log']}); SHA-256: `{metadata['raw_log_sha256']}`.",
        "",
        "`sample_results.csv` preserves physical and injected values, causal labels, independent observable labels and detector output. `fault_episodes.csv` reports episode detection and recovery latency; `metrics_per_fault.csv` reports the full per-sample confusion counts; `cross_fault_detections.csv` maps causal injection modes to co-detected observable labels; `hardware_metadata.json` and `hardware_probe.json` record firmware and independently probed capacity evidence.",
        "",
        "The detection latency plot shows each of the five episode values with a median marker; it does not emphasize p95 for n=5. The timeline plot shows physical and injected traces, active intervals, first target detections and both SPIKE excursion/return markers. The clean baseline represents one observation and must not be generalized to other sensors, rooms or sample rates.",
        "",
        "Figures: [fault timeline](plots/fault_timeline.png), [detection latency](plots/detection_latency.png), [detection performance](plots/detection_performance.png), [baseline false positives](plots/baseline_false_positives.png).",
        "",
        "Humidity was not evaluated; BH1750 was not tested. `results/scenarios.csv` is separate v0.1 synthetic evidence.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    update(ROOT / "README.md", make_block(chinese=False))
    update(ROOT / "README.zh-CN.md", make_block(chinese=True))
    (RESULTS / "README.md").write_text(make_results_index(), encoding="utf-8")
    print("Rendered README results from results/v0.2 evidence")


if __name__ == "__main__":
    main()
