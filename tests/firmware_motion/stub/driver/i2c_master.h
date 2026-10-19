#pragma once
#include <cstdint>
#include <cstddef>
typedef int esp_err_t;
#define ESP_OK 0
typedef void* i2c_master_bus_handle_t;
typedef void* i2c_master_dev_handle_t;
enum { I2C_ADDR_BIT_LEN_7 = 0 };
typedef struct { int dev_addr_length; uint16_t device_address; uint32_t scl_speed_hz; } i2c_device_config_t;
inline esp_err_t i2c_master_bus_add_device(i2c_master_bus_handle_t, const i2c_device_config_t*, i2c_master_dev_handle_t*) { return 0; }
inline esp_err_t i2c_master_bus_rm_device(i2c_master_dev_handle_t) { return 0; }
inline esp_err_t i2c_master_transmit(i2c_master_dev_handle_t, const uint8_t*, size_t, int) { return 0; }
inline esp_err_t i2c_master_transmit_receive(i2c_master_dev_handle_t, const uint8_t*, size_t, uint8_t*, size_t, int) { return 0; }
