"""Generate four compact figures from the parsed physical-board CSV evidence."""
from __future__ import annotations

import csv
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "results" / "v0.2"
PLOTS = RESULTS / "plots"
BITS = {"RANGE": 1, "STUCK": 2, "SPIKE": 4, "DRIFT": 8, "MISSING": 16}
MODES = (("FREEZE", "STUCK"), ("SPIKE", "SPIKE"), ("DRIFT", "DRIFT"),
         ("DROP", "MISSING"), ("OUT_OF_RANGE", "RANGE"), ("OFFSET", "SPIKE"))


def read_csv(name: str) -> list[dict[str, str]]:
    with (RESULTS / name).open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def timeline(samples: list[dict[str, str]]) -> None:
    fig, axes = plt.subplots(3, 2, figsize=(12, 9), sharex=False)
    for axis, (mode, target) in zip(axes.flat, MODES):
        active = [row for row in samples if row["injection"] == mode and
                  row["fault_active"] == "True" and row["run"] == "1"]
        start = int(active[0]["timestamp_ms"])
        end_index = int(active[-1]["sample_index"]) + 11
        rows = [row for row in samples if row["run"] == "1" and
                int(row["sample_index"]) >= int(active[0]["sample_index"]) and
                int(row["sample_index"]) <= end_index]
        x = [(int(row["timestamp_ms"]) - start) / 1000 for row in rows]
        raw = [float(row["raw_value"]) if row["raw_valid"] == "True" else np.nan
               for row in rows]
        injected = [float(row["injected_value"]) if row["injected_valid"] == "True" else np.nan
                    for row in rows]
        axis.plot(x, raw, color="#687386", linewidth=1.2, label="physical raw")
        axis.plot(x, injected, color="#1769aa", linewidth=1.5, label="injected")
        active_end = (int(active[-1]["timestamp_ms"]) - start) / 1000
        axis.axvspan(0, active_end, color="#f4a340", alpha=0.18, label="fault active")
        if mode == "OFFSET":
            flag_label = "SPIKE (transition probe)"
        else:
            flag_label = f"{target} flag"
        flagged = [i for i, row in enumerate(rows)
                   if int(row["detected_flags"]) & BITS[target]]
        flag_values = [injected[i] if np.isfinite(injected[i]) else raw[i]
                       for i in flagged]
        axis.scatter([x[i] for i in flagged], flag_values, marker="x", s=45,
                     linewidths=1.5, color="#c33b33", label=flag_label, zorder=4)
        axis.set_title("OFFSET: transient SPIKE only" if mode == "OFFSET" else mode)
        axis.set_ylabel("Temperature (°C)")
        axis.grid(True, alpha=0.25)
    for axis in axes[-1, :]:
        axis.set_xlabel("Seconds from injection start")
    handles, labels = [], []
    seen = set()
    for axis in axes.flat:
        axis_handles, axis_labels = axis.get_legend_handles_labels()
        for handle, label in zip(axis_handles, axis_labels):
            if label not in seen:
                handles.append(handle)
                labels.append(label)
                seen.add(label)
    fig.legend(handles, labels, loc="upper center", ncol=4,
               frameon=False, bbox_to_anchor=(0.5, 0.995))
    fig.suptitle("Physical SHT30 traces through deterministic injection", y=1.03,
                 fontsize=14)
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    fig.savefig(PLOTS / "fault_timeline.png", dpi=180, bbox_inches="tight")
    plt.close(fig)


def latency(episode_rows: list[dict[str, str]]) -> None:
    faults = list(BITS)
    fig, axis = plt.subplots(figsize=(9, 4.8))
    values = []
    for index, fault in enumerate(faults, 1):
        latencies = [float(row["detection_latency_ms"]) / 1000
                     for row in episode_rows if row["fault"] == fault and
                     row["detection_latency_ms"] not in ("", "N/A")]
        values.extend(latencies)
        jitter = np.linspace(-0.08, 0.08, len(latencies)) if len(latencies) > 1 else [0]
        axis.scatter(index + jitter, latencies, color="#1769aa", s=38, alpha=0.85)
        if latencies:
            median = float(np.median(latencies))
            axis.plot([index - 0.2, index + 0.2], [median, median],
                      color="#c33b33", linewidth=2)
    axis.set_xticks(range(1, len(faults) + 1), faults)
    axis.set_ylabel("Detection latency (s)")
    axis.set_title("Detection latency by injected episode (n = 5 each)")
    axis.grid(axis="y", alpha=0.25)
    fig.tight_layout()
    fig.savefig(PLOTS / "detection_latency.png", dpi=180, bbox_inches="tight")
    plt.close(fig)


def performance(metric_rows: list[dict[str, str]]) -> None:
    faults = [row["fault"] for row in metric_rows]
    episode_recall = [float(row["episode_recall"]) for row in metric_rows]
    precision = [float(row["precision"]) for row in metric_rows]
    x = np.arange(len(faults))
    fig, axis = plt.subplots(figsize=(9, 4.8))
    width = 0.36
    axis.bar(x - width / 2, episode_recall, width, label="episode recall",
             color="#1769aa")
    axis.bar(x + width / 2, precision, width, label="sample precision",
             color="#e48b32")
    axis.set_xticks(x, faults)
    axis.set_ylim(0, 1.08)
    axis.set_ylabel("Score")
    axis.set_title("Episode-level detection and raw sample precision")
    axis.legend(frameon=False)
    axis.grid(axis="y", alpha=0.25)
    fig.tight_layout()
    fig.savefig(PLOTS / "detection_performance.png", dpi=180, bbox_inches="tight")
    plt.close(fig)


def baseline_false_positives(baseline: dict) -> None:
    faults = list(BITS)
    counts = [baseline["false_positive_by_fault"][fault] for fault in faults]
    fig, axis = plt.subplots(figsize=(8.5, 4.4))
    bars = axis.bar(faults, counts, color="#5b9a73")
    axis.bar_label(bars, padding=3, fmt="%d")
    axis.set_ylim(0, max(counts, default=0) + 1)
    axis.set_ylabel("False-positive samples")
    axis.set_title(f"Clean physical baseline: {baseline['samples']:,} samples, "
                   f"{baseline['false_positive_samples']} flagged samples")
    axis.grid(axis="y", alpha=0.25)
    fig.tight_layout()
    fig.savefig(PLOTS / "baseline_false_positives.png", dpi=180, bbox_inches="tight")
    plt.close(fig)


def main() -> None:
    PLOTS.mkdir(parents=True, exist_ok=True)
    timeline(read_csv("sample_results.csv"))
    latency(read_csv("fault_episodes.csv"))
    performance(read_csv("metrics_per_fault.csv"))
    import json
    baseline = json.loads((RESULTS / "clean_baseline.json").read_text(encoding="utf-8"))
    baseline_false_positives(baseline)
    print(f"Wrote four figures to {PLOTS.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
