#ifndef PANDA_VIBRATOR_H
#define PANDA_VIBRATOR_H

// 攀搭：M5Stack Unit Vibrator（U059）驱动
// 接线：Grove 口，黄线 MOTOR → G9；5V 由 PM1 升压（BOOST）提供
// 电流限制：马达满力度约 0.42A，StickS3 Grove 口最多 0.38A → PWM 占空比默认 50%
//
// 震法（和 02_功能清单 一致）：
//   short1  短震 1 下  收到了（记录失败、改按、开机）
//   short2  短震 2 下  成功了（记录完攀）
//   long1   长震 1 下  我要说话，请注意听
//   triple  连震 3 下  太棒了
//   long2   长震 2 下  注意安全
//   sos     持续震动    紧急（StopVibration 结束）

#include <driver/gpio.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <freertos/task.h>
#include <functional>
#include <string>

class PandaVibrator {
public:
    // power: 打开 / 关闭 Grove 5V（板子提供），空闲 2 秒后自动关掉省电
    PandaVibrator(gpio_num_t pin, std::function<void(bool)> power, int duty_percent = 50);
    ~PandaVibrator();

    // 按名字震动，未知名字返回 false；新的震动会打断正在进行的
    bool Play(const std::string& pattern);
    void Stop();
    void SetStrength(int duty_percent);  // 10–60

private:
    struct Step { uint16_t on_ms; uint16_t off_ms; };
    struct Request { const Step* steps; uint8_t count; bool repeat; };

    gpio_num_t pin_;
    std::function<void(bool)> power_;
    int duty_percent_;
    QueueHandle_t queue_ = nullptr;
    TaskHandle_t task_ = nullptr;
    bool powered_ = false;

    void Task();
    void Motor(bool on);
    bool WaitOrInterrupt(int ms, Request& next, bool& got_next);
};

#endif
