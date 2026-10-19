#pragma once
#include <cstdio>
#define ESP_LOGI(tag, fmt, ...) printf("[I] " fmt "\n", ##__VA_ARGS__)
#define ESP_LOGW(tag, fmt, ...) printf("[W] " fmt "\n", ##__VA_ARGS__)
#define ESP_LOGE(tag, fmt, ...) printf("[E] " fmt "\n", ##__VA_ARGS__)
