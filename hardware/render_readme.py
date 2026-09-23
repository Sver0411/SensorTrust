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


def ms(value: str) -> str:
    if not value or value == "N/A":
        return "N/A"
    return f"{float(value):,.0f} ms"


def ratio(value: str) -> str:
    return "N/A" if not value or value == "N/A" else f"{float(value):.3f}"


def make_block(*, chinese: bool) -> str:
    baseline = json.loads((RESULTS / "clean_baseline.json").read_text(encoding="utf-8"))
    metrics = {row["fault"]: row for row in csv_rows("metrics_per_fault.csv")}
    episodes = csv_rows("fault_episodes.csv")
    metadata = json.loads((RESULTS / "hardware_metadata.json").read_text(encoding="utf-8"))
    offset = [row for row in episodes if row["fault"] == "OFFSET"]
    offset_samples = csv_rows("sample_results.csv")
    offset_active = [row for row in offset_samples if row["injection"] == "OFFSET" and
                     row["fault_active"] == "True"]
    offset_spikes = sum(bool(int(row["detected_flags"]) & 4) for row in offset_active)
    duration_min, duration_sec = divmod(baseline["duration_ms"] // 1000, 60)
    rate = baseline["false_positive_rate"] * 100
    meta_commit = metadata["git_commit"]
    config_hash = metadata["config_sha256"]
    raw_log = metadata["raw_log"]
    raw_hash = metadata["raw_log_sha256"]
    compiler = metadata.get("toolchain", {})
    compiler_text = compiler.get("compiler_version", "not recorded")

    if chinese:
        lines = [
            "## v0.2 真机评估（由原始串口日志生成）",
            "",
            f"正常真实数据基线：{duration_min} 分 {duration_sec} 秒，{baseline['samples']:,} 个样本；"
            f"误报样本 {baseline['false_positive_samples']}，误报率 {rate:.3f}%；"
            f"物理读取失败 {baseline['raw_read_failures']}。采样间隔为实验室 1 Hz。",
            "",
            "| 故障 | 注入轮数 | 检出轮数 | 轮次召回率 | 中位检测延迟 | p95 检测延迟 | 中位恢复延迟 |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
        for fault in ORDER:
            row = metrics[fault]
            lines.append(f"| {fault} | {row['episodes']} | {row['episodes_detected']} | "
                         f"{float(row['episode_recall']):.0%} | "
                         f"{ms(row['median_detection_latency_ms'])} | "
                         f"{ms(row['p95_detection_latency_ms'])} | "
                         f"{ms(row['median_recovery_latency_ms'])} |")
        lines += [
            "",
            "逐样本指标（确认窗口尚未满足的故障样本会计为 FN）：",
            "",
            "| 故障 | TP | FP | FN | Precision | Recall | F1 |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
        for fault in ORDER:
            row = metrics[fault]
            lines.append(f"| {fault} | {row['sample_tp']} | {row['sample_fp']} | "
                         f"{row['sample_fn']} | {ratio(row['precision'])} | "
                         f"{ratio(row['recall'])} | {ratio(row['f1'])} |")
        lines += [
            "",
            "基线各故障标志误报数：" + "，".join(
                f"{fault} {baseline['false_positive_by_fault'][fault]}" for fault in ORDER) + "。",
            "SPIKE 的 25 个逐样本 FP 出现在注入/恢复边界；其中稳定 OFFSET 注入开始的 5 个样本产生瞬态 SPIKE。正常基线没有 SPIKE 误报。",
            f"OFFSET 盲点探针：{len(offset)} 轮，恒定偏移持续期间没有 OFFSET 检测器；"
            f"偏移开始时 {offset_spikes}/{len(offset_active)} 个样本触发 SPIKE。"
            "偏移撤除会产生反向瞬态；稳定偏移本身不被单通道检测器识别。",
            "",
            "五类检测器的 episode recall 都是 5/5，但这不等于逐样本无漏报。"
            "FREEZE、DRIFT、DROP 需要确认窗口，所以各自存在窗口期 FN；"
            "SPIKE 的逐样本 precision 也低于 episode recall。稳定基线为零误报仅适用于本次 SHT30 环境和约 30 分钟观察。",
            "",
            "图表（均由 `results/v0.2/` CSV 生成）：",
            "",
            "![真机数据上的各注入模式时间序列](results/v0.2/plots/fault_timeline.png)",
            "",
            "![各轮故障检测延迟](results/v0.2/plots/detection_latency.png)",
            "",
            "![轮次召回率与逐样本 precision](results/v0.2/plots/detection_performance.png)",
            "",
            "![正常基线误报数](results/v0.2/plots/baseline_false_positives.png)",
            "",
            f"测量固件 commit `{meta_commit}`（git_dirty=false）；配置 SHA-256 `{config_hash}`；"
            f"编译器 `{compiler_text}`。原始日志：`{raw_log}`（SHA-256 `{raw_hash}`）。",
            "SHT30 temperature_C 已评估；humidity_percent 仅由驱动读取、未评估；BH1750 未测试。",
        ]
    else:
        lines = [
            "## v0.2 physical evaluation (generated from the raw serial log)",
            "",
            f"Clean physical baseline: {duration_min} min {duration_sec} s, "
            f"{baseline['samples']:,} samples; {baseline['false_positive_samples']} false-positive "
            f"samples ({rate:.3f}%); {baseline['raw_read_failures']} physical read failures. "
            "Sampling was 1 Hz in this laboratory experiment.",
            "",
            "| Fault | Episodes | Detected | Episode recall | Median detection | p95 detection | Median recovery |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
        for fault in ORDER:
            row = metrics[fault]
            lines.append(f"| {fault} | {row['episodes']} | {row['episodes_detected']} | "
                         f"{float(row['episode_recall']):.0%} | "
                         f"{ms(row['median_detection_latency_ms'])} | "
                         f"{ms(row['p95_detection_latency_ms'])} | "
                         f"{ms(row['median_recovery_latency_ms'])} |")
        lines += [
            "",
            "Raw sample metrics (confirmation-window misses count as FN):",
            "",
            "| Fault | TP | FP | FN | Precision | Recall | F1 |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
        for fault in ORDER:
            row = metrics[fault]
            lines.append(f"| {fault} | {row['sample_tp']} | {row['sample_fp']} | "
                         f"{row['sample_fn']} | {ratio(row['precision'])} | "
                         f"{ratio(row['recall'])} | {ratio(row['f1'])} |")
        lines += [
            "",
            "Baseline false positives by detector: " + ", ".join(
                f"{fault} {baseline['false_positive_by_fault'][fault]}" for fault in ORDER) + ".",
            "SPIKE's 25 raw sample false positives occur at injection/recovery boundaries; "
            "five appear when the OFFSET probe begins. The clean baseline had no SPIKE flags.",
            f"OFFSET blind-spot probe: {len(offset)} episodes. There is no OFFSET detector; "
            f"SPIKE appeared on {offset_spikes}/{len(offset_active)} active biased samples, "
            "at the transition into the bias. Removing the bias creates a reverse transient; "
            "the sustained constant offset itself is not identified by this single-channel detector.",
            "",
            "All five detector types reached 5/5 episode recall, but that does not mean zero "
            "sample-level misses. FREEZE, DRIFT and DROP have confirmation-window false negatives; "
            "SPIKE sample precision is also lower than episode recall. Zero false positives on this "
            "baseline applies only to this SHT30 environment and roughly 30-minute observation.",
            "",
            "Figures (generated from the `results/v0.2/` CSV evidence):",
            "",
            "![Physical traces under each injection](results/v0.2/plots/fault_timeline.png)",
            "",
            "![Detection latency by episode](results/v0.2/plots/detection_latency.png)",
            "",
            "![Episode recall and sample precision](results/v0.2/plots/detection_performance.png)",
            "",
            "![Clean baseline false positives](results/v0.2/plots/baseline_false_positives.png)",
            "",
            f"Measurement firmware commit `{meta_commit}` (`git_dirty=false`); config SHA-256 "
            f"`{config_hash}`; compiler `{compiler_text}`. Raw log: `{raw_log}` "
            f"(SHA-256 `{raw_hash}`).",
            "SHT30 temperature_C was evaluated; humidity_percent is read by the driver but was not evaluated; BH1750 was not tested.",
        ]
    return "\n".join([START, *lines, END])


def update(path: Path, block: str) -> None:
    text = path.read_text(encoding="utf-8")
    if START in text and END in text:
        start = text.index(START)
        end = text.index(END, start) + len(END)
        text = text[:start] + block + text[end:]
    else:
        insertion = text.find("\n\n", text.find("\n\n", text.find("# ")) + 2)
        text = text[:insertion + 2] + block + "\n\n" + text[insertion + 2:]
    path.write_text(text, encoding="utf-8")


def make_results_index() -> str:
    baseline = json.loads((RESULTS / "clean_baseline.json").read_text(encoding="utf-8"))
    metrics = {row["fault"]: row for row in csv_rows("metrics_per_fault.csv")}
    metadata = json.loads((RESULTS / "hardware_metadata.json").read_text(encoding="utf-8"))
    duration_min, duration_sec = divmod(baseline["duration_ms"] // 1000, 60)
    lines = [
        "# v0.2 physical evidence",
        "",
        "This directory contains the committed ESP32-S3/SHT30 temperature evaluation. "
        "The immutable `raw/` serial log is the source for the parsed CSV and JSON files; "
        "the measurement firmware used the v0.1 C detector without semantic changes.",
        "",
        f"Clean baseline: {duration_min} min {duration_sec} s, {baseline['samples']:,} samples, "
        f"{baseline['false_positive_samples']} false-positive samples, "
        f"{baseline['raw_read_failures']} physical read failures.",
        "",
        "| Fault | Detected episodes | Recall | Median detection | p95 detection | Median recovery | Sample FP | Sample FN |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for fault in ORDER:
        row = metrics[fault]
        lines.append(f"| {fault} | {row['episodes_detected']}/{row['episodes']} | "
                     f"{float(row['episode_recall']):.0%} | "
                     f"{ms(row['median_detection_latency_ms'])} | "
                     f"{ms(row['p95_detection_latency_ms'])} | "
                     f"{ms(row['median_recovery_latency_ms'])} | "
                     f"{row['sample_fp']} | {row['sample_fn']} |")
    lines += [
        "",
        "OFFSET was a blind-spot probe, not a detector target. The sustained bias produced no OFFSET verdict; its entry and exit caused transient SPIKE flags. The core/API semantics were not changed after the run.",
        "",
        f"Measurement commit: `{metadata['git_commit']}` (`git_dirty=false`).",
        f"Config SHA-256: `{metadata['config_sha256']}`.",
        f"Compiler: `{metadata.get('toolchain', {}).get('compiler_version', 'not recorded')}`.",
        f"Raw log SHA-256: `{metadata['raw_log_sha256']}`.",
        "",
        "`sample_results.csv` preserves each physical and injected value with sample ground truth. "
        "`fault_episodes.csv` records event latency and recovery; `metrics_per_fault.csv` reports "
        "raw per-sample confusion counts separately from episode recall. `hardware_metadata.json` "
        "records board, wiring, detector configuration and provenance.",
        "",
        "The clean baseline is one roughly 30-minute observation from this board/environment. "
        "Humidity was not evaluated; BH1750 was not tested. Do not generalize this result to other "
        "sensors, rooms or deployment sample rates.",
        "",
        "`results/scenarios.csv` is separate v0.1 synthetic evidence.",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    update(ROOT / "README.md", make_block(chinese=False))
    update(ROOT / "README.zh-CN.md", make_block(chinese=True))
    (RESULTS / "README.md").write_text(make_results_index(), encoding="utf-8")
    print("Rendered README results from results/v0.2 evidence")


if __name__ == "__main__":
    main()
