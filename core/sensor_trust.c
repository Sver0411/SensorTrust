/*
 * SensorTrust v0.1 - core detection logic.
 *
 * Pure C11: no dynamic memory, no RTOS, no hardware dependency.
 * See sensor_trust.h for the API contract.
 */
#include "sensor_trust.h"

#include <float.h>
#include <math.h>
#include <stdio.h>
#include <string.h>

/* ------------------------------------------------------------------ */
/* helpers                                                             */
/* ------------------------------------------------------------------ */

static int clamp_score(int score)
{
    if (score < 0) {
        return 0;
    }
    if (score > SENSOR_TRUST_SCORE_START) {
        return SENSOR_TRUST_SCORE_START;
    }
    return score;
}

/* A sample is usable when the caller flagged it valid and the payload is a
 * finite number. NaN and +-Inf are treated as read failures, not as values. */
static bool sample_is_usable(const sensor_sample_t *sample)
{
    if (!sample->valid) {
        return false;
    }
    if (isnan(sample->value) || isinf(sample->value)) {
        return false;
    }
    return true;
}

static void window_push(sensor_trust_t *ctx, float value)
{
    int capacity = ctx->window_capacity;

    if (capacity <= 0) {
        return;
    }
    if (ctx->window_count < capacity) {
        int index = (ctx->window_head + ctx->window_count) % capacity;
        ctx->window[index] = value;
        ctx->window_count++;
        return;
    }
    /* full: overwrite the oldest sample and advance the head */
    ctx->window[ctx->window_head] = value;
    ctx->window_head = (ctx->window_head + 1) % capacity;
}

/*
 * Value of the i-th oldest sample among the last n stored values
 * (i == 0 is the oldest of that tail, i == n - 1 is the newest sample).
 */
static float window_tail_value(const sensor_trust_t *ctx, int n, int i)
{
    int offset = (ctx->window_count - n) + i;
    int index = (ctx->window_head + offset) % ctx->window_capacity;
    return ctx->window[index];
}

/* Least-squares slope (value units per sample) over the last n values.
 * Two-pass, so the accumulators stay small and float precision is enough. */
static float window_slope(const sensor_trust_t *ctx, int n)
{
    float sum_x = 0.0f;
    float sum_y = 0.0f;
    float mean_x;
    float mean_y;
    float numerator = 0.0f;
    float denominator = 0.0f;
    int i;

    for (i = 0; i < n; i++) {
        sum_x += (float)i;
        sum_y += window_tail_value(ctx, n, i);
    }
    mean_x = sum_x / (float)n;
    mean_y = sum_y / (float)n;

    for (i = 0; i < n; i++) {
        float dx = (float)i - mean_x;
        float dy = window_tail_value(ctx, n, i) - mean_y;
        numerator += dx * dy;
        denominator += dx * dx;
    }
    if (denominator <= 0.0f) {
        return 0.0f;
    }
    return numerator / denominator;
}

static bool channel_is_usable(const sensor_trust_t *ctx)
{
    return ctx != NULL && ctx->initialised;
}

/* ------------------------------------------------------------------ */
/* configuration                                                       */
/* ------------------------------------------------------------------ */

void sensor_trust_default_config(sensor_trust_config_t *config)
{
    if (config == NULL) {
        return;
    }
    config->min_value = -100.0f;
    config->max_value = 100.0f;
    config->stuck_epsilon = 0.01f;
    config->stuck_window = 20;
    config->spike_threshold = 5.0f;
    config->drift_window = 24;
    config->drift_threshold = 0.01f;
    config->missing_limit = 3;
}

bool sensor_trust_config_is_valid(const sensor_trust_config_t *config)
{
    if (config == NULL) {
        return false;
    }
    if (isnan(config->min_value) || isinf(config->min_value) ||
        isnan(config->max_value) || isinf(config->max_value)) {
        return false;
    }
    if (!(config->min_value < config->max_value)) {
        return false;
    }
    if (isnan(config->stuck_epsilon) || config->stuck_epsilon < 0.0f) {
        return false;
    }
    if (config->stuck_window < 2 || config->stuck_window > SENSOR_TRUST_MAX_WINDOW) {
        return false;
    }
    if (isnan(config->spike_threshold) || config->spike_threshold < 0.0f) {
        return false;
    }
    if (config->drift_window < 2 || config->drift_window > SENSOR_TRUST_MAX_WINDOW) {
        return false;
    }
    if (isnan(config->drift_threshold) || config->drift_threshold <= 0.0f) {
        return false;
    }
    if (config->missing_limit < 1) {
        return false;
    }
    return true;
}

static int window_capacity_for(const sensor_trust_config_t *config)
{
    int capacity = config->stuck_window;

    if (config->drift_window > capacity) {
        capacity = config->drift_window;
    }
    if (capacity > SENSOR_TRUST_MAX_WINDOW) {
        capacity = SENSOR_TRUST_MAX_WINDOW;
    }
    return capacity;
}

/* ------------------------------------------------------------------ */
/* lifecycle                                                           */
/* ------------------------------------------------------------------ */

bool sensor_trust_init(sensor_trust_t *ctx, const sensor_trust_config_t *config)
{
    if (ctx == NULL) {
        return false;
    }
    memset(ctx, 0, sizeof(*ctx));
    if (!sensor_trust_config_is_valid(config)) {
        /* never run with a half-valid configuration */
        return false;
    }
    ctx->config = *config;
    ctx->window_capacity = window_capacity_for(config);
    ctx->initialised = true;
    /* No sample has been seen yet: no evidence of a fault, so report the
     * untouched score instead of a startup false alarm. */
    ctx->last_result.health_score = SENSOR_TRUST_SCORE_START;
    ctx->last_result.state = SENSOR_STATE_HEALTHY;
    ctx->last_result.fault_flags = FAULT_NONE;
    return true;
}

void sensor_trust_reset(sensor_trust_t *ctx)
{
    sensor_trust_config_t config;

    if (ctx == NULL) {
        return;
    }
    config = ctx->config;
    memset(ctx, 0, sizeof(*ctx));
    ctx->config = config;
    ctx->window_capacity = window_capacity_for(&config);
    ctx->initialised = true;
    ctx->last_result.health_score = SENSOR_TRUST_SCORE_START;
    ctx->last_result.state = SENSOR_STATE_HEALTHY;
    ctx->last_result.fault_flags = FAULT_NONE;
}

/* ------------------------------------------------------------------ */
/* detection                                                           */
/* ------------------------------------------------------------------ */

/*
 * Evidence collected on valid samples only. Invalid samples carry no value,
 * so they can neither trigger nor clear the value-based detectors; they feed
 * the MISSING counter instead.
 */
static uint32_t detect_on_valid_sample(sensor_trust_t *ctx, float value)
{
    uint32_t flags = FAULT_NONE;
    bool spike_returned = false;

    /* 1. RANGE_ERROR: outside the configured physical range. */
    if (value < ctx->config.min_value || value > ctx->config.max_value) {
        flags |= FAULT_RANGE;
    }

    /* 2. SPIKE: a jump away from the previous valid value.
     *    A jump followed by a return near the value it jumped from is the
     *    characteristic isolated excursion, so that sample is flagged too
     *    (and counted as a confirmed spike). A jump that never returns is
     *    reported once and then treated as a new level. */
    if (ctx->spike_pending) {
        bool returned =
            fabsf(value - ctx->spike_reference) <= ctx->config.spike_threshold;
        ctx->spike_pending = false;
        if (returned) {
            flags |= FAULT_SPIKE;
            ctx->spike_confirmed_count++;
            spike_returned = true;
        }
    }
    if (!spike_returned) {
        float jump = ctx->has_last_valid ? fabsf(value - ctx->last_valid_value) : 0.0f;
        if (jump > ctx->config.spike_threshold) {
            flags |= FAULT_SPIKE;
            ctx->spike_pending = true;
            ctx->spike_reference = ctx->last_valid_value;
        }
    }

    window_push(ctx, value);

    /* 3. STUCK: the last stuck_window values barely move. */
    if (ctx->window_count >= ctx->config.stuck_window) {
        float lowest = FLT_MAX;
        float highest = -FLT_MAX;
        int i;
        for (i = 0; i < ctx->config.stuck_window; i++) {
            float sample_value = window_tail_value(ctx, ctx->config.stuck_window, i);
            if (sample_value < lowest) {
                lowest = sample_value;
            }
            if (sample_value > highest) {
                highest = sample_value;
            }
        }
        if ((highest - lowest) < ctx->config.stuck_epsilon) {
            flags |= FAULT_STUCK;
        }
    }

    /* 4. DRIFT: a sustained one-directional trend.
     *    The trend must be visible in drift_window consecutive verdicts, so a
     *    short environmental ramp (heater warming up, window opened) does not
     *    raise the flag: whether it is a real change or a sensor drifting is
     *    not decidable from one value channel alone. */
    if (ctx->window_count >= ctx->config.drift_window) {
        float slope = window_slope(ctx, ctx->config.drift_window);
        if (fabsf(slope) > ctx->config.drift_threshold) {
            int direction = (slope > 0.0f) ? 1 : -1;
            if (direction == ctx->drift_direction) {
                ctx->drift_streak++;
            } else {
                ctx->drift_direction = direction;
                ctx->drift_streak = 1;
            }
            if (ctx->drift_streak >= ctx->config.drift_window) {
                flags |= FAULT_DRIFT;
            }
        } else {
            ctx->drift_streak = 0;
            ctx->drift_direction = 0;
        }
    }
    return flags;
}

sensor_health_result_t sensor_trust_update(sensor_trust_t *ctx,
                                           const sensor_sample_t *sample)
{
    sensor_health_result_t result;
    uint32_t flags = FAULT_NONE;

    if (!channel_is_usable(ctx) || sample == NULL) {
        result.health_score = 0;
        result.state = SENSOR_STATE_FAULT;
        result.fault_flags = FAULT_MISSING;
        if (ctx != NULL) {
            ctx->last_result = result;
        }
        return result;
    }

    if (sample_is_usable(sample)) {
        flags = detect_on_valid_sample(ctx, sample->value);
        ctx->consecutive_invalid = 0;
        ctx->last_valid_value = sample->value;
        ctx->last_valid_ts = sample->timestamp_ms;
        ctx->has_last_valid = true;
    } else {
        /* 5. MISSING_DATA: invalid payload or read failure. */
        if (ctx->consecutive_invalid < 0x7FFFFFFF) {
            ctx->consecutive_invalid++;
        }
        if (ctx->consecutive_invalid >= ctx->config.missing_limit) {
            flags |= FAULT_MISSING;
        }
        /* The buffer still holds the older values, but a trend is only
         * claimed when it is continuously observed, so a gap breaks it. */
        ctx->drift_streak = 0;
        ctx->drift_direction = 0;
    }

    result.fault_flags = flags;
    result.health_score = sensor_trust_score_from_flags(flags);
    result.state = sensor_trust_state_from_score(result.health_score);
    ctx->last_result = result;
    return result;
}

sensor_health_result_t sensor_trust_last(const sensor_trust_t *ctx)
{
    sensor_health_result_t result;

    if (!channel_is_usable(ctx)) {
        result.health_score = 0;
        result.state = SENSOR_STATE_FAULT;
        result.fault_flags = FAULT_MISSING;
        return result;
    }
    return ctx->last_result;
}

/* ------------------------------------------------------------------ */
/* reporting helpers                                                   */
/* ------------------------------------------------------------------ */

int sensor_trust_score_from_flags(uint32_t fault_flags)
{
    int penalty = 0;

    if (fault_flags & FAULT_RANGE) {
        penalty += SENSOR_TRUST_PENALTY_RANGE;
    }
    if (fault_flags & FAULT_STUCK) {
        penalty += SENSOR_TRUST_PENALTY_STUCK;
    }
    if (fault_flags & FAULT_SPIKE) {
        penalty += SENSOR_TRUST_PENALTY_SPIKE;
    }
    if (fault_flags & FAULT_DRIFT) {
        penalty += SENSOR_TRUST_PENALTY_DRIFT;
    }
    if (fault_flags & FAULT_MISSING) {
        penalty += SENSOR_TRUST_PENALTY_MISSING;
    }
    return clamp_score(SENSOR_TRUST_SCORE_START - penalty);
}

sensor_health_state_t sensor_trust_state_from_score(int health_score)
{
    if (health_score >= SENSOR_TRUST_HEALTHY_MIN_SCORE) {
        return SENSOR_STATE_HEALTHY;
    }
    if (health_score >= SENSOR_TRUST_DEGRADED_MIN_SCORE) {
        return SENSOR_STATE_DEGRADED;
    }
    return SENSOR_STATE_FAULT;
}

const char *sensor_trust_state_name(sensor_health_state_t state)
{
    switch (state) {
    case SENSOR_STATE_HEALTHY:
        return "HEALTHY";
    case SENSOR_STATE_DEGRADED:
        return "DEGRADED";
    case SENSOR_STATE_FAULT:
        return "FAULT";
    default:
        return "UNKNOWN";
    }
}

const char *sensor_trust_fault_token(uint32_t fault_flag)
{
    switch (fault_flag) {
    case FAULT_NONE:
        return "NONE";
    case FAULT_RANGE:
        return "RANGE";
    case FAULT_STUCK:
        return "STUCK";
    case FAULT_SPIKE:
        return "SPIKE";
    case FAULT_DRIFT:
        return "DRIFT";
    case FAULT_MISSING:
        return "MISSING";
    default:
        return "";
    }
}

size_t sensor_trust_format_flags(uint32_t fault_flags, char *buffer,
                                 size_t buffer_size)
{
    static const uint32_t k_order[] = {FAULT_RANGE, FAULT_STUCK, FAULT_SPIKE,
                                       FAULT_DRIFT, FAULT_MISSING};
    size_t used = 0;
    unsigned int index;

    if (buffer == NULL || buffer_size == 0) {
        return 0;
    }
    buffer[0] = '\0';
    if (fault_flags == FAULT_NONE) {
        return (size_t)snprintf(buffer, buffer_size, "NONE");
    }
    for (index = 0; index < sizeof(k_order) / sizeof(k_order[0]); index++) {
        int written;
        if ((fault_flags & k_order[index]) == 0) {
            continue;
        }
        written = snprintf(buffer + used, (used < buffer_size) ? buffer_size - used : 0,
                           "%s%s", (used == 0) ? "" : "|",
                           sensor_trust_fault_token(k_order[index]));
        if (written < 0) {
            break;
        }
        used += (size_t)written;
    }
    return used;
}
