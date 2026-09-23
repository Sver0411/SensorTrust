#include <assert.h>
#include <math.h>
#include <stdio.h>

#include "fault_injector.h"

int main(void)
{
    fault_injector_t injector = {0};
    fault_injector_config_t config = {10.0f, 4.0f, 0.02f, 150.0f};
    sensor_sample_t raw = {25.0f, true, 1000};
    sensor_sample_t changed;

    fault_injector_start(&injector, INJECT_PASS, &raw, 1000);
    changed = fault_injector_apply(&injector, &config, raw);
    assert(changed.value == raw.value && changed.valid == raw.valid &&
           changed.timestamp_ms == raw.timestamp_ms);

    fault_injector_start(&injector, INJECT_FREEZE, &raw, 1000);
    changed = fault_injector_apply(&injector, &config,
                                   (sensor_sample_t){26.0f, true, 2000});
    assert(changed.value == 25.0f && changed.valid && changed.timestamp_ms == 2000);
    changed = fault_injector_apply(&injector, &config,
                                   (sensor_sample_t){27.0f, true, 3000});
    assert(changed.value == 25.0f);

    fault_injector_start(&injector, INJECT_SPIKE, &raw, 1000);
    changed = fault_injector_apply(&injector, &config, raw);
    assert(changed.value == 35.0f);

    fault_injector_start(&injector, INJECT_OFFSET, &raw, 1000);
    changed = fault_injector_apply(&injector, &config, raw);
    assert(changed.value == 29.0f);

    fault_injector_start(&injector, INJECT_DRIFT, &raw, 1000);
    changed = fault_injector_apply(&injector, &config,
                                   (sensor_sample_t){25.0f, true, 3500});
    assert(fabsf(changed.value - 25.05f) < 0.0001f);

    fault_injector_start(&injector, INJECT_DROP, &raw, 1000);
    changed = fault_injector_apply(&injector, &config, raw);
    assert(!changed.valid && changed.value == raw.value);

    fault_injector_start(&injector, INJECT_OUT_OF_RANGE, &raw, 1000);
    changed = fault_injector_apply(&injector, &config, raw);
    assert(changed.valid && changed.value == 150.0f);

    /* Physical I2C failure cannot be replaced with a fabricated value. */
    changed = fault_injector_apply(&injector, &config,
                                   (sensor_sample_t){0.0f, false, 2000});
    assert(!changed.valid);
    puts("7 injection modes and invalid-read preservation passed");
    return 0;
}
