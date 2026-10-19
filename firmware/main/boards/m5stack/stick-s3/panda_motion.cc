#include "panda_motion.h"

#include <esp_log.h>
#include <esp_timer.h>
#include <algorithm>
#include <cmath>
#include <cstring>

#define TAG "PandaMotion"

// BMI270 的 8KB 初始化配置（来自 espressif/bmi270_sensor 组件里博世官方的配置文件）
extern "C" const uint8_t bmi270_config_file[];
static constexpr size_t kConfigSize = 8192;

namespace {
// BMI270 寄存器
constexpr uint8_t REG_CHIP_ID = 0x00;
constexpr uint8_t REG_DATA_ACC = 0x0C;  // acc x/y/z, gyr x/y/z，各 2 字节小端
constexpr uint8_t REG_INTERNAL_STATUS = 0x21;
constexpr uint8_t REG_ACC_CONF = 0x40;
constexpr uint8_t REG_ACC_RANGE = 0x41;
constexpr uint8_t REG_GYR_CONF = 0x42;
constexpr uint8_t REG_GYR_RANGE = 0x43;
constexpr uint8_t REG_INIT_CTRL = 0x59;
constexpr uint8_t REG_INIT_ADDR_0 = 0x5B;
constexpr uint8_t REG_INIT_DATA = 0x5E;
constexpr uint8_t REG_PWR_CONF = 0x7C;
constexpr uint8_t REG_PWR_CTRL = 0x7D;
constexpr uint8_t REG_CMD = 0x7E;
constexpr uint8_t CHIP_ID = 0x24;

constexpr float kAccLsbPerG = 2048.0f;   // ±16g
constexpr float kGyrLsbPerDps = 16.4f;   // ±2000°/s
constexpr float kG = 9.81f;

int64_t NowMs() { return esp_timer_get_time() / 1000; }
}  // namespace

PandaMotion::PandaMotion(i2c_master_bus_handle_t bus, uint8_t addr) : bus_(bus), addr_(addr) {}

PandaMotion::~PandaMotion() {
    if (task_) {
        vTaskDelete(task_);
    }
    if (dev_) {
        i2c_master_bus_rm_device(dev_);
    }
}

void PandaMotion::SetCallback(Callback cb) {
    std::lock_guard<std::mutex> lock(mutex_);
    callback_ = std::move(cb);
}

bool PandaMotion::WriteReg(uint8_t reg, uint8_t value) {
    uint8_t buf[2] = {reg, value};
    return i2c_master_transmit(dev_, buf, 2, 50) == ESP_OK;
}

bool PandaMotion::WriteBurst(uint8_t reg, const uint8_t* data, size_t len) {
    uint8_t buf[129];
    if (len > sizeof(buf) - 1) {
        return false;
    }
    buf[0] = reg;
    memcpy(buf + 1, data, len);
    return i2c_master_transmit(dev_, buf, len + 1, 100) == ESP_OK;
}

bool PandaMotion::ReadRegs(uint8_t reg, uint8_t* data, size_t len) {
    return i2c_master_transmit_receive(dev_, &reg, 1, data, len, 30) == ESP_OK;
}

bool PandaMotion::InitSensor() {
    uint8_t id = 0;
    if (!ReadRegs(REG_CHIP_ID, &id, 1) || id != CHIP_ID) {
        ESP_LOGW(TAG, "BMI270 not found (chip id 0x%02x)", id);
        return false;
    }
    // 博世初始化流程：软复位 → 关高级省电 → 上传配置 → 等内部状态 = 初始化完成
    WriteReg(REG_CMD, 0xB6);
    vTaskDelay(pdMS_TO_TICKS(20));
    WriteReg(REG_PWR_CONF, 0x00);
    vTaskDelay(pdMS_TO_TICKS(10));
    WriteReg(REG_INIT_CTRL, 0x00);
    constexpr size_t kChunk = 128;
    for (size_t off = 0; off < kConfigSize; off += kChunk) {
        uint16_t word = off / 2;
        uint8_t addr[2] = {(uint8_t)(word & 0x0F), (uint8_t)(word >> 4)};
        if (!WriteBurst(REG_INIT_ADDR_0, addr, 2) ||
            !WriteBurst(REG_INIT_DATA, bmi270_config_file + off, std::min(kChunk, kConfigSize - off))) {
            ESP_LOGW(TAG, "BMI270 config upload failed at %u", (unsigned)off);
            return false;
        }
    }
    WriteReg(REG_INIT_CTRL, 0x01);
    vTaskDelay(pdMS_TO_TICKS(30));
    uint8_t status = 0;
    ReadRegs(REG_INTERNAL_STATUS, &status, 1);
    if ((status & 0x0F) != 0x01) {
        // 配置没装好时加速度 / 陀螺仪原始数据通常仍可读，只是高级功能不可用；继续试
        ESP_LOGW(TAG, "BMI270 internal status 0x%02x (expected 0x01)", status);
    }
    // 加速度 100Hz ±16g，陀螺仪 100Hz ±2000°/s，性能模式
    WriteReg(REG_ACC_CONF, 0xA8);
    WriteReg(REG_ACC_RANGE, 0x03);
    WriteReg(REG_GYR_CONF, 0xA8);
    WriteReg(REG_GYR_RANGE, 0x00);
    WriteReg(REG_PWR_CTRL, 0x0E);  // 开加速度、陀螺仪、温度
    WriteReg(REG_PWR_CONF, 0x02);  // 关高级省电，保持 FIFO 自唤醒
    vTaskDelay(pdMS_TO_TICKS(50));
    uint8_t raw[12];
    if (!ReadRegs(REG_DATA_ACC, raw, sizeof(raw))) {
        return false;
    }
    int16_t az = (int16_t)(raw[4] | (raw[5] << 8));
    ESP_LOGI(TAG, "BMI270 ready, status 0x%02x, az=%.2fg", status, az / kAccLsbPerG);
    return true;
}

bool PandaMotion::Start() {
    i2c_device_config_t cfg = {};
    cfg.dev_addr_length = I2C_ADDR_BIT_LEN_7;
    cfg.device_address = addr_;
    cfg.scl_speed_hz = 400000;
    if (i2c_master_bus_add_device(bus_, &cfg, &dev_) != ESP_OK) {
        ESP_LOGW(TAG, "add I2C device failed");
        return false;
    }
    if (!InitSensor()) {
        return false;
    }
    ready_ = true;
    xTaskCreate([](void* arg) { static_cast<PandaMotion*>(arg)->Task(); }, "panda_motion", 4096, this, 3, &task_);
    return true;
}

void PandaMotion::Task() {
    TickType_t last = xTaskGetTickCount();
    const TickType_t period = std::max<TickType_t>(1, pdMS_TO_TICKS(10));
    int errors = 0;
    while (true) {
        vTaskDelayUntil(&last, period);
        uint8_t raw[12];
        if (!ReadRegs(REG_DATA_ACC, raw, sizeof(raw))) {
            if (++errors % 500 == 1) {
                ESP_LOGW(TAG, "IMU read failed (%d)", errors);
            }
            continue;
        }
        float v[6];
        for (int i = 0; i < 6; i++) {
            v[i] = (int16_t)(raw[i * 2] | (raw[i * 2 + 1] << 8));
        }
        float a = sqrtf(v[0] * v[0] + v[1] * v[1] + v[2] * v[2]) / kAccLsbPerG;
        float w = sqrtf(v[3] * v[3] + v[4] * v[4] + v[5] * v[5]) / kGyrLsbPerDps;
        Process(a, w, NowMs());
    }
}

PandaMotion::Climb PandaMotion::CurrentClimb(int64_t end_ms) const {
    Climb c;
    if (seg_start_ms_ == 0 || end_ms - last_active_ms_ > kSegmentGapMs) {
        return c;  // 掉下来之前没有持续活动（比如被碰掉的）
    }
    c.valid = true;
    c.start_ms = seg_start_ms_;
    c.end_ms = end_ms;
    c.jerk = seg_jerk_n_ ? seg_jerk_sum_ / seg_jerk_n_ : 0;
    c.swings = seg_swings_;
    return c;
}

void PandaMotion::Emit(const char* event, float a, float b) {
    Callback cb;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        cb = callback_;
    }
    ESP_LOGI(TAG, "event %s %.2f %.2f", event, a, b);
    if (cb) {
        cb(event, a, b);
    }
}

void PandaMotion::Process(float a, float w, int64_t now_ms) {
    const char* event = nullptr;
    float ea = 0, eb = 0;
    {
        std::lock_guard<std::mutex> lock(mutex_);

        // ---- 掉落：失重 → 冲击 ----
        switch (fall_state_) {
            case kIdle:
                if (a < kFreeFallG) {
                    fall_state_ = kFreeFall;
                    ff_start_ms_ = now_ms;
                    ff_climb_ = CurrentClimb(now_ms);  // 掉下来那一刻，攀爬结束
                }
                break;
            case kFreeFall:
                if (a >= kFreeFallEndG) {
                    ff_dur_ms_ = now_ms - ff_start_ms_;
                    if (ff_dur_ms_ >= kMinFreeFallMs) {
                        fall_state_ = kImpact;
                        impact_peak_ = a;
                        impact_end_ms_ = now_ms + kImpactWindowMs;
                    } else {
                        fall_state_ = kIdle;  // 太短：粉袋晃了一下
                    }
                }
                break;
            case kImpact:
                impact_peak_ = std::max(impact_peak_, a);
                if (now_ms >= impact_end_ms_) {
                    float t = std::min<int64_t>(ff_dur_ms_, 1200) / 1000.0f;
                    Fall f;
                    f.valid = true;
                    f.time_ms = ff_start_ms_;
                    f.h = std::min(6.0f, 0.5f * kG * t * t);
                    f.impact = impact_peak_;
                    f.climb = ff_climb_;
                    last_fall_ = f;
                    fall_state_ = kIdle;
                    ESP_LOGI(TAG, "fall %.2fm impact %.1fg (free fall %lldms)", f.h, f.impact, ff_dur_ms_);
                    if (f.impact >= kHardImpactG) {
                        event = "hard_fall";
                        ea = f.impact;
                        eb = f.h;
                        watching_ = true;
                        watch_until_ms_ = now_ms + kStillWatchSeconds * 1000;
                        still_secs_ = 0;
                        moving_secs_ = 0;
                    }
                }
                break;
        }

        // ---- 晃动 ----
        if (w > kSwingDps && now_ms - last_swing_ms_ > 500) {
            last_swing_ms_ = now_ms;
            if (seg_start_ms_ != 0) {
                seg_swings_++;
            }
        }

        // ---- 1 秒窗口 ----
        if (win_n_ == 0) {
            win_start_ms_ = now_ms;
        }
        win_n_++;
        win_sum_a_ += a;
        win_sum_a2_ += a * a;
        win_sum_w_ += w;
        win_jerk_ += fabsf(a - prev_a_);
        prev_a_ = a;
        if (now_ms - win_start_ms_ >= 1000) {
            float n = win_n_;
            float mean = win_sum_a_ / n;
            float sd = sqrtf(std::max(0.0f, win_sum_a2_ / n - mean * mean));
            float wm = win_sum_w_ / n;
            bool active = sd > 0.06f || wm > 20.0f;
            bool still = sd < 0.025f && wm < 6.0f;
            if (active) {
                if (seg_start_ms_ == 0 || win_start_ms_ - last_active_ms_ > kSegmentGapMs) {
                    seg_start_ms_ = win_start_ms_;  // 新的一段活动
                    seg_jerk_sum_ = 0;
                    seg_jerk_n_ = 0;
                    seg_swings_ = 0;
                }
                last_active_ms_ = now_ms;
                seg_jerk_sum_ += win_jerk_;
                seg_jerk_n_ += win_n_;
            }
            if (watching_) {
                still_secs_ = still ? still_secs_ + 1 : 0;
                moving_secs_ = active ? moving_secs_ + 1 : 0;
                if (still_secs_ >= kStillSeconds) {
                    watching_ = false;
                    if (event == nullptr) {
                        event = "still";
                        ea = still_secs_;
                    }
                } else if (moving_secs_ >= kMovedSeconds || now_ms > watch_until_ms_) {
                    watching_ = false;  // 起来连续活动了 / 过了观察期：人能自己动，不再盯
                }
            }
            win_n_ = 0;
            win_sum_a_ = win_sum_a2_ = win_sum_w_ = win_jerk_ = 0;
        }
    }
    if (event) {
        Emit(event, ea, eb);
    }
}

std::string PandaMotion::TakeAttempt() {
    if (!ready_) {
        return "";
    }
    std::lock_guard<std::mutex> lock(mutex_);
    int64_t now = NowMs();
    bool has_fall = last_fall_.valid && last_fall_.time_ms > last_take_ms_ && now - last_fall_.time_ms < 120000;
    Climb climb;
    int64_t end_ms = now;
    if (has_fall) {
        climb = last_fall_.climb;
        end_ms = last_fall_.time_ms;
    } else if (seg_start_ms_ != 0 && now - last_active_ms_ < 20000) {
        climb = CurrentClimb(last_active_ms_);  // 没掉下来（爬下来 / 完攀走下来）
        end_ms = last_active_ms_;
    }
    char buf[160] = {0};
    int len = 0;
    auto add = [&](const char* fmt, auto v) {
        len += snprintf(buf + len, sizeof(buf) - len, "%s", len ? "," : "");
        len += snprintf(buf + len, sizeof(buf) - len, fmt, v);
    };
    if (has_fall) {
        add("\"fall_m\":%.2f", (double)last_fall_.h);
        add("\"impact_g\":%.1f", (double)last_fall_.impact);
    }
    if (climb.valid) {
        int climb_s = (int)((climb.end_ms - climb.start_ms) / 1000);
        if (climb_s >= 3 && climb_s <= 900) {
            add("\"climb_s\":%d", climb_s);
            add("\"swings\":%d", climb.swings);
            // 平均每个采样加速度变化 0.01g 很平稳 → 100；0.07g 很顿 → 10
            int smooth = (int)std::clamp(100.0f - (climb.jerk - 0.01f) * 1500.0f, 0.0f, 100.0f);
            add("\"smooth\":%d", smooth);
        }
        if (prev_end_ms_ != 0 && climb.start_ms > prev_end_ms_) {
            add("\"rest_s\":%d", (int)((climb.start_ms - prev_end_ms_) / 1000));
        }
    }
    prev_end_ms_ = end_ms;
    last_take_ms_ = now;
    seg_start_ms_ = 0;  // 下一次攀爬重新算
    ESP_LOGI(TAG, "attempt motion {%s}", buf);
    return len ? std::string(buf, len) : std::string();
}
