#include "fault_injector.h"

#include <stddef.h>

void fault_injector_start(fault_injector_t *injector,
                          fault_injection_mode_t mode,
                          const sensor_sample_t *last_real,
                          uint64_t start_ms)
{
    if (injector == NULL) {
        return;
    }
    injector->mode = mode;
    injector->start_ms = start_ms;
    injector->has_freeze_value = last_real != NULL && last_real->valid;
    injector->freeze_value = injector->has_freeze_value ? last_real->value : 0.0f;
}

sensor_sample_t fault_injector_apply(const fault_injector_t *injector,
                                     const fault_injector_config_t *config,
                                     sensor_sample_t raw)
{
    if (injector == NULL || config == NULL || injector->mode == INJECT_PASS) {
        return raw;
    }
    if (injector->mode == INJECT_DROP) {
        raw.valid = false;
        return raw;
    }
    /* A failed physical read remains a failed read. No generated reading may
     * substitute for missing hardware during a formal experiment. */
    if (!raw.valid) {
        return raw;
    }
    switch (injector->mode) {
    case INJECT_FREEZE:
        if (injector->has_freeze_value) {
            raw.value = injector->freeze_value;
        } else {
            raw.valid = false;
        }
        break;
    case INJECT_SPIKE:
        raw.value += config->spike_amplitude;
        break;
    case INJECT_OFFSET:
        raw.value += config->offset;
        break;
    case INJECT_DRIFT:
        if (raw.timestamp_ms >= injector->start_ms) {
            raw.value += config->drift_rate_per_second *
                         ((float)(raw.timestamp_ms - injector->start_ms) / 1000.0f);
        }
        break;
    case INJECT_OUT_OF_RANGE:
        raw.value = config->out_of_range_value;
        break;
    case INJECT_PASS:
    case INJECT_DROP:
        break;
    }
    return raw;
}

const char *fault_injector_name(fault_injection_mode_t mode)
{
    switch (mode) {
    case INJECT_PASS: return "PASS";
    case INJECT_FREEZE: return "FREEZE";
    case INJECT_SPIKE: return "SPIKE";
    case INJECT_OFFSET: return "OFFSET";
    case INJECT_DRIFT: return "DRIFT";
    case INJECT_DROP: return "DROP";
    case INJECT_OUT_OF_RANGE: return "OUT_OF_RANGE";
    }
    return "UNKNOWN";
}
