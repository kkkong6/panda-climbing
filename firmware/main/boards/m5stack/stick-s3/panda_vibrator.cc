#include "panda_vibrator.h"

#include <driver/ledc.h>
#include <esp_log.h>
#include <cstring>

#define TAG "PandaVibrator"

// 背光用了 LEDC_TIMER_0 / CHANNEL_0，马达用 1 号
#define VIB_TIMER    LEDC_TIMER_1
#define VIB_CHANNEL  LEDC_CHANNEL_1
#define VIB_RES      LEDC_TIMER_10_BIT
#define VIB_MAX_DUTY ((1 << 10) - 1)
#define POWER_IDLE_OFF_MS 2000  // 震完 2 秒没有新震动就关 5V

namespace {
struct Pattern {
    const char* name;
    struct { uint16_t on_ms; uint16_t off_ms; } steps[4];
    uint8_t count;
    bool repeat;
};
// 每段震动不超过 0.6 秒，和 03 文档「每次只震 0.2–0.3 秒」的原则一致（长震稍长才能区分）
const Pattern kPatterns[] = {
    {"short1", {{200, 0}}, 1, false},
    {"short2", {{180, 150}, {180, 0}}, 2, false},
    {"long1", {{600, 0}}, 1, false},
    {"triple", {{150, 120}, {150, 120}, {150, 0}}, 3, false},
    {"long2", {{600, 300}, {600, 0}}, 2, false},
    {"sos", {{500, 500}}, 1, true},
};
}  // namespace

PandaVibrator::PandaVibrator(gpio_num_t pin, std::function<void(bool)> power, int duty_percent)
    : pin_(pin), power_(std::move(power)), duty_percent_(duty_percent) {
    ledc_timer_config_t timer = {
        .speed_mode = LEDC_LOW_SPEED_MODE,
        .duty_resolution = VIB_RES,
        .timer_num = VIB_TIMER,
        .freq_hz = 20000,  // 20kHz，人耳听不到 PWM 啸叫
        .clk_cfg = LEDC_AUTO_CLK,
        .deconfigure = false,
    };
    ESP_ERROR_CHECK(ledc_timer_config(&timer));
    ledc_channel_config_t ch = {
        .gpio_num = pin_,
        .speed_mode = LEDC_LOW_SPEED_MODE,
        .channel = VIB_CHANNEL,
        .intr_type = LEDC_INTR_DISABLE,
        .timer_sel = VIB_TIMER,
        .duty = 0,
        .hpoint = 0,
        .flags = {.output_invert = 0},
    };
    ESP_ERROR_CHECK(ledc_channel_config(&ch));

    queue_ = xQueueCreate(4, sizeof(Request));
    xTaskCreate([](void* arg) { static_cast<PandaVibrator*>(arg)->Task(); }, "panda_vib", 3072, this, 4, &task_);
    ESP_LOGI(TAG, "Vibrator on GPIO%d, strength %d%%", pin_, duty_percent_);
}

PandaVibrator::~PandaVibrator() {
    if (task_) vTaskDelete(task_);
    if (queue_) vQueueDelete(queue_);
    Motor(false);
}

void PandaVibrator::SetStrength(int duty_percent) {
    if (duty_percent < 10) duty_percent = 10;
    if (duty_percent > 60) duty_percent = 60;  // 超过 60% 可能超 Grove 口电流上限
    duty_percent_ = duty_percent;
}

bool PandaVibrator::Play(const std::string& pattern) {
    for (auto& p : kPatterns) {
        if (pattern == p.name) {
            Request r = {reinterpret_cast<const Step*>(p.steps), p.count, p.repeat};
            xQueueReset(queue_);  // 新的打断旧的
            xQueueSend(queue_, &r, 0);
            ESP_LOGI(TAG, "Play %s", p.name);
            return true;
        }
    }
    ESP_LOGW(TAG, "Unknown pattern: %s", pattern.c_str());
    return false;
}

void PandaVibrator::Stop() {
    Request r = {nullptr, 0, false};
    xQueueReset(queue_);
    xQueueSend(queue_, &r, 0);
}

void PandaVibrator::Motor(bool on) {
    uint32_t duty = on ? (VIB_MAX_DUTY * duty_percent_ / 100) : 0;
    ledc_set_duty(LEDC_LOW_SPEED_MODE, VIB_CHANNEL, duty);
    ledc_update_duty(LEDC_LOW_SPEED_MODE, VIB_CHANNEL);
}

// 等 ms 毫秒；期间来了新请求就返回 true（被打断）
bool PandaVibrator::WaitOrInterrupt(int ms, Request& next, bool& got_next) {
    if (ms <= 0) return false;
    if (xQueueReceive(queue_, &next, pdMS_TO_TICKS(ms)) == pdTRUE) {
        got_next = true;
        return true;
    }
    return false;
}

void PandaVibrator::Task() {
    Request req;
    bool have = false;
    while (true) {
        if (!have) {
            // 空闲：等新请求，等太久就关 5V 省电
            if (xQueueReceive(queue_, &req, pdMS_TO_TICKS(POWER_IDLE_OFF_MS)) != pdTRUE) {
                if (powered_) {
                    power_(false);
                    powered_ = false;
                    ESP_LOGI(TAG, "Grove 5V off (idle)");
                }
                continue;
            }
        }
        have = false;
        if (req.steps == nullptr || req.count == 0) {
            Motor(false);
            continue;  // Stop
        }
        if (!powered_) {
            power_(true);
            powered_ = true;
            vTaskDelay(pdMS_TO_TICKS(20));  // 等升压稳定
        }
        bool interrupted = false;
        do {
            for (int i = 0; i < req.count && !interrupted; i++) {
                Motor(true);
                interrupted = WaitOrInterrupt(req.steps[i].on_ms, req, have);
                Motor(false);
                if (!interrupted) {
                    interrupted = WaitOrInterrupt(req.steps[i].off_ms, req, have);
                }
            }
        } while (req.repeat && !interrupted);
        Motor(false);
    }
}
