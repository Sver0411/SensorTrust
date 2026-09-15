/*
 * SensorTrust v0.1 - host replay harness.
 *
 * Host-only driver used by the simulator: it reads a dataset CSV, replays the
 * samples through the real detection core and prints one result line per
 * sample. It is not part of the library, and it is not built into firmware --
 * it exists so the Python simulator never has to re-implement the algorithm.
 *
 * Usage: sensor_trust_replay <dataset.csv>
 *
 * Dataset format (written by simulator/generate.py):
 *     # scenario: stuck
 *     # expected: STUCK
 *     # channel: temperature_C
 *     #config: min_value=-40.0 max_value=85.0 ... missing_limit=3
 *     timestamp_ms,value,valid
 *     1000,25.013,1
 *
 * Output: one CSV line per sample
 *     sample_index,timestamp_ms,value,valid,health_score,state,fault_flags
 */
#include "sensor_trust.h"

#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define LINE_LENGTH 256

#define CONFIG_KEY_COUNT 8

typedef struct {
    const char *key;
    size_t offset; /* offset of the matching config field */
    bool is_float; /* explicit, so a value like "-40" is never guessed wrong */
} config_key_t;

static const config_key_t CONFIG_KEYS[CONFIG_KEY_COUNT] = {
    {"min_value", offsetof(sensor_trust_config_t, min_value), true},
    {"max_value", offsetof(sensor_trust_config_t, max_value), true},
    {"stuck_epsilon", offsetof(sensor_trust_config_t, stuck_epsilon), true},
    {"stuck_window", offsetof(sensor_trust_config_t, stuck_window), false},
    {"spike_threshold", offsetof(sensor_trust_config_t, spike_threshold), true},
    {"drift_window", offsetof(sensor_trust_config_t, drift_window), false},
    {"drift_threshold", offsetof(sensor_trust_config_t, drift_threshold), true},
    {"missing_limit", offsetof(sensor_trust_config_t, missing_limit), false},
};

_Static_assert(sizeof(CONFIG_KEYS) / sizeof(CONFIG_KEYS[0]) == CONFIG_KEY_COUNT,
               "config key table and CONFIG_KEY_COUNT must stay in sync");

static bool assign_config_value(sensor_trust_config_t *config, const char *key,
                                const char *value, uint32_t *seen)
{
    char *end = NULL;
    size_t index;

    for (index = 0; index < CONFIG_KEY_COUNT; index++) {
        char *field;
        if (strcmp(CONFIG_KEYS[index].key, key) != 0) {
            continue;
        }
        field = (char *)config + CONFIG_KEYS[index].offset;
        if (CONFIG_KEYS[index].is_float) {
            *(float *)field = strtof(value, &end);
        } else {
            *(int *)field = (int)strtol(value, &end, 10);
        }
        if (end == value || *end != '\0') {
            fprintf(stderr, "replay: bad value for '%s': '%s'\n", key, value);
            return false;
        }
        *seen |= (1u << index);
        return true;
    }
    fprintf(stderr, "replay: unknown config key '%s' (typo in the dataset?)\n", key);
    return false;
}

/* Splits a whitespace separated token list in place. Smaller than pulling in
 * strtok_r and keeps the tool free of POSIX-only declarations. */
static char *next_token(char **cursor)
{
    char *start = *cursor;
    char *scan;

    if (start == NULL) {
        return NULL;
    }
    while (*start == ' ' || *start == '\t' || *start == '\r' || *start == '\n') {
        start++;
    }
    if (*start == '\0') {
        *cursor = NULL;
        return NULL;
    }
    scan = start;
    while (*scan != '\0' && *scan != ' ' && *scan != '\t' && *scan != '\r' &&
           *scan != '\n') {
        scan++;
    }
    if (*scan != '\0') {
        *scan = '\0';
        *cursor = scan + 1;
    } else {
        *cursor = NULL;
    }
    return start;
}

/* Parses "#config: key=value key=value ...". Every key is mandatory, so a
 * dataset can never be replayed with half of its configuration silently
 * replaced by defaults. */
static bool parse_config_line(char *line, sensor_trust_config_t *config)
{
    char *cursor = line + strlen("#config:");
    char *token;
    uint32_t seen = 0;

    sensor_trust_default_config(config);
    while ((token = next_token(&cursor)) != NULL) {
        char *equals = strchr(token, '=');
        if (equals == NULL) {
            fprintf(stderr, "replay: expected key=value, got '%s'\n", token);
            return false;
        }
        *equals = '\0';
        if (!assign_config_value(config, token, equals + 1, &seen)) {
            return false;
        }
    }
    if (seen != ((1u << CONFIG_KEY_COUNT) - 1u)) {
        fprintf(stderr, "replay: dataset config is incomplete (mask 0x%x)\n", seen);
        return false;
    }
    if (!sensor_trust_config_is_valid(config)) {
        fprintf(stderr, "replay: dataset config is not valid\n");
        return false;
    }
    return true;
}

/* Splits "timestamp,value,valid" in place. */
static bool parse_data_line(char *line, uint64_t *timestamp_ms, float *value,
                           int *valid)
{
    char *first = strchr(line, ',');
    char *second;

    if (first == NULL) {
        return false;
    }
    second = strchr(first + 1, ',');
    if (second == NULL) {
        return false;
    }
    *first = '\0';
    *second = '\0';
    *timestamp_ms = strtoull(line, NULL, 10);
    *value = strtof(first + 1, NULL); /* "nan" for invalid samples */
    *valid = (int)strtol(second + 1, NULL, 10);
    return true;
}

int main(int argc, char **argv)
{
    sensor_trust_config_t config;
    sensor_trust_t ctx;
    bool have_config = false;
    char line[LINE_LENGTH];
    int sample_index = 0;
    FILE *stream;

    if (argc != 2) {
        fprintf(stderr, "usage: %s <dataset.csv>\n", argv[0]);
        return 2;
    }
    stream = fopen(argv[1], "r");
    if (stream == NULL) {
        fprintf(stderr, "replay: cannot open '%s'\n", argv[1]);
        return 2;
    }

    printf("sample_index,timestamp_ms,value,valid,health_score,state,fault_flags\n");

    while (fgets(line, sizeof(line), stream) != NULL) {
        unsigned char first = (unsigned char)line[0];
        uint64_t timestamp_ms = 0;
        float value = 0.0f;
        int valid = 0;
        sensor_sample_t sample;
        sensor_health_result_t result;

        if (first == '#' || first == '\n' || first == '\r' || first == '\0') {
            if (strncmp(line, "#config:", strlen("#config:")) == 0) {
                if (!parse_config_line(line, &config)) {
                    fclose(stream);
                    return 2;
                }
                if (!sensor_trust_init(&ctx, &config)) {
                    fprintf(stderr, "replay: core rejected the dataset config\n");
                    fclose(stream);
                    return 2;
                }
                have_config = true;
            }
            continue;
        }
        if (strncmp(line, "timestamp_ms", strlen("timestamp_ms")) == 0) {
            continue; /* column header */
        }
        if (!have_config) {
            fprintf(stderr, "replay: data before the #config line\n");
            fclose(stream);
            return 2;
        }
        if (!parse_data_line(line, &timestamp_ms, &value, &valid)) {
            fprintf(stderr, "replay: malformed data line '%s'\n", line);
            fclose(stream);
            return 2;
        }

        sample.value = value;
        sample.valid = (valid != 0);
        sample.timestamp_ms = timestamp_ms;
        result = sensor_trust_update(&ctx, &sample);

        printf("%d,%llu,%.6f,%d,%d,%s,%u\n", sample_index,
               (unsigned long long)timestamp_ms, (double)value, valid,
               result.health_score, sensor_trust_state_name(result.state),
               (unsigned)result.fault_flags);
        sample_index++;
    }

    fclose(stream);
    if (!have_config) {
        fprintf(stderr, "replay: no #config line found in '%s'\n", argv[1]);
        return 2;
    }
    return 0;
}
