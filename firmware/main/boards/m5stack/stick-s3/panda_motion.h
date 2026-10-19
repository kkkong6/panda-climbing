#ifndef PANDA_MOTION_H
#define PANDA_MOTION_H

// 攀搭：运动数据（StickS3 内置 BMI270，SYS_I2C@0x68）
//
// 设备当场算好每次尝试的指标，按键时随记录上传（不传原始数据），见 02_功能清单「运动数据」：
//   fall_m    下落高度：失重持续时间 t → h = ½·g·t²                         ⭐⭐⭐
//   impact_g  落地冲击：失重结束后 300ms 内的最大加速度（g）                 ⭐⭐⭐
//   rest_s    休息时长：上一次尝试结束 → 这次开始持续活动                   ⭐⭐⭐
//   climb_s   攀爬时长：持续活动开始 → 掉下来（或最后一次活动）             ⭐⭐ 参考值
//   swings    身体晃动：旋转速度超过 250°/s 的次数（可能脱脚甩出）          ⭐⭐
//   smooth    动作流畅度：加速度变化越小越平稳，0–100                       ⭐
//
// 安全提醒（固定规则，断网也能用）：
//   落地冲击 ≥ kHardImpactG → 回调 "hard_fall"(冲击g, 下落米)
//   重摔后 90 秒内连续 kStillSeconds 秒几乎不动 → 回调 "still"(秒数, 0)
//   （中途连续活动 8 秒说明人能自己动了，不再盯）
//
// 阈值都是初始值，岩馆实测后再调。

#include <driver/i2c_master.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <functional>
#include <mutex>
#include <string>

class PandaMotion {
public:
    using Callback = std::function<void(const char* event, float a, float b)>;

    PandaMotion(i2c_master_bus_handle_t bus, uint8_t addr = 0x68);
    ~PandaMotion();

    // 初始化传感器并开始采样；没有 IMU / 初始化失败返回 false（其他功能照常，只是没有运动数据）
    bool Start();
    bool ready() const { return ready_; }
    void SetCallback(Callback cb);

    // 按键记录一次尝试时调用：返回这次尝试的运动字段（JSON 片段，不含花括号，如
    // "\"fall_m\":2.80,\"impact_g\":5.1"），没测到返回空串；同时开始统计下一次
    std::string TakeAttempt();

    // 测试用：最近一次掉落
    float last_fall_m() const { return last_fall_.h; }

    static constexpr float kFreeFallG = 0.35f;     // 低于这个算失重
    static constexpr float kFreeFallEndG = 0.6f;   // 回到这个以上算失重结束
    static constexpr int kMinFreeFallMs = 150;     // 至少失重这么久才算掉落（约 0.11 米）
    static constexpr int kImpactWindowMs = 300;    // 失重结束后找冲击峰值的时间
    static constexpr float kHardImpactG = 8.0f;    // 重摔
    static constexpr int kStillSeconds = 30;       // 重摔后几乎不动多久报警
    static constexpr int kStillWatchSeconds = 90;  // 重摔后盯多久
    static constexpr int kMovedSeconds = 8;        // 重摔后连续活动这么久 = 能自己动，不再盯
    static constexpr float kSwingDps = 250.0f;     // 大幅晃动
    static constexpr int kSegmentGapMs = 4000;     // 活动中间停顿不超过这个算同一段

private:
    struct Climb {
        bool valid = false;
        int64_t start_ms = 0;
        int64_t end_ms = 0;
        float jerk = 0;  // 平均每个采样的加速度变化（g）
        int swings = 0;
    };
    struct Fall {
        bool valid = false;
        int64_t time_ms = 0;  // 开始失重的时刻
        float h = 0;
        float impact = 0;
        Climb climb;
    };

    i2c_master_bus_handle_t bus_;
    uint8_t addr_;
    i2c_master_dev_handle_t dev_ = nullptr;
    TaskHandle_t task_ = nullptr;
    bool ready_ = false;
    Callback callback_;
    std::mutex mutex_;

    // 掉落状态机
    enum { kIdle, kFreeFall, kImpact } fall_state_ = kIdle;
    int64_t ff_start_ms_ = 0;
    int64_t ff_dur_ms_ = 0;
    int64_t impact_end_ms_ = 0;
    float impact_peak_ = 0;
    Climb ff_climb_;
    Fall last_fall_;

    // 1 秒窗口统计
    int win_n_ = 0;
    float win_sum_a_ = 0, win_sum_a2_ = 0, win_sum_w_ = 0, win_jerk_ = 0;
    int64_t win_start_ms_ = 0;
    float prev_a_ = 1.0f;

    // 当前活动段（攀爬候选）
    int64_t seg_start_ms_ = 0;  // 0 = 没有
    int64_t last_active_ms_ = 0;
    float seg_jerk_sum_ = 0;
    int seg_jerk_n_ = 0;
    int seg_swings_ = 0;
    int64_t last_swing_ms_ = 0;

    // 尝试之间
    int64_t prev_end_ms_ = 0;   // 上一次尝试结束时刻（0 = 开机后还没有）
    int64_t last_take_ms_ = 0;

    // 重摔后盯着看动不动
    bool watching_ = false;
    int64_t watch_until_ms_ = 0;
    int still_secs_ = 0;
    int moving_secs_ = 0;

    bool WriteReg(uint8_t reg, uint8_t value);
    bool WriteBurst(uint8_t reg, const uint8_t* data, size_t len);
    bool ReadRegs(uint8_t reg, uint8_t* data, size_t len);
    bool InitSensor();
    void Task();
    void Process(float a, float w, int64_t now_ms);
    void CloseWindow(int64_t now_ms);
    Climb CurrentClimb(int64_t end_ms) const;
    void Emit(const char* event, float a, float b);
};

#endif
