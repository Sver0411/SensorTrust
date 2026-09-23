#include "sensor_reader.h"

#include <math.h>

#include "driver/i2c_master.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "experiment_config.h"

static i2c_master_bus_handle_t bus;
static i2c_master_dev_handle_t sht30;

static uint8_t sht_crc(const uint8_t *data)
{
    uint8_t crc = 0xff;
    for (int i = 0; i < 2; ++i) {
        crc ^= data[i];
        for (int bit = 0; bit < 8; ++bit) {
            crc = (crc & 0x80u) ? (uint8_t)((crc << 1) ^ 0x31u)
                                : (uint8_t)(crc << 1);
        }
    }
    return crc;
}

sht30_reading_t sensor_reader_read(void)
{
    sht30_reading_t reading = {0};
    const uint8_t command[2] = {0x24, 0x00}; /* single shot, high repeatability */
    uint8_t data[6] = {0};
    esp_err_t error = i2c_master_transmit(sht30, command, sizeof(command), 100);
    if (error == ESP_OK) {
        vTaskDelay(pdMS_TO_TICKS(20));
        error = i2c_master_receive(sht30, data, sizeof(data), 100);
    }
    reading.timestamp_ms = (uint64_t)(esp_timer_get_time() / 1000);
    if (error != ESP_OK || sht_crc(data) != data[2] || sht_crc(data + 3) != data[5]) {
        return reading;
    }
    uint16_t raw_t = (uint16_t)((data[0] << 8) | data[1]);
    uint16_t raw_h = (uint16_t)((data[3] << 8) | data[4]);
    reading.temperature_c = -45.0f + 175.0f * (float)raw_t / 65535.0f;
    reading.humidity_percent = 100.0f * (float)raw_h / 65535.0f;
    reading.valid = isfinite(reading.temperature_c) && isfinite(reading.humidity_percent) &&
                    reading.temperature_c >= -40.0f && reading.temperature_c <= 85.0f &&
                    reading.humidity_percent >= 0.0f && reading.humidity_percent <= 100.0f;
    return reading;
}

bool sensor_reader_init(void)
{
    i2c_master_bus_config_t bus_config = {
        .i2c_port = I2C_NUM_0,
        .sda_io_num = EXP_SDA_GPIO,
        .scl_io_num = EXP_SCL_GPIO,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = true,
    };
    i2c_device_config_t dev_config = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = EXP_SENSOR_ADDRESS,
        .scl_speed_hz = 100000,
    };
    if (i2c_new_master_bus(&bus_config, &bus) != ESP_OK ||
        i2c_master_bus_add_device(bus, &dev_config, &sht30) != ESP_OK) {
        return false;
    }
    for (int attempt = 0; attempt < 5; ++attempt) {
        if (sensor_reader_read().valid) {
            return true;
        }
        vTaskDelay(pdMS_TO_TICKS(100));
    }
    return false;
}
