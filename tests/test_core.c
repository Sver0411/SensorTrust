/*
 * SensorTrust v0.1 - host tests for the C core.
 *
 * Standalone: no test framework, no dependencies beyond libc/libm.
 *   cc core/sensor_trust.c tests/test_core.c -o test_core -lm && ./test_core
 */
#include "../core/sensor_trust.h"

#include <math.h>
#include <stdio.h>
#include <string.h>

/* ------------------------------------------------------------------ */
/* tiny test harness                                                   */
/* ------------------------------------------------------------------ */

static int g_tests_run;
static int g_tests_failed;
static int g_checks;
static int g_checks_failed;
static const char *g_test_name = "";

#define CHECK(cond)                                                          \
    do {                                                                     \
        g_checks++;                                                          \
        if (!(cond)) {                                                       \
            g_checks_failed++;                                               \
            printf("    FAIL [%s] %s:%d  %s\n", g_test_name, __FILE__, __LINE__,     \
                   #cond);                                                       \
        }                                                                    \
    } while (0)

/* Filler byte for the guard region behind a buffer under test: if a function
 * writes past the size it was given, it has to show up here. */
#define GUARD_BYTE 0xA5

static void run_test(const char *name, void (*test_fn)(void))
{
    int failures_before = g_checks_failed;

    g_test_name = name;
    test_fn();
    g_tests_run++;
    if (g_checks_failed != failures_before) {
        g_tests_failed++;
        printf("not ok %d - %s\n", g_tests_run, name);
    } else {
        printf("ok %d - %s\n", g_tests_run, name);
    }
}

/* ------------------------------------------------------------------ */
/* helpers                                                             */
/* ------------------------------------------------------------------ */

/* Deterministic, zero-mean, zero-slope noise pattern: period 8, amplitude
 * +-0.10, largest step 0.05. Not random, so results are reproducible. */
static float gentle_noise(int index)
{
    static const int k_pattern[8] = {4, 5, 6, 7, 6, 5, 4, 3};
    return ((float)k_pattern[index % 8] - 5.0f) * 0.05f;
}

static sensor_trust_config_t default_config(void)
{
    sensor_trust_config_t config;
    sensor_trust_default_config(&config);
    return config;
}

static sensor_health_result_t feed(sensor_trust_t *ctx, float value, uint64_t timestamp_ms)
{
    sensor_sample_t sample;
    sample.value = value;
    sample.valid = true;
    sample.timestamp_ms = timestamp_ms;
    return sensor_trust_update(ctx, &sample);
}

static sensor_health_result_t feed_invalid(sensor_trust_t *ctx, uint64_t timestamp_ms)
{
    sensor_sample_t sample;
    sample.value = 0.0f;
    sample.valid = false;
    sample.timestamp_ms = timestamp_ms;
    return sensor_trust_update(ctx, &sample);
}

typedef struct {
    uint32_t union_flags;
    int min_score;
    int flag_samples;     /* samples whose result contained the watched flag */
    int first_flag_index; /* -1 when the watched flag never appeared          */
} run_summary_t;

/* Replays a value list at a caller-chosen sampling interval. The interval has
 * to be controllable because the DRIFT slope is measured against real time:
 * feeding the same physical trend at 1 s, 2 s and 5 s must give the same
 * verdict, and that can only be checked if the interval is a parameter. */
static run_summary_t run_values_at_interval(sensor_trust_t *ctx, const float *values,
                                            int count, uint64_t interval_ms,
                                            uint32_t watched_flag)
{
    run_summary_t summary;
    int i;

    summary.union_flags = FAULT_NONE;
    summary.min_score = SENSOR_TRUST_SCORE_START;
    summary.flag_samples = 0;
    summary.first_flag_index = -1;

    for (i = 0; i < count; i++) {
        sensor_health_result_t result =
            feed(ctx, values[i], (uint64_t)(i + 1) * interval_ms);
        summary.union_flags |= result.fault_flags;
        if (result.health_score < summary.min_score) {
            summary.min_score = result.health_score;
        }
        if (watched_flag != FAULT_NONE && (result.fault_flags & watched_flag) != 0u) {
            summary.flag_samples++;
            if (summary.first_flag_index < 0) {
                summary.first_flag_index = i;
            }
        }
    }
    return summary;
}

/* The common case: 1 Hz, which is what the simulator and the demo use. */
static run_summary_t run_values(sensor_trust_t *ctx, const float *values, int count,
                                uint32_t watched_flag)
{
    return run_values_at_interval(ctx, values, count, 1000u, watched_flag);
}

/* ------------------------------------------------------------------ */
/* tests                                                               */
/* ------------------------------------------------------------------ */

static void test_config_validation(void)
{
    sensor_trust_config_t config = default_config();
    sensor_trust_config_t bad;
    sensor_trust_t ctx;
    sensor_health_result_t result;

    CHECK(sensor_trust_default_config != NULL);
    CHECK(sensor_trust_config_is_valid(&config));
    CHECK(!sensor_trust_config_is_valid(NULL));

    bad = config;
    bad.min_value = 100.0f;
    bad.max_value = -40.0f;
    CHECK(!sensor_trust_config_is_valid(&bad));

    bad = config;
    bad.stuck_window = SENSOR_TRUST_MAX_WINDOW + 1;
    CHECK(!sensor_trust_config_is_valid(&bad));

    bad = config;
    bad.drift_window = 1;
    CHECK(!sensor_trust_config_is_valid(&bad));

    bad = config;
    bad.missing_limit = 0;
    CHECK(!sensor_trust_config_is_valid(&bad));

    bad = config;
    bad.stuck_epsilon = -0.01f;
    CHECK(!sensor_trust_config_is_valid(&bad));

    bad = config;
    bad.drift_threshold = 0.0f;
    CHECK(!sensor_trust_config_is_valid(&bad));

    /* a zero spike threshold is allowed: every change counts as a jump */
    bad = config;
    bad.spike_threshold = 0.0f;
    CHECK(sensor_trust_config_is_valid(&bad));

    /* an invalid configuration must not be accepted silently */
    bad = config;
    bad.stuck_window = 0;
    CHECK(!sensor_trust_init(&ctx, &bad));
    result = feed(&ctx, 25.0f, 1000u);
    CHECK(result.health_score == 0);
    CHECK(result.state == SENSOR_STATE_FAULT);

    CHECK(sensor_trust_init(&ctx, &config));
}

/*
 * Every float field has to be finite. NaN compares false against everything,
 * so a range check alone would let it through; an infinity silently disables
 * (or saturates) the detector it belongs to. Only the genuinely distinct
 * boundaries are covered here - five fields times the three non-finite values
 * - rather than padding the count with repeats.
 */
static void test_config_rejects_non_finite_values(void)
{
    sensor_trust_config_t good = default_config();
    sensor_trust_config_t bad;

    CHECK(sensor_trust_config_is_valid(&good));

    /* NaN */
    bad = good;
    bad.min_value = NAN;
    CHECK(!sensor_trust_config_is_valid(&bad));
    bad = good;
    bad.max_value = NAN;
    CHECK(!sensor_trust_config_is_valid(&bad));
    bad = good;
    bad.stuck_epsilon = NAN;
    CHECK(!sensor_trust_config_is_valid(&bad));
    bad = good;
    bad.spike_threshold = NAN;
    CHECK(!sensor_trust_config_is_valid(&bad));
    bad = good;
    bad.drift_threshold = NAN;
    CHECK(!sensor_trust_config_is_valid(&bad));

    /* +Inf */
    bad = good;
    bad.min_value = INFINITY;
    CHECK(!sensor_trust_config_is_valid(&bad));
    bad = good;
    bad.max_value = INFINITY;
    CHECK(!sensor_trust_config_is_valid(&bad));
    bad = good;
    bad.stuck_epsilon = INFINITY;
    CHECK(!sensor_trust_config_is_valid(&bad));
    bad = good;
    bad.spike_threshold = INFINITY;
    CHECK(!sensor_trust_config_is_valid(&bad));
    bad = good;
    bad.drift_threshold = INFINITY;
    CHECK(!sensor_trust_config_is_valid(&bad));

    /* -Inf */
    bad = good;
    bad.min_value = -INFINITY;
    CHECK(!sensor_trust_config_is_valid(&bad));
    bad = good;
    bad.max_value = -INFINITY;
    CHECK(!sensor_trust_config_is_valid(&bad));
    bad = good;
    bad.stuck_epsilon = -INFINITY;
    CHECK(!sensor_trust_config_is_valid(&bad));
    bad = good;
    bad.spike_threshold = -INFINITY;
    CHECK(!sensor_trust_config_is_valid(&bad));
    bad = good;
    bad.drift_threshold = -INFINITY;
    CHECK(!sensor_trust_config_is_valid(&bad));

    /* an infinite range is not a licence to accept every reading */
    {
        sensor_trust_t ctx;
        sensor_health_result_t result;

        bad = good;
        bad.max_value = INFINITY;
        CHECK(!sensor_trust_init(&ctx, &bad));
        result = feed(&ctx, 1000.0f, 1000u);
        CHECK(result.health_score == 0);
        CHECK(result.state == SENSOR_STATE_FAULT);
    }
}

static void test_uninitialised_context_is_safe(void)
{
    sensor_trust_t ctx;
    sensor_sample_t sample;
    sensor_health_result_t result;

    memset(&ctx, 0, sizeof(ctx));
    sample.value = 25.0f;
    sample.valid = true;
    sample.timestamp_ms = 1000u;

    result = sensor_trust_last(&ctx);
    CHECK(result.health_score == 0);
    CHECK(result.state == SENSOR_STATE_FAULT);
    CHECK(result.fault_flags == FAULT_MISSING);

    result = sensor_trust_update(&ctx, &sample);
    CHECK(result.health_score == 0);
    CHECK(result.state == SENSOR_STATE_FAULT);

    result = sensor_trust_update(NULL, &sample);
    CHECK(result.health_score == 0);
    CHECK(result.fault_flags == FAULT_MISSING);

    ctx.last_result.health_score = 7;
    sensor_trust_reset(NULL); /* must not crash */
}

/*
 * A reset restarts a running channel. It must never turn a context that was
 * never usable into one that looks initialised: that would hand the caller a
 * channel with no valid configuration, and every later sample would then be
 * judged against garbage thresholds.
 */
static void test_reset_on_uninitialised_context_is_safe(void)
{
    sensor_trust_config_t config = default_config();
    sensor_trust_config_t bad = config;
    sensor_trust_t ctx;
    sensor_health_result_t result;

    /* (a) a zeroed context was never initialised. Resetting it must leave it
     * unusable instead of fabricating a working channel. */
    memset(&ctx, 0, sizeof(ctx));
    sensor_trust_reset(&ctx);
    CHECK(ctx.initialised == false);
    result = sensor_trust_last(&ctx);
    CHECK(result.health_score == 0);
    CHECK(result.state == SENSOR_STATE_FAULT);
    CHECK(result.fault_flags == FAULT_MISSING);

    result = feed(&ctx, 25.0f, 1000u);
    CHECK(result.health_score == 0);
    CHECK(result.state == SENSOR_STATE_FAULT);
    CHECK(result.fault_flags == FAULT_MISSING);
    CHECK(ctx.initialised == false);
    CHECK(ctx.window_count == 0);

    /* (b) init failed, so reset must not "repair" the context */
    bad.stuck_window = 0;
    CHECK(!sensor_trust_init(&ctx, &bad));
    CHECK(ctx.initialised == false);
    sensor_trust_reset(&ctx);
    CHECK(ctx.initialised == false);
    result = sensor_trust_last(&ctx);
    CHECK(result.health_score == 0);
    CHECK(result.state == SENSOR_STATE_FAULT);
    CHECK(result.fault_flags == FAULT_MISSING);
    result = feed(&ctx, 25.0f, 1000u);
    CHECK(result.health_score == 0);
    CHECK(result.state == SENSOR_STATE_FAULT);

    /* (c) a running context whose configuration was corrupted in place (the
     * struct is public) is refused too, rather than restarted with NaN */
    CHECK(sensor_trust_init(&ctx, &config));
    ctx.config.drift_threshold = NAN;
    sensor_trust_reset(&ctx);
    CHECK(ctx.initialised == false);
    CHECK(sensor_trust_last(&ctx).state == SENSOR_STATE_FAULT);

    /* (d) a genuinely running channel is still restartable and keeps config */
    CHECK(sensor_trust_init(&ctx, &config));
    feed(&ctx, 25.0f, 1000u);
    sensor_trust_reset(&ctx);
    CHECK(ctx.initialised == true);
    CHECK(ctx.config.stuck_window == config.stuck_window);
    CHECK(ctx.window_count == 0);
    result = sensor_trust_last(&ctx);
    CHECK(result.health_score == 100);
    CHECK(result.state == SENSOR_STATE_HEALTHY);
    CHECK(result.fault_flags == FAULT_NONE);

    /* (e) NULL is ignored */
    sensor_trust_reset(NULL);
}

static void test_healthy_sequence_stays_healthy(void)
{
    sensor_trust_config_t config = default_config();
    sensor_trust_t ctx;
    float values[400];
    run_summary_t summary;
    int i;

    /* longer than SENSOR_TRUST_MAX_WINDOW, so the ring buffer wraps a few times */
    for (i = 0; i < 400; i++) {
        values[i] = 25.0f + gentle_noise(i);
    }
    CHECK(sensor_trust_init(&ctx, &config));
    summary = run_values(&ctx, values, 400, FAULT_NONE);

    CHECK(summary.union_flags == FAULT_NONE);
    CHECK(summary.min_score == 100);

    CHECK(ctx.last_result.state == SENSOR_STATE_HEALTHY);
    CHECK(ctx.last_result.health_score == 100);
    CHECK(strcmp(sensor_trust_state_name(ctx.last_result.state), "HEALTHY") == 0);
}

static void test_range_error_both_bounds(void)
{
    sensor_trust_config_t config = default_config();
    sensor_trust_t ctx;
    sensor_health_result_t result;

    /* a humidity-like channel. The spike threshold is raised so that this
     * test isolates RANGE from the jump detector. */
    config.min_value = 0.0f;
    config.max_value = 100.0f;
    config.spike_threshold = 1000.0f;
    CHECK(sensor_trust_init(&ctx, &config));

    result = feed(&ctx, 45.0f, 1000u);
    CHECK(result.fault_flags == FAULT_NONE);
    CHECK(result.health_score == 100);

    result = feed(&ctx, 135.0f, 2000u);
    CHECK(result.fault_flags == FAULT_RANGE);
    CHECK(result.health_score == 45);
    CHECK(result.state == SENSOR_STATE_FAULT); /* an impossible value is unusable */

    result = feed(&ctx, 45.0f, 3000u);
    CHECK(result.fault_flags == FAULT_NONE);
    CHECK(result.health_score == 100);

    result = feed(&ctx, -0.5f, 4000u);
    CHECK((result.fault_flags & FAULT_RANGE) != 0u);

    /* detectors are independent: a jump into impossible territory carries
     * both flags, and both penalties apply */
    config.spike_threshold = 5.0f;
    CHECK(sensor_trust_init(&ctx, &config));
    feed(&ctx, 45.0f, 1000u);
    result = feed(&ctx, 135.0f, 2000u);
    CHECK((result.fault_flags & FAULT_RANGE) != 0u);
    CHECK((result.fault_flags & FAULT_SPIKE) != 0u);
    CHECK(result.fault_flags == (FAULT_RANGE | FAULT_SPIKE));
    CHECK(result.health_score == 20); /* 100 - 55 - 25 */
    CHECK(result.state == SENSOR_STATE_FAULT);
}

static void test_stuck_detected_after_full_window(void)
{
    sensor_trust_config_t config = default_config();
    sensor_trust_t ctx;
    run_summary_t summary;
    float values[40];
    int i;
    sensor_health_result_t result;

    config.stuck_window = 20;
    config.stuck_epsilon = 0.01f;
    CHECK(sensor_trust_init(&ctx, &config));
    for (i = 0; i < 40; i++) {
        values[i] = 25.213f; /* frozen register value */
    }

    for (i = 0; i < 19; i++) {
        result = feed(&ctx, values[i], (uint64_t)(i + 1) * 1000u);
        CHECK((result.fault_flags & FAULT_STUCK) == 0u);
    }
    result = feed(&ctx, values[19], 20000u);
    CHECK((result.fault_flags & FAULT_STUCK) != 0u);
    CHECK(result.health_score == 65);
    CHECK(result.state == SENSOR_STATE_DEGRADED);

    /* a long frozen run stays STUCK and never drifts or spikes */
    summary = run_values(&ctx, values, 40, FAULT_STUCK);
    CHECK(summary.union_flags == FAULT_STUCK);
    CHECK(summary.min_score == 65);
}

static void test_stuck_ignores_noisy_signal(void)
{
    sensor_trust_config_t config = default_config();
    sensor_trust_t ctx;
    float values[200];
    run_summary_t summary;
    int i;

    config.stuck_window = 20;
    config.stuck_epsilon = 0.01f;
    for (i = 0; i < 200; i++) {
        /* a stable but genuinely moving environment: +-0.10 around 25 C */
        values[i] = 25.0f + gentle_noise(i);
    }
    CHECK(sensor_trust_init(&ctx, &config));
    summary = run_values(&ctx, values, 200, FAULT_STUCK);
    CHECK(summary.union_flags == FAULT_NONE);
    CHECK(summary.min_score == 100);
}

static void test_spike_isolated_jump_confirmed(void)
{
    sensor_trust_config_t config = default_config();
    sensor_trust_t ctx;
    float values[12];
    run_summary_t summary;
    int i;

    config.spike_threshold = 3.0f;
    for (i = 0; i < 12; i++) {
        values[i] = 25.0f;
    }
    values[5] = 61.0f; /* single-sample excursion, returned on the next sample */

    CHECK(sensor_trust_init(&ctx, &config));
    summary = run_values(&ctx, values, 12, FAULT_SPIKE);

    CHECK((summary.union_flags & FAULT_SPIKE) != 0u);
    CHECK(summary.flag_samples == 2);          /* the jump and the return */
    CHECK(summary.first_flag_index == 5);
    CHECK(summary.min_score == 75);
    CHECK(ctx.spike_confirmed_count == 1);
    CHECK(ctx.last_result.fault_flags == FAULT_NONE); /* back to normal */
    CHECK(ctx.last_result.health_score == 100);
}

/*
 * A real level change is not an isolated excursion: the first sample at the
 * new level does look like a jump, so it is reported once, but because the
 * reading never comes back the excursion is never confirmed and the new level
 * is accepted. Hence the name - it is not that a step change cannot look like
 * a spike, it is that a step change is flagged once and then accepted.
 */
static void test_step_change_is_flagged_once_then_accepted(void)
{
    sensor_trust_config_t config = default_config();
    sensor_trust_t ctx;
    float values[20];
    run_summary_t summary;
    int i;

    config.spike_threshold = 3.0f;
    config.stuck_window = 20;
    config.drift_window = 24; /* larger than the sequence: no drift verdict */
    for (i = 0; i < 20; i++) {
        values[i] = (i < 10) ? 25.0f : 61.0f;
    }

    CHECK(sensor_trust_init(&ctx, &config));
    summary = run_values(&ctx, values, 20, FAULT_SPIKE);

    /* a level change is reported once, then accepted as the new level */
    CHECK(summary.flag_samples == 1);
    CHECK(summary.first_flag_index == 10);
    CHECK(summary.union_flags == FAULT_SPIKE);
    CHECK(ctx.last_result.fault_flags == FAULT_NONE);
    CHECK(ctx.spike_confirmed_count == 0);
}

static void test_spike_uses_last_valid_value_across_gap(void)
{
    sensor_trust_config_t config = default_config();
    sensor_trust_t ctx;
    sensor_health_result_t result;

    config.spike_threshold = 3.0f;
    config.missing_limit = 10; /* tolerate the gap in this test */
    CHECK(sensor_trust_init(&ctx, &config));

    feed(&ctx, 25.0f, 1000u);
    feed(&ctx, 25.0f, 2000u);
    feed_invalid(&ctx, 3000u);
    feed_invalid(&ctx, 4000u);
    result = feed(&ctx, 25.0f, 5000u);
    CHECK(result.fault_flags == FAULT_NONE);
    CHECK(result.health_score == 100);

    result = feed(&ctx, 61.0f, 6000u); /* compared against 25, not against 0 */
    CHECK((result.fault_flags & FAULT_SPIKE) != 0u);
}

static void test_drift_detected_on_sustained_ramp(void)
{
    sensor_trust_config_t config = default_config();
    sensor_trust_t ctx;
    float values[200];
    run_summary_t summary;
    int i;

    config.drift_window = 24;
    config.drift_threshold = 0.01f; /* 0.01 C per second */
    /* +0.02 C per second, sampled at 1 Hz, no noise. The verdict has to repeat
     * for a full drift_window, so it appears after roughly two windows. */
    for (i = 0; i < 200; i++) {
        values[i] = 25.0f + 0.02f * (float)i;
    }
    CHECK(sensor_trust_init(&ctx, &config));
    summary = run_values(&ctx, values, 200, FAULT_DRIFT);

    CHECK((summary.union_flags & FAULT_DRIFT) != 0u);
    CHECK(summary.min_score == 75);
    CHECK(summary.first_flag_index >= config.drift_window);
    CHECK(summary.first_flag_index <= 2 * config.drift_window);
    CHECK(ctx.last_result.state == SENSOR_STATE_DEGRADED); /* still drifting */
}

static void test_drift_not_detected_on_short_ramp(void)
{
    sensor_trust_config_t config = default_config();
    sensor_trust_t ctx;
    float values[120];
    run_summary_t summary;
    int i;

    config.drift_window = 24;
    config.drift_threshold = 0.01f; /* 0.01 C per second */

    /* a quick 1 C warm-up over 20 samples, then a stable but live reading:
     * a real change that lasts less than one window is not reported as drift */
    for (i = 0; i < 120; i++) {
        values[i] = (i < 20) ? 25.0f + 0.05f * (float)i : 26.0f + gentle_noise(i);
    }
    CHECK(sensor_trust_init(&ctx, &config));
    summary = run_values(&ctx, values, 120, FAULT_DRIFT);
    CHECK((summary.union_flags & FAULT_DRIFT) == 0u);
    CHECK(ctx.last_result.fault_flags == FAULT_NONE);
    CHECK(ctx.last_result.health_score == 100);

    /* a slow trend that stays below the drift threshold is not drift either:
     * 0.008 C per second (sampled at 1 Hz) over 120 s is a perfectly
     * plausible environment */
    for (i = 0; i < 120; i++) {
        values[i] = 25.0f + 0.008f * (float)i;
    }
    CHECK(sensor_trust_init(&ctx, &config));
    summary = run_values(&ctx, values, 120, FAULT_DRIFT);
    CHECK((summary.union_flags & FAULT_DRIFT) == 0u);
    CHECK(ctx.last_result.fault_flags == FAULT_NONE);
}

/*
 * The DRIFT slope is measured in value units per second, so the same physical
 * trend has to be judged the same way whatever the sampling interval is. That
 * matters because a scheduler (AdaptiveSense) may change the interval at
 * runtime; with a per-sample slope the drift sensitivity would silently depend
 * on that scheduling decision.
 *
 * The second half is the discriminating case. A 0.008 C/s trend has a
 * per-sample step of 0.008 at 1 s, 0.016 at 2 s and 0.04 at 5 s. With a
 * threshold read as "0.01 per sample", the 2 s and 5 s runs would report drift
 * on a trend that is physically well below the sensitivity.
 */
static void test_drift_slope_is_independent_of_sampling_interval(void)
{
    static const uint64_t k_intervals_ms[3] = {1000u, 2000u, 5000u};
    sensor_trust_config_t config = default_config();
    int first_flag_index[3] = {-1, -1, -1};
    unsigned int index;

    config.min_value = -40.0f;
    config.max_value = 85.0f;
    config.stuck_epsilon = 0.01f;
    config.stuck_window = 20;
    config.spike_threshold = 3.0f;
    config.drift_window = 24;
    config.drift_threshold = 0.01f; /* 0.01 C per SECOND */
    config.missing_limit = 3;

    /* the same physical trend, +0.02 C per second, at three intervals: the
     * per-sample step differs by 5x, the real slope does not */
    for (index = 0; index < 3u; index++) {
        sensor_trust_t ctx;
        run_summary_t summary;
        float values[120];
        float seconds_per_sample = (float)k_intervals_ms[index] / 1000.0f;
        int i;

        for (i = 0; i < 120; i++) {
            values[i] = 25.0f + 0.02f * seconds_per_sample * (float)i;
        }
        CHECK(sensor_trust_init(&ctx, &config));
        summary = run_values_at_interval(&ctx, values, 120, k_intervals_ms[index],
                                        FAULT_DRIFT);

        CHECK((summary.union_flags & FAULT_DRIFT) != 0u);
        CHECK(summary.union_flags == FAULT_DRIFT); /* nothing else fires */
        first_flag_index[index] = summary.first_flag_index;
    }

    /* Not just the same verdict: the same NUMBER of samples. The trend has to
     * fill one window before the streak starts, then hold for drift_window
     * more verdicts, and none of that counts time. Only the wall-clock
     * duration of the run differs between the three intervals. */
    CHECK(first_flag_index[0] == first_flag_index[1]);
    CHECK(first_flag_index[1] == first_flag_index[2]);
    CHECK(first_flag_index[0] >= config.drift_window);
    CHECK(first_flag_index[0] <= 2 * config.drift_window);

    /* 0.008 C per second: below the sensitivity at every interval */
    for (index = 0; index < 3u; index++) {
        sensor_trust_t ctx;
        run_summary_t summary;
        float values[120];
        float seconds_per_sample = (float)k_intervals_ms[index] / 1000.0f;
        int i;

        for (i = 0; i < 120; i++) {
            values[i] = 25.0f + 0.008f * seconds_per_sample * (float)i;
        }
        CHECK(sensor_trust_init(&ctx, &config));
        summary = run_values_at_interval(&ctx, values, 120, k_intervals_ms[index],
                                        FAULT_DRIFT);
        CHECK(summary.union_flags == FAULT_NONE);
    }
}

/*
 * A broken clock must not produce a drift verdict, a division by zero or a
 * NaN. A stopped or rolling-back timestamp makes a window unusable for the
 * time-based detector; RANGE, SPIKE and STUCK do not use time at all, so they
 * keep working on exactly the same samples. No new fault type is introduced
 * for this: the answer is simply "no drift claimed".
 */
static void test_drift_ignores_non_increasing_timestamps(void)
{
    sensor_trust_config_t config = default_config();
    sensor_trust_t ctx;
    float values[120];
    sensor_health_result_t result;
    uint32_t union_flags;
    int inconsistent;
    int i;

    /* a threshold so low that a time axis built from the sample index would
     * certainly report drift on this ramp */
    config.min_value = -40.0f;
    config.max_value = 85.0f;
    config.stuck_epsilon = 0.01f;
    config.stuck_window = 20;
    config.spike_threshold = 3.0f;
    config.drift_window = 24;
    config.drift_threshold = 0.0001f;
    config.missing_limit = 3;

    for (i = 0; i < 120; i++) {
        values[i] = 25.0f + 0.02f * (float)i;
    }

    /* (a) a clock that never moves */
    union_flags = FAULT_NONE;
    inconsistent = 0;
    CHECK(sensor_trust_init(&ctx, &config));
    for (i = 0; i < 120; i++) {
        result = feed(&ctx, values[i], 1700000000000u);
        union_flags |= result.fault_flags;
        if (result.health_score != sensor_trust_score_from_flags(result.fault_flags) ||
            result.health_score < 0 || result.health_score > 100) {
            inconsistent++;
        }
    }
    CHECK(union_flags == FAULT_NONE);
    CHECK(inconsistent == 0);
    CHECK(ctx.last_result.health_score == 100);

    /* (b) a clock that rolls backwards while the value climbs */
    union_flags = FAULT_NONE;
    inconsistent = 0;
    CHECK(sensor_trust_init(&ctx, &config));
    for (i = 0; i < 120; i++) {
        result = feed(&ctx, values[i], 1700000000000u - (uint64_t)i * 1000u);
        union_flags |= result.fault_flags;
        if (result.health_score != sensor_trust_score_from_flags(result.fault_flags) ||
            result.health_score < 0 || result.health_score > 100) {
            inconsistent++;
        }
    }
    CHECK(union_flags == FAULT_NONE);
    CHECK(inconsistent == 0);

    /* (c) the time-free detectors still work on a stopped clock */
    config.stuck_window = 8;
    CHECK(sensor_trust_init(&ctx, &config));
    for (i = 0; i < 12; i++) {
        result = feed(&ctx, 25.213f, 5000u);
    }
    CHECK(result.fault_flags == FAULT_STUCK);
    CHECK(result.health_score == 65);

    result = feed(&ctx, 200.0f, 5000u);
    CHECK((result.fault_flags & FAULT_RANGE) != 0u);
}

static void test_missing_detected_after_limit_and_clears_on_recovery(void)
{
    sensor_trust_config_t config = default_config();
    sensor_trust_t ctx;
    sensor_sample_t sample;
    sensor_health_result_t result;

    config.missing_limit = 3;
    CHECK(sensor_trust_init(&ctx, &config));

    result = feed(&ctx, 25.0f, 1000u);
    CHECK(result.fault_flags == FAULT_NONE);

    result = feed_invalid(&ctx, 2000u);
    CHECK(result.fault_flags == FAULT_NONE);
    result = feed_invalid(&ctx, 3000u);
    CHECK(result.fault_flags == FAULT_NONE);
    result = feed_invalid(&ctx, 4000u);
    CHECK((result.fault_flags & FAULT_MISSING) != 0u);
    CHECK(result.health_score == 45);
    CHECK(result.state == SENSOR_STATE_FAULT); /* no usable data at all */

    result = feed(&ctx, 25.0f, 5000u); /* recovery */
    CHECK(result.fault_flags == FAULT_NONE);
    CHECK(result.health_score == 100);

    /* NaN and +-Inf are read failures even when the caller sets valid = true */
    sample.value = NAN;
    sample.valid = true;
    sample.timestamp_ms = 6000u;
    CHECK(sensor_trust_update(&ctx, &sample).fault_flags == FAULT_NONE);
    sample.value = INFINITY;
    sample.timestamp_ms = 7000u;
    sensor_trust_update(&ctx, &sample);
    sample.value = -INFINITY;
    sample.timestamp_ms = 8000u;
    result = sensor_trust_update(&ctx, &sample);
    CHECK((result.fault_flags & FAULT_MISSING) != 0u);
    CHECK(result.state == SENSOR_STATE_FAULT);
}

static void test_stuck_and_range_combine_into_fault(void)
{
    sensor_trust_config_t config = default_config();
    sensor_trust_t ctx;
    sensor_health_result_t result;
    float values[10];
    run_summary_t summary;
    int i;

    config.min_value = 0.0f;
    config.max_value = 50.0f;
    config.spike_threshold = 1.0f;
    config.stuck_window = 4;
    config.stuck_epsilon = 0.01f;
    config.drift_window = 4;
    config.drift_threshold = 100.0f; /* keep drift out of this test */
    CHECK(sensor_trust_init(&ctx, &config));

    /* plausible readings first, so nothing is flagged */
    for (i = 0; i < 6; i++) {
        values[i] = 25.0f + gentle_noise(i);
    }
    /* then the channel freezes on an impossible value */
    for (i = 6; i < 10; i++) {
        values[i] = 200.0f;
    }

    for (i = 0; i < 6; i++) {
        result = feed(&ctx, values[i], (uint64_t)(i + 1) * 1000u);
    }
    CHECK(result.fault_flags == FAULT_NONE);
    CHECK(result.health_score == 100);

    /* the jump itself is RANGE + SPIKE */
    result = feed(&ctx, values[6], 7000u);
    CHECK(result.fault_flags == (FAULT_RANGE | FAULT_SPIKE));
    CHECK(result.health_score == 20); /* 100 - 55 - 25 */

    /* once the frozen value fills the window it is RANGE + STUCK as well */
    result = feed(&ctx, values[7], 8000u);
    result = feed(&ctx, values[8], 9000u);
    CHECK((result.fault_flags & FAULT_STUCK) == 0u);
    result = feed(&ctx, values[9], 10000u);
    CHECK(result.fault_flags == (FAULT_RANGE | FAULT_STUCK));
    CHECK(result.health_score == 10); /* 100 - 55 - 35 */
    CHECK(result.state == SENSOR_STATE_FAULT);
    CHECK(strcmp(sensor_trust_state_name(result.state), "FAULT") == 0);
    CHECK(result.health_score >= 0); /* clamped, never negative */

    /* all three detectors fired on the same stream */
    sensor_trust_reset(&ctx);
    summary = run_values(&ctx, values, 10, FAULT_NONE);
    CHECK(summary.union_flags == (FAULT_RANGE | FAULT_STUCK | FAULT_SPIKE));
    CHECK(summary.min_score == 10);
}

static void test_score_from_flags_is_clamped(void)
{
    CHECK(sensor_trust_score_from_flags(FAULT_NONE) == 100);
    CHECK(sensor_trust_score_from_flags(FAULT_RANGE) == 45);
    CHECK(sensor_trust_score_from_flags(FAULT_MISSING) == 45);
    CHECK(sensor_trust_score_from_flags(FAULT_STUCK) == 65);
    CHECK(sensor_trust_score_from_flags(FAULT_SPIKE) == 75);
    CHECK(sensor_trust_score_from_flags(FAULT_DRIFT) == 75);
    CHECK(sensor_trust_score_from_flags(FAULT_RANGE | FAULT_SPIKE) == 20);
    CHECK(sensor_trust_score_from_flags(FAULT_STUCK | FAULT_DRIFT) == 40);
    CHECK(sensor_trust_score_from_flags(FAULT_ALL) == 0);
    CHECK(sensor_trust_score_from_flags(FAULT_RANGE | FAULT_MISSING) == 0);
    CHECK(sensor_trust_score_from_flags(FAULT_RANGE | FAULT_STUCK | FAULT_SPIKE) == 0);

    /* a single confirmed fault never leaves the channel HEALTHY */
    CHECK(sensor_trust_state_from_score(sensor_trust_score_from_flags(FAULT_RANGE)) !=
          SENSOR_STATE_HEALTHY);
    CHECK(sensor_trust_state_from_score(sensor_trust_score_from_flags(FAULT_STUCK)) !=
          SENSOR_STATE_HEALTHY);
    CHECK(sensor_trust_state_from_score(sensor_trust_score_from_flags(FAULT_SPIKE)) !=
          SENSOR_STATE_HEALTHY);
    CHECK(sensor_trust_state_from_score(sensor_trust_score_from_flags(FAULT_DRIFT)) !=
          SENSOR_STATE_HEALTHY);
    CHECK(sensor_trust_state_from_score(sensor_trust_score_from_flags(FAULT_MISSING)) !=
          SENSOR_STATE_HEALTHY);
}

static void test_state_thresholds_and_names(void)
{
    CHECK(sensor_trust_state_from_score(100) == SENSOR_STATE_HEALTHY);
    CHECK(sensor_trust_state_from_score(80) == SENSOR_STATE_HEALTHY);
    CHECK(sensor_trust_state_from_score(79) == SENSOR_STATE_DEGRADED);
    CHECK(sensor_trust_state_from_score(65) == SENSOR_STATE_DEGRADED);
    CHECK(sensor_trust_state_from_score(50) == SENSOR_STATE_DEGRADED);
    CHECK(sensor_trust_state_from_score(49) == SENSOR_STATE_FAULT);
    CHECK(sensor_trust_state_from_score(0) == SENSOR_STATE_FAULT);

    CHECK(strcmp(sensor_trust_state_name(SENSOR_STATE_HEALTHY), "HEALTHY") == 0);
    CHECK(strcmp(sensor_trust_state_name(SENSOR_STATE_DEGRADED), "DEGRADED") == 0);
    CHECK(strcmp(sensor_trust_state_name(SENSOR_STATE_FAULT), "FAULT") == 0);

    CHECK(strcmp(sensor_trust_fault_token(FAULT_NONE), "NONE") == 0);
    CHECK(strcmp(sensor_trust_fault_token(FAULT_DRIFT), "DRIFT") == 0);
    CHECK(strcmp(sensor_trust_fault_token(FAULT_RANGE | FAULT_STUCK), "") == 0);
}

/*
 * sensor_trust_format_flags follows the snprintf contract: the return value is
 * the length of the complete string, however little of it fitted. The failure
 * mode this guards against is a buffer offset running past the end once a
 * truncation has happened, so every size is checked against a guard region
 * that must stay untouched.
 */
static void test_format_flags_truncation_is_safe(void)
{
    static const uint32_t k_flags[] = {
        FAULT_NONE,
        FAULT_RANGE,
        FAULT_RANGE | FAULT_MISSING,
        FAULT_RANGE | FAULT_STUCK | FAULT_SPIKE,
        FAULT_ALL,
    };
    static const char *const k_text[] = {
        "NONE",
        "RANGE",
        "RANGE|MISSING",
        "RANGE|STUCK|SPIKE",
        "RANGE|STUCK|SPIKE|DRIFT|MISSING",
    };
    static const size_t k_lengths[] = {4u, 5u, 13u, 17u, 31u};
    static const size_t k_sizes[] = {0u, 1u, 2u, 5u, 16u, 32u};
    const size_t case_count = sizeof(k_flags) / sizeof(k_flags[0]);
    const size_t size_count = sizeof(k_sizes) / sizeof(k_sizes[0]);
    size_t c;
    size_t s;

    /* A NULL buffer is the documented exception to the length contract: there
     * is nowhere to write, so the answer is 0 rather than a length the caller
     * cannot use. The API keeps that behaviour instead of copying snprintf. */
    CHECK(sensor_trust_format_flags(FAULT_ALL, NULL, 0u) == 0u);
    CHECK(sensor_trust_format_flags(FAULT_ALL, NULL, 64u) == 0u);
    CHECK(sensor_trust_format_flags(FAULT_NONE, NULL, 0u) == 0u);

    for (c = 0; c < case_count; c++) {
        for (s = 0; s < size_count; s++) {
            const size_t size = k_sizes[s];
            char storage[64];
            size_t length;
            size_t written;
            size_t i;
            bool untouched = true;

            memset(storage, GUARD_BYTE, sizeof(storage));
            length = sensor_trust_format_flags(k_flags[c], storage, size);

            /* the length of the complete string, however little fitted */
            CHECK(length == k_lengths[c]);

            /* nothing at or beyond `size` was written */
            for (i = size; i < sizeof(storage); i++) {
                if ((unsigned char)storage[i] != (unsigned char)GUARD_BYTE) {
                    untouched = false;
                }
            }
            CHECK(untouched);

            if (size == 0u) {
                continue; /* no room: no content, and nothing to terminate */
            }
            written = strlen(storage);
            /* Either the whole string is present, or the buffer is filled to
             * its last byte and terminated there. Never an unterminated
             * prefix, and never more characters than the size allows. */
            CHECK(written == length || written == size - 1u);
            CHECK(written < size);
            CHECK(strncmp(storage, k_text[c], written) == 0);
            CHECK(storage[written] == '\0');
        }
    }
}

static void test_reset_clears_detection_state(void)
{
    sensor_trust_config_t config = default_config();
    sensor_trust_t ctx;
    sensor_health_result_t result;
    int i;

    config.stuck_window = 8;
    config.stuck_epsilon = 0.01f;
    CHECK(sensor_trust_init(&ctx, &config));
    for (i = 0; i < 12; i++) {
        result = feed(&ctx, 25.0f, (uint64_t)(i + 1) * 1000u);
    }
    CHECK((result.fault_flags & FAULT_STUCK) != 0u);

    sensor_trust_reset(&ctx);
    result = sensor_trust_last(&ctx);
    CHECK(result.health_score == 100);
    CHECK(result.state == SENSOR_STATE_HEALTHY);
    CHECK(result.fault_flags == FAULT_NONE);
    CHECK(ctx.window_count == 0);
    CHECK(ctx.has_last_valid == false);
    CHECK(ctx.spike_confirmed_count == 0);
    CHECK(ctx.config.stuck_window == 8); /* configuration survives the reset */

    /* the history really is gone: a single sample cannot be STUCK */
    result = feed(&ctx, 25.0f, 100000u);
    CHECK(result.fault_flags == FAULT_NONE);
}

int main(void)
{
    run_test("config_validation", test_config_validation);
    run_test("config_rejects_non_finite_values", test_config_rejects_non_finite_values);
    run_test("uninitialised_context_is_safe", test_uninitialised_context_is_safe);
    run_test("reset_on_uninitialised_context_is_safe",
             test_reset_on_uninitialised_context_is_safe);
    run_test("healthy_sequence_stays_healthy", test_healthy_sequence_stays_healthy);
    run_test("range_error_both_bounds", test_range_error_both_bounds);
    run_test("stuck_detected_after_full_window", test_stuck_detected_after_full_window);
    run_test("stuck_ignores_noisy_signal", test_stuck_ignores_noisy_signal);
    run_test("spike_isolated_jump_confirmed", test_spike_isolated_jump_confirmed);
    run_test("step_change_is_flagged_once_then_accepted",
             test_step_change_is_flagged_once_then_accepted);
    run_test("spike_uses_last_valid_value_across_gap",
             test_spike_uses_last_valid_value_across_gap);
    run_test("drift_detected_on_sustained_ramp", test_drift_detected_on_sustained_ramp);
    run_test("drift_not_detected_on_short_ramp", test_drift_not_detected_on_short_ramp);
    run_test("drift_slope_is_independent_of_sampling_interval",
             test_drift_slope_is_independent_of_sampling_interval);
    run_test("drift_ignores_non_increasing_timestamps",
             test_drift_ignores_non_increasing_timestamps);
    run_test("missing_detected_after_limit_and_clears_on_recovery",
             test_missing_detected_after_limit_and_clears_on_recovery);
    run_test("stuck_and_range_combine_into_fault", test_stuck_and_range_combine_into_fault);
    run_test("score_from_flags_is_clamped", test_score_from_flags_is_clamped);
    run_test("state_thresholds_and_names", test_state_thresholds_and_names);
    run_test("format_flags_truncation_is_safe", test_format_flags_truncation_is_safe);
    run_test("reset_clears_detection_state", test_reset_clears_detection_state);

    printf("\n%d tests, %d passed, %d failed (%d checks)\n", g_tests_run,
           g_tests_run - g_tests_failed, g_tests_failed, g_checks);
    return (g_tests_failed == 0) ? 0 : 1;
}
