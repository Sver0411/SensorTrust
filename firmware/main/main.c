/*
 * SensorTrust v0.1 - ESP32 demo.
 *
 * Feeds a short synthetic sequence into the core and prints the verdict for
 * every sample, then a final summary. No real sensor is connected yet, and
 * there is no driver here: the core takes plain {value, valid, timestamp}
 * samples, so wiring up a real sensor later only means producing those.
 *
 * This file deliberately uses nothing but stdio and the core: on the ESP32
 * printf goes to the UART console, and the same file also compiles on a PC
 * (see README, "Run it"), so the output can be verified without hardware.
 */
#include <stdio.h>

#include "sensor_trust.h"

#define DEMO_SAMPLE_COUNT 60
#define DEMO_SAMPLE_INTERVAL_MS 1000

/* 30 normal readings, one 36 C jump in the middle, then the register freezes. */
static float demo_value(int index)
{
    static const float wobble[8] = {0.00f, 0.05f, 0.10f, 0.05f,
                                   0.00f, -0.05f, -0.10f, -0.05f};

    if (index == 30) {
        return 61.0f; /* a single bad reading */
    }
    if (index < 40) {
        return 25.0f + wobble[index % 8];
    }
    return 25.213f; /* the register stops moving */
}

void app_main(void)
{
    sensor_trust_config_t config;
    sensor_trust_t ctx;
    sensor_health_result_t result;
    char flag_text[48];
    int index;

    /* A temperature channel. The core knows nothing about this sensor. */
    sensor_trust_default_config(&config);
    config.min_value = -40.0f;
    config.max_value = 85.0f;

    if (!sensor_trust_init(&ctx, &config)) {
        printf("sensor_trust_init failed: configuration rejected\n");
        return;
    }

    printf("SensorTrust v0.1 demo, one temperature channel, 1 Hz\n");
    for (index = 0; index < DEMO_SAMPLE_COUNT; index++) {
        sensor_sample_t sample;

        sample.value = demo_value(index);
        sample.valid = true;
        sample.timestamp_ms = (uint64_t)(index + 1) * DEMO_SAMPLE_INTERVAL_MS;

        result = sensor_trust_update(&ctx, &sample);
        sensor_trust_format_flags(result.fault_flags, flag_text, sizeof(flag_text));
        printf("%3d  %8.3f  %-8s  score=%3d  flags=%s\n", index + 1,
               (double)sample.value, sensor_trust_state_name(result.state),
               result.health_score, flag_text);
    }

    result = sensor_trust_last(&ctx);
    sensor_trust_format_flags(result.fault_flags, flag_text, sizeof(flag_text));
    printf("\nsensor state: %s\n", sensor_trust_state_name(result.state));
    printf("score: %d\n", result.health_score);
    printf("flags: %s\n", flag_text);
}
