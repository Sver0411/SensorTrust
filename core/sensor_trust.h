/*
 * SensorTrust v0.1 - lightweight sensor health monitoring for embedded IoT nodes.
 *
 * Answers one question only:
 *     "Can the current reading from this sensor channel be trusted?"
 *
 * It does NOT decide whether the environment is abnormal, and it does NOT
 * decide when to sample or upload (that is the caller's / scheduler's job).
 *
 * Design constraints:
 *   - portable C11, no dynamic memory, no RTOS / ESP-IDF dependency
 *   - fixed-size history buffer (SENSOR_TRUST_MAX_WINDOW samples)
 *   - O(window) per sample, integer + single-precision float only
 *
 * A detected fault means "the data looks suspicious", not
 * "the sensor is physically broken". See README.md, section Limitations.
 */
#ifndef SENSOR_TRUST_H
#define SENSOR_TRUST_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ------------------------------------------------------------------ */
/* Compile-time limits and defaults                                    */
/* ------------------------------------------------------------------ */

/* Largest configurable detection window, in samples. The context keeps a
 * fixed ring buffer of this size; it never allocates. */
#ifndef SENSOR_TRUST_MAX_WINDOW
#define SENSOR_TRUST_MAX_WINDOW 64
#endif

/* Full score at startup; penalties are subtracted per active fault.
 *
 * The weights are chosen so that a single confirmed fault never leaves the
 * channel in HEALTHY: an impossible value or missing data is FAULT (the data
 * cannot be used), a frozen or moving-suspicious channel is DEGRADED (the
 * data is usable but should not be trusted blindly). Faults accumulate, and
 * the score is clamped at 0. Overridable at build time. */
#define SENSOR_TRUST_SCORE_START 100

#ifndef SENSOR_TRUST_PENALTY_RANGE
#define SENSOR_TRUST_PENALTY_RANGE 55
#endif
#ifndef SENSOR_TRUST_PENALTY_STUCK
#define SENSOR_TRUST_PENALTY_STUCK 35
#endif
#ifndef SENSOR_TRUST_PENALTY_SPIKE
#define SENSOR_TRUST_PENALTY_SPIKE 25
#endif
#ifndef SENSOR_TRUST_PENALTY_DRIFT
#define SENSOR_TRUST_PENALTY_DRIFT 25
#endif
#ifndef SENSOR_TRUST_PENALTY_MISSING
#define SENSOR_TRUST_PENALTY_MISSING 55
#endif

/* Score -> state boundaries (inclusive lower bound). */
#define SENSOR_TRUST_HEALTHY_MIN_SCORE 80
#define SENSOR_TRUST_DEGRADED_MIN_SCORE 50

/* ------------------------------------------------------------------ */
/* Public types                                                        */
/* ------------------------------------------------------------------ */

/* Fault bitmask. FAULT_NONE is reported when nothing suspicious is seen. */
#define FAULT_NONE 0u
#define FAULT_RANGE (1u << 0)   /* value outside the configured physical range */
#define FAULT_STUCK (1u << 1)   /* value almost constant for a long window     */
#define FAULT_SPIKE (1u << 2)   /* sudden jump away from the previous value    */
#define FAULT_DRIFT (1u << 3)   /* sustained one-directional trend             */
#define FAULT_MISSING (1u << 4) /* invalid / NaN / read-failure samples        */

#define FAULT_ALL                                                         \
    (FAULT_RANGE | FAULT_STUCK | FAULT_SPIKE | FAULT_DRIFT | FAULT_MISSING)

typedef enum {
    SENSOR_STATE_HEALTHY = 0,
    SENSOR_STATE_DEGRADED = 1,
    SENSOR_STATE_FAULT = 2
} sensor_health_state_t;

/*
 * Per-channel configuration. All thresholds are configurable; nothing about
 * a specific physical sensor is hard-coded in the detection logic.
 *
 * Units:
 *   min_value / max_value / stuck_epsilon / spike_threshold
 *                       -> in the sensor's own unit (e.g. degree Celsius)
 *   drift_threshold     -> in the sensor's own unit PER SECOND (e.g. C/s)
 *   stuck_window / drift_window / missing_limit -> in samples
 *
 * drift_threshold is a slope over real time: |delta value| per second, taken
 * from the sample timestamps. It does not depend on the sampling interval, so
 * the same physical trend is judged the same way at 1 Hz or at 0.2 Hz, which
 * matters as soon as a scheduler is free to change the interval at runtime.
 * At 1 Hz a rate of "0.01 per second" and "0.01 per sample" coincide, which is
 * why the older scenario numbers did not have to change.
 */
typedef struct {
    float min_value;       /* smallest physically plausible value      */
    float max_value;       /* largest physically plausible value       */
    float stuck_epsilon;   /* max-min below this = "not moving"        */
    int stuck_window;      /* samples inspected for STUCK              */
    float spike_threshold; /* jump larger than this = candidate SPIKE  */
    int drift_window;      /* samples used for the trend fit           */
    float drift_threshold; /* abs(slope) in units/second = candidate   */
    int missing_limit;     /* consecutive invalid samples = MISSING    */
} sensor_trust_config_t;

/*
 * One reading of one sensor channel. `valid` is set to false by the caller
 * for read failures, timeouts, stale registers, NaN, ...
 *
 * `timestamp_ms` is not decoration: the DRIFT detector fits its slope against
 * real time and therefore needs a clock that moves forward. It must come from
 * a monotonic source, and consecutive valid samples should carry increasing
 * timestamps. If a timestamp stays still or rolls back, the affected window is
 * simply not used for a DRIFT verdict (see the notes below).
 */
typedef struct {
    float value;
    bool valid;
    uint64_t timestamp_ms;
} sensor_sample_t;

/* Result for the most recent sample. */
typedef struct {
    int health_score;               /* 0..100, heuristic severity        */
    sensor_health_state_t state;    /* HEALTHY / DEGRADED / FAULT        */
    uint32_t fault_flags;           /* bitmask of FAULT_*                */
} sensor_health_result_t;

/*
 * One valid sample as kept in the history window. The timestamp is stored next
 * to the value because the DRIFT slope is measured against real time; the two
 * live in one struct so they cannot drift apart and desynchronise the fit.
 */
typedef struct {
    float value;
    uint64_t timestamp_ms;
} sensor_trust_window_point_t;

/*
 * Context for one sensor channel. Sized for stack / .bss placement, so the
 * struct is public, but treat the fields as private: use the API below.
 */
typedef struct {
    sensor_trust_config_t config;
    bool initialised;

    /* Ring buffer of the most recent valid points, oldest at window_head. */
    sensor_trust_window_point_t window[SENSOR_TRUST_MAX_WINDOW];
    int window_capacity; /* max(stuck_window, drift_window), <= MAX_WINDOW */
    int window_count;    /* valid samples currently stored (<= capacity)    */
    int window_head;     /* read index of the oldest stored sample          */

    int consecutive_invalid;   /* invalid samples since the last valid one  */
    bool has_last_valid;       /* true once at least one valid sample came  */
    float last_valid_value;    /* previous valid value, used by SPIKE       */
    uint64_t last_valid_ts;

    bool spike_pending;        /* a jump was seen, next sample may confirm  */
    float spike_reference;     /* value the sensor jumped away from         */
    int spike_confirmed_count; /* confirmed spikes so far (diagnostics)     */

    int drift_streak;     /* consecutive same-direction drift verdicts     */
    int drift_direction;  /* -1 falling, 0 none, +1 rising                 */

    sensor_health_result_t last_result;
} sensor_trust_t;

/* ------------------------------------------------------------------ */
/* API                                                                 */
/* ------------------------------------------------------------------ */

/* A generic, sensor-agnostic configuration (range -100..100, 1 Hz-ish
 * windows). Typical use: copy it, then override the physical range. */
void sensor_trust_default_config(sensor_trust_config_t *config);

/* True when every field is self-consistent and the windows fit in
 * SENSOR_TRUST_MAX_WINDOW. Every float field must be finite: NaN and +-Inf are
 * rejected, so a configuration can never turn a detector into nonsense.
 * SensorTrust never applies a partially valid configuration. */
bool sensor_trust_config_is_valid(const sensor_trust_config_t *config);

/*
 * Prepare a context for one channel.
 * Returns false (and leaves the context unusable) when the configuration is
 * invalid; an unusable context reports FAULT with score 0.
 * A usable context starts with no history: score 100 / HEALTHY / no flags
 * until the first sample arrives.
 */
bool sensor_trust_init(sensor_trust_t *ctx, const sensor_trust_config_t *config);

/* Clear all history and detection state, keep the configuration. The channel
 * is then back in its "no sample seen yet" state (score 100, HEALTHY, no
 * flags), so a restart does not raise a startup alarm.
 *
 * This only restarts a context that is really running: one that was
 * initialised successfully and whose configuration is still valid. Resetting a
 * context that was never initialised, or one whose init failed, leaves it
 * uninitialised and unusable rather than fabricating a working channel out of
 * an unconfigured struct. A NULL context is ignored. */
void sensor_trust_reset(sensor_trust_t *ctx);

/*
 * Feed one sample and get the health of the channel as of this sample.
 * Cheap enough to call on every reading. Invalid samples only report
 * MISSING (once missing_limit is reached); the window based detectors resume
 * on the next valid sample.
 */
sensor_health_result_t sensor_trust_update(sensor_trust_t *ctx,
                                           const sensor_sample_t *sample);

/* Latest result without feeding a new sample. */
sensor_health_result_t sensor_trust_last(const sensor_trust_t *ctx);

/* --------------------------------------------------------------------- */
/* Notes on how the detectors behave                                      */
/* --------------------------------------------------------------------- */
/*
 * - The detectors are independent: one sample can carry several flags. A
 *   garbage reading that jumps away from the previous value is normally both
 *   RANGE and SPIKE, and an out-of-range value that then freezes is also
 *   STUCK.
 * - STUCK looks at the ring buffer of the most recent valid values, so its
 *   window is counted in SAMPLES, not in seconds. "The reading has not moved"
 *   is a statement about the readings, so the wall clock is irrelevant to it.
 * - DRIFT uses the SAME ring buffer but measures its slope against real time:
 *   value units per second, from `timestamp_ms`. Its verdict is therefore
 *   unchanged when the sampling interval changes, which is what makes the
 *   thresholds safe to use with a scheduler that varies the interval.
 * - An invalid sample reports MISSING only. It does not feed the value
 *   window, and it restarts the DRIFT trend counter, so a trend is only
 *   claimed when it is continuously observed.
 * - DRIFT requires |slope| > drift_threshold to hold for drift_window
 *   consecutive samples, i.e. a trend has to last roughly two windows
 *   (about 48 s at 1 Hz with the defaults) before it is reported as
 *   suspected drift. A short or curved transient is not drift.
 * - A window whose timestamps do not strictly increase - a stopped or
 *   rolling-back clock - produces no slope at all, so it cannot report DRIFT,
 *   cannot divide by zero and cannot produce NaN/Inf. RANGE, SPIKE and STUCK
 *   still work on those samples, because none of them uses time.
 */

/* Score contributed by a fault bitmask (100 minus penalties, clamped 0..100). */
int sensor_trust_score_from_flags(uint32_t fault_flags);

/* Map a score onto HEALTHY / DEGRADED / FAULT. */
sensor_health_state_t sensor_trust_state_from_score(int health_score);

/* "HEALTHY" / "DEGRADED" / "FAULT". */
const char *sensor_trust_state_name(sensor_health_state_t state);

/* Token for a single fault bit: "NONE", "RANGE", "STUCK", "SPIKE",
 * "DRIFT", "MISSING"; "" when the argument is not a single known bit. */
const char *sensor_trust_fault_token(uint32_t fault_flag);

/* "NONE" or "RANGE|STUCK|..." into buffer.
 *
 * Returns the number of characters the COMPLETE string needs, excluding the
 * terminator - the snprintf contract, so a caller can size a buffer by calling
 * once with a small buffer and reading the return value. Truncation is safe:
 * at most `buffer_size` bytes are ever touched, the result is always
 * NUL-terminated when buffer_size >= 1, and `buffer + n` is never formed for
 * n >= buffer_size.
 *
 * The one exception: a NULL buffer returns 0, because there is nothing to
 * measure the string against. */
size_t sensor_trust_format_flags(uint32_t fault_flags, char *buffer,
                                 size_t buffer_size);

#ifdef __cplusplus
}
#endif

#endif /* SENSOR_TRUST_H */
