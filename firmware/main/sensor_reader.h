#ifndef SENSOR_READER_H
#define SENSOR_READER_H

#include <stdbool.h>
#include <stdint.h>

typedef struct {
    float temperature_c;
    float humidity_percent;
    bool valid;
    uint64_t timestamp_ms;
} sht30_reading_t;

/* Real SHT30 I2C only. Failure returns false; there is no synthetic fallback. */
bool sensor_reader_init(void);
sht30_reading_t sensor_reader_read(void);

#endif
