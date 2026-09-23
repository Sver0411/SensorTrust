#ifndef FAULT_INJECTOR_H
#define FAULT_INJECTOR_H

#include "sensor_trust.h"

typedef enum {
    INJECT_PASS = 0,
    INJECT_FREEZE,
    INJECT_SPIKE,
    INJECT_OFFSET,
    INJECT_DRIFT,
    INJECT_DROP,
    INJECT_OUT_OF_RANGE
} fault_injection_mode_t;

typedef struct {
    float spike_amplitude;
    float offset;
    float drift_rate_per_second;
    float out_of_range_value;
} fault_injector_config_t;

typedef struct {
    fault_injection_mode_t mode;
    uint64_t start_ms;
    float freeze_value;
    bool has_freeze_value;
} fault_injector_t;

void fault_injector_start(fault_injector_t *injector,
                          fault_injection_mode_t mode,
                          const sensor_sample_t *last_real,
                          uint64_t start_ms);
sensor_sample_t fault_injector_apply(const fault_injector_t *injector,
                                     const fault_injector_config_t *config,
                                     sensor_sample_t raw);
const char *fault_injector_name(fault_injection_mode_t mode);

#endif
