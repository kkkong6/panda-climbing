#pragma once
typedef void* TaskHandle_t;
inline void vTaskDelete(TaskHandle_t) {}
inline void vTaskDelay(TickType_t) {}
inline TickType_t xTaskGetTickCount() { return 0; }
inline void vTaskDelayUntil(TickType_t*, TickType_t) {}
inline int xTaskCreate(void (*)(void*), const char*, int, void*, int, TaskHandle_t*) { return 1; }
