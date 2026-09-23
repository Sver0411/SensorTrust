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
    fig, axes = plt.subplots(3, 2, figsize=(13, 10), sharex=False)
    for axis, (mode, target) in zip(axes.flat, MODES):
        active = [row for row in samples if row["injection_mode"] == mode and
                  row["fault_active"] == "True" and row["run"] == "1"]
        start = int(active[0]["timestamp_ms"])
        end_index = int(active[-1]["sample_index"]) + 11
        rows = [row for row in samples if row["run"] == "1" and
                row["episode_mode"] == mode and
                int(row["sample_index"]) >= int(active[0]["sample_index"]) and
                int(row["sample_index"]) <= end_index]
        x = [(int(row["timestamp_ms"]) - start) / 1000 for row in rows]
        raw = [float(row["raw_value"]) if row["raw_valid"] == "True" else np.nan
               for row in rows]
        injected = [float(row["injected_value"]) if row["injected_valid"] == "True" else np.nan
                    for row in rows]
        axis.plot(x, raw, color="#687386", linewidth=1.2, label="physical raw")
        axis.plot(x, injected, color="#1769aa", linewidth=1.7, label="injected")
        recovery_start = next((row for row in rows if row["phase"] == "RECOVERY"), None)
        active_end_ms = (int(recovery_start["timestamp_ms"]) if recovery_start else
                         int(active[-1]["timestamp_ms"]) + 1000)
        active_end = (active_end_ms - start) / 1000
        axis.axvspan(0, active_end, color="#f4a340", alpha=0.18,
                     label="injection active")
        axis.axvline(0, color="#252a34", linestyle="--", linewidth=1,
                     label="fault start")
        primary = [i for i, row in enumerate(rows)
                   if int(row["detected_flags"]) & BITS[target]]
        primary_values = [injected[i] if np.isfinite(injected[i]) else raw[i]
                          for i in primary]
        axis.scatter([x[i] for i in primary], primary_values, marker="x", s=42,
                     linewidths=1.5, color="#c33b33", label=f"detected {target}",
                     zorder=4)
        other = [i for i, row in enumerate(rows)
                 if int(row["detected_flags"]) & ~BITS[target]]
        other_values = [injected[i] if np.isfinite(injected[i]) else raw[i]
                        for i in other]
        if other:
            axis.scatter([x[i] for i in other], other_values, marker="D", s=30,
                         color="#7b55a0", label="other detected flag", zorder=4)
        first = next((i for i in primary if 0 <= x[i] < active_end), None)
        if mode == "OFFSET":
            delay_note = "blind-spot probe; SPIKE on entry/exit"
        elif first is not None:
            axis.axvline(x[first], color="#33845b", linestyle=":", linewidth=1.4,
                         label="first target detection")
            delay_note = f"target detection: {x[first]:.0f} s"
        else:
            delay_note = "target not detected"
        axis.set_title(f"{mode} · {delay_note}")
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
    fig.suptitle("Physical SHT30 raw/injected traces and detector events (one episode per mode)", y=1.03,
                 fontsize=14)
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    fig.savefig(PLOTS / "fault_timeline.png", dpi=180, bbox_inches="tight")
    plt.close(fig)


def latency(episode_rows: list[dict[str, str]]) -> None:
    faults = list(BITS)
    fig, axis = plt.subplots(figsize=(9, 4.8))
    for index, fault in enumerate(faults, 1):
        latencies = [float(row["detection_latency_ms"]) / 1000
                     for row in episode_rows if row["fault"] == fault and
                     row["detection_latency_ms"] not in ("", "N/A")]
        jitter = np.linspace(-0.08, 0.08, len(latencies)) if len(latencies) > 1 else [0]
        axis.scatter(index + jitter, latencies, color="#1769aa", s=38, alpha=0.85)
        if latencies:
            median = float(np.median(latencies))
            axis.plot([index - 0.2, index + 0.2], [median, median],
                      color="#c33b33", linewidth=2)
    axis.set_xticks(range(1, len(faults) + 1), faults)
    axis.set_ylabel("Detection latency (s)")
    axis.set_title("One point per injected episode; line marks the median (n = 5)")
    axis.grid(axis="y", alpha=0.25)
    fig.tight_layout()
    fig.savefig(PLOTS / "detection_latency.png", dpi=180, bbox_inches="tight")
    plt.close(fig)


def performance(metric_rows: list[dict[str, str]]) -> None:
    faults = [row["fault"] for row in metric_rows]
    detected = [int(row["episodes_detected"]) for row in metric_rows]
    episode_counts = [int(row["episodes"]) for row in metric_rows]
    ratios = {name: [float(row[name]) if row[name] not in ("", "N/A") else np.nan
                     for row in metric_rows]
              for name in ("precision", "recall", "f1")}
    x = np.arange(len(faults))
    fig, (episode_axis, sample_axis) = plt.subplots(
        1, 2, figsize=(13, 5.2), gridspec_kw={"width_ratios": [1, 1.8]})
    bars = episode_axis.bar(x, detected, color="#1769aa")
    episode_axis.bar_label(bars, labels=[f"{d}/{n}" for d, n in zip(detected, episode_counts)],
                           padding=3)
    episode_axis.set_xticks(x, faults, rotation=25, ha="right")
    episode_axis.set_ylim(0, max(episode_counts, default=5) + 0.8)
    episode_axis.set_yticks(range(0, max(episode_counts, default=5) + 1))
    episode_axis.set_ylabel("Detected episodes")
    episode_axis.set_title("Episode recall (5 runs per fault)")
    episode_axis.grid(axis="y", alpha=0.25)
    width = 0.24
    for index, (name, color) in enumerate((("precision", "#1769aa"),
                                           ("recall", "#e48b32"),
                                           ("f1", "#5b9a73"))):
        sample_axis.bar(x + (index - 1) * width, ratios[name], width,
                        label=name.capitalize(), color=color)
    sample_axis.set_xticks(x, faults)
    sample_axis.set_ylim(0, 1.08)
    sample_axis.set_ylabel("Score")
    sample_axis.set_title("Per-sample metrics")
    sample_axis.text(0.5, -0.19,
                     "Recall counts the intentional pre-confirmation interval as FN.",
                     transform=sample_axis.transAxes, ha="center", fontsize=9)
    sample_axis.legend(frameon=False, ncol=3, loc="upper center")
    sample_axis.grid(axis="y", alpha=0.25)
    fig.tight_layout()
    fig.savefig(PLOTS / "detection_performance.png", dpi=180, bbox_inches="tight")
    plt.close(fig)


def baseline_false_positives(baseline: dict) -> None:
    faults = list(BITS)
    fig, axis = plt.subplots(figsize=(9, 4.6))
    axis.axis("off")
    axis.text(0.5, 0.74,
              f"{baseline['false_positive_samples']} / {baseline['samples']:,}",
              transform=axis.transAxes, ha="center", va="center", fontsize=34,
              fontweight="bold", color="#286a46")
    axis.text(0.5, 0.56, "observed false-positive samples",
              transform=axis.transAxes, ha="center", va="center", fontsize=15)
    duration = baseline["duration_ms"]
    minutes, seconds = divmod(duration // 1000, 60)
    fig.suptitle("Clean real-sensor baseline: observed false positives")
    axis.text(0.5, 0.43,
              f"one physical SHT30 temperature baseline · {minutes}m{seconds:02d}s · 1 Hz",
              transform=axis.transAxes, ha="center", va="center", fontsize=10)
    per_fault = "     ".join(f"{fault}  {baseline['false_positive_by_fault'][fault]}"
                            for fault in faults)
    axis.text(0.5, 0.22, per_fault, transform=axis.transAxes,
              ha="center", va="center", fontsize=11, family="monospace")
    axis.text(0.5, 0.07,
              "Observed rate for this trace: 0.000%; this does not establish a universal rate.",
              transform=axis.transAxes, ha="center", va="center", fontsize=9)
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
