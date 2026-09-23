#include "experiment.h"

#include <inttypes.h>
#include <stdio.h>

#include "esp_chip_info.h"
#include "esp_flash.h"
#include "esp_heap_caps.h"
#include "esp_idf_version.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_timer.h"
#include "experiment_config.h"
#include "fault_injector.h"
#include "sensor_reader.h"
#include "sensor_trust.h"

typedef struct {
    fault_injection_mode_t mode;
    int duration;
    const char *expected;
} episode_plan_t;

static const episode_plan_t plans[] = {
    {INJECT_FREEZE, EXP_FREEZE_DURATION_SAMPLES, "STUCK"},
    {INJECT_SPIKE, EXP_SPIKE_DURATION_SAMPLES, "SPIKE"},
    {INJECT_DRIFT, EXP_DRIFT_DURATION_SAMPLES, "DRIFT"},
    {INJECT_DROP, EXP_DROP_DURATION_SAMPLES, "MISSING"},
    {INJECT_OUT_OF_RANGE, EXP_OUT_OF_RANGE_DURATION_SAMPLES, "RANGE"},
    {INJECT_OFFSET, EXP_OFFSET_DURATION_SAMPLES, "NONE"},
};

static sensor_trust_t trust_ctx;

static sensor_trust_config_t trust_config(void)
{
    sensor_trust_config_t config = {
        .min_value = EXP_TRUST_MIN_VALUE,
        .max_value = EXP_TRUST_MAX_VALUE,
        .stuck_epsilon = EXP_TRUST_STUCK_EPSILON,
        .stuck_window = EXP_TRUST_STUCK_WINDOW,
        .spike_threshold = EXP_TRUST_SPIKE_THRESHOLD,
        .drift_window = EXP_TRUST_DRIFT_WINDOW,
        .drift_threshold = EXP_TRUST_DRIFT_THRESHOLD,
        .missing_limit = EXP_TRUST_MISSING_LIMIT,
    };
    return config;
}

static fault_injector_config_t injection_config(void)
{
    fault_injector_config_t config = {
        .spike_amplitude = EXP_SPIKE_AMPLITUDE,
        .offset = EXP_OFFSET_OFFSET,
        .drift_rate_per_second = EXP_DRIFT_RATE_PER_SECOND,
        .out_of_range_value = EXP_OUT_OF_RANGE_VALUE,
    };
    return config;
}

static void print_begin(void)
{
    esp_chip_info_t chip;
    uint32_t flash_bytes = 0;
    esp_chip_info(&chip);
    (void)esp_flash_get_size(NULL, &flash_bytes);
    printf("ST_BEGIN {\"schema\":1,\"experiment_id\":\"%s\","
           "\"git_commit\":\"%s\",\"git_dirty\":%s,"
           "\"config_sha256\":\"%s\",\"chip\":\"ESP32-S3\","
           "\"chip_revision\":%u,\"flash_bytes\":%" PRIu32 ","
           "\"psram_bytes\":%u,\"esp_idf\":\"%s\","
           "\"sensor\":\"%s\",\"channel\":\"%s\","
           "\"sensor_address\":%d,\"sda_gpio\":%d,\"scl_gpio\":%d,"
           "\"sample_interval_ms\":%d,\"baseline_samples\":%d,"
           "\"repetitions\":%d,\"recovery_samples\":%d,\"seed\":%d,"
           "\"start_monotonic_ms\":%" PRIu64 ","
           "\"config\":{\"min_value\":%.6f,\"max_value\":%.6f,"
           "\"stuck_epsilon\":%.6f,\"stuck_window\":%d,"
           "\"spike_threshold\":%.6f,\"drift_window\":%d,"
           "\"drift_threshold\":%.6f,\"missing_limit\":%d}}\n",
           EXP_EXPERIMENT_ID, ST_GIT_COMMIT, ST_GIT_DIRTY ? "true" : "false",
           ST_CONFIG_SHA256, (unsigned)chip.revision, flash_bytes,
           (unsigned)heap_caps_get_total_size(MALLOC_CAP_SPIRAM), esp_get_idf_version(),
           EXP_SENSOR, EXP_CHANNEL, EXP_SENSOR_ADDRESS, EXP_SDA_GPIO,
           EXP_SCL_GPIO, EXP_SAMPLE_INTERVAL_MS, EXP_BASELINE_SAMPLES,
           EXP_REPETITIONS, EXP_RECOVERY_SAMPLES, EXP_SEED,
           (uint64_t)(esp_timer_get_time() / 1000),
           (double)EXP_TRUST_MIN_VALUE, (double)EXP_TRUST_MAX_VALUE,
           (double)EXP_TRUST_STUCK_EPSILON, EXP_TRUST_STUCK_WINDOW,
           (double)EXP_TRUST_SPIKE_THRESHOLD, EXP_TRUST_DRIFT_WINDOW,
           (double)EXP_TRUST_DRIFT_THRESHOLD, EXP_TRUST_MISSING_LIMIT);
}

static void print_sample(int index, const char *phase, int run,
                         const episode_plan_t *plan, sensor_sample_t raw,
                         sensor_sample_t injected, sensor_health_result_t result)
{
    char raw_text[24];
    char value_text[24];
    const char *expected = "NONE";
    const char *mode = "PASS";
    bool active = plan != NULL && phase[0] == 'I';
    if (raw.valid) {
        snprintf(raw_text, sizeof(raw_text), "%.5f", (double)raw.value);
    } else {
        snprintf(raw_text, sizeof(raw_text), "null");
    }
    if (injected.valid) {
        snprintf(value_text, sizeof(value_text), "%.5f", (double)injected.value);
    } else {
        snprintf(value_text, sizeof(value_text), "null");
    }
    if (active) {
        mode = fault_injector_name(plan->mode);
        expected = raw.valid ? plan->expected : "MISSING";
    } else if (!raw.valid) {
        expected = "MISSING";
    }
    printf("ST_SAMPLE {\"index\":%d,\"timestamp_ms\":%" PRIu64 ","
           "\"sensor\":\"%s\",\"channel\":\"%s\","
           "\"phase\":\"%s\",\"run\":%d,\"raw_value\":%s,"
           "\"raw_valid\":%s,\"injected_value\":%s,"
           "\"injected_valid\":%s,\"injection\":\"%s\","
           "\"fault_active\":%s,\"expected_fault\":\"%s\","
           "\"detected_flags\":%" PRIu32 ",\"health_score\":%d,"
           "\"state\":\"%s\"}\n",
           index, raw.timestamp_ms, EXP_SENSOR, EXP_CHANNEL, phase, run,
           raw_text, raw.valid ? "true" : "false", value_text,
           injected.valid ? "true" : "false", mode,
           active ? "true" : "false", expected, result.fault_flags,
           result.health_score, sensor_trust_state_name(result.state));
}

void experiment_run(void)
{
    sensor_trust_config_t config = trust_config();
    fault_injector_config_t inject_config = injection_config();
    fault_injector_t injector = {0};
    sensor_sample_t last_real = {0};
    TickType_t wake_tick;
    int index = 0;
    int bad_reads = 0;

    if (!sensor_trust_init(&trust_ctx, &config)) {
        printf("ST_ERROR {\"reason\":\"invalid_config\"}\n");
        return;
    }
    if (!sensor_reader_init()) {
        printf("ST_ERROR {\"reason\":\"sht30_sanity_failed\"}\n");
        return;
    }
    print_begin();
    wake_tick = xTaskGetTickCount();

    for (int group = -1; group < (int)(sizeof(plans) / sizeof(plans[0])); ++group) {
        int runs = group < 0 ? 1 : EXP_REPETITIONS;
        for (int run = 1; run <= runs; ++run) {
            int duration = group < 0 ? EXP_BASELINE_SAMPLES : plans[group].duration;
            for (int phase_index = 0; phase_index < (group < 0 ? 1 : 2); ++phase_index) {
                const char *phase = group < 0 ? "BASELINE" :
                                    phase_index == 0 ? "INJECTION" : "RECOVERY";
                const episode_plan_t *plan = group < 0 ? NULL : &plans[group];
                int count = phase_index == 0 ? duration : EXP_RECOVERY_SAMPLES;
                for (int step = 0; step < count; ++step) {
                    vTaskDelayUntil(&wake_tick, pdMS_TO_TICKS(EXP_SAMPLE_INTERVAL_MS));
                    sht30_reading_t reading = sensor_reader_read();
                    sensor_sample_t raw = {
                        .value = reading.temperature_c,
                        .valid = reading.valid,
                        .timestamp_ms = reading.timestamp_ms,
                    };
                    if (phase_index == 0 && group >= 0 && step == 0) {
                        fault_injector_start(&injector, plan->mode, &last_real,
                                             raw.timestamp_ms);
                    } else if (phase_index == 1 && step == 0) {
                        fault_injector_start(&injector, INJECT_PASS, NULL,
                                             raw.timestamp_ms);
                    }
                    sensor_sample_t injected = fault_injector_apply(&injector,
                                                                     &inject_config, raw);
                    sensor_health_result_t result = sensor_trust_update(&trust_ctx,
                                                                         &injected);
                    print_sample(index++, phase, group < 0 ? 0 : run, plan,
                                 raw, injected, result);
                    if (raw.valid) {
                        last_real = raw;
                        bad_reads = 0;
                    } else if (++bad_reads >= 10) {
                        printf("ST_END {\"status\":\"hardware_error\","
                               "\"samples\":%d}\n", index);
                        return;
                    }
                }
            }
        }
    }
    printf("ST_END {\"status\":\"complete\",\"samples\":%d,"
           "\"end_monotonic_ms\":%" PRIu64 "}\n",
           index, (uint64_t)(esp_timer_get_time() / 1000));
}
