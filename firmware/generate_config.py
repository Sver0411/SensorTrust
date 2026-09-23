"""Generate the firmware's C constants from the reviewed experiment manifest."""
import json
import sys
from pathlib import Path


def main() -> None:
    source = Path(sys.argv[1])
    config = json.loads(source.read_text(encoding="utf-8"))
    trust = config["sensor_trust"]
    injection = config["injection"]
    lines = ["/* Generated from experiment_config.json; do not edit. */", "#pragma once"]
    for key in ("experiment_id", "sensor", "channel"):
        lines.append(f'#define EXP_{key.upper()} "{config[key]}"')
    for key in ("sensor_address", "sda_gpio", "scl_gpio", "sample_interval_ms",
                "baseline_samples", "repetitions", "recovery_samples", "seed"):
        lines.append(f"#define EXP_{key.upper()} {config[key]}")
    for key, value in trust.items():
        suffix = "f" if isinstance(value, float) else ""
        lines.append(f"#define EXP_TRUST_{key.upper()} {value}{suffix}")
    for mode, settings in injection.items():
        for key, value in settings.items():
            suffix = "f" if isinstance(value, float) else ""
            lines.append(f"#define EXP_{mode}_{key.upper()} {value}{suffix}")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
