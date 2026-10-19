#include <cstdio>
#include <cmath>
#include <random>
#include <string>
#define private public
#include "panda_motion.h"
#undef private
extern "C" const uint8_t bmi270_config_file[8192] = {0};
int64_t g_now_us = 0;
static std::mt19937 rng(1);
static std::normal_distribution<float> N(0, 1);
static int fails = 0;
#define CHECK(c, ...) do { printf((c) ? "   ✅ " : "   ❌ "); printf(__VA_ARGS__); printf("\n"); fails += !(c); } while (0)

PandaMotion m(nullptr);
std::string events;
int64_t t_ms = 0;
void step(float a, float w) { t_ms += 10; g_now_us = t_ms * 1000; m.Process(a, w, t_ms); }
void rest(float sec) { for (int i = 0; i < sec * 100; i++) step(1.0f + 0.008f * N(rng), 2 + fabsf(N(rng))); }
void still(float sec) { for (int i = 0; i < sec * 100; i++) step(1.0f + 0.003f * N(rng), 1 + 0.5f * fabsf(N(rng))); }
void walk(float sec) { for (int i = 0; i < sec * 100; i++) step(1.0f + 0.25f * sinf(t_ms / 80.0f) + 0.05f * N(rng), 30 + 10 * fabsf(N(rng))); }
void climb(float sec, int swings, float rough) {
    int n = sec * 100;
    for (int i = 0; i < n; i++) {
        float w = 40 + 15 * fabsf(N(rng));
        if (swings && i > 0 && i % (n / (swings + 1)) == 0 && i / (n / (swings + 1)) <= swings) w = 320;
        step(1.0f + rough * N(rng) + 0.1f * sinf(t_ms / 300.0f), w);
    }
}
void fall(float sec, float peak) {
    for (int i = 0; i < sec * 100; i++) step(0.08f + 0.03f * fabsf(N(rng)), 60);
    step(peak * 0.6f, 200); step(peak, 200); step(peak * 0.5f, 150); step(2.0f, 80);
    for (int i = 0; i < 40; i++) step(1.0f + 0.05f * N(rng), 20);
}
double field(const std::string& s, const char* k) {
    auto p = s.find(std::string("\"") + k + "\":");
    return p == std::string::npos ? NAN : atof(s.c_str() + p + strlen(k) + 3);
}

int main() {
    m.ready_ = true;
    m.SetCallback([](const char* e, float a, float b) { char buf[64]; snprintf(buf, sizeof buf, "%s(%.1f,%.2f) ", e, a, b); events += buf; });

    printf("① 第一次：静止 20s → 爬 30s（2 次大晃动）→ 掉 0.7s（理论 2.40 米）冲击 5g → 按键\n");
    rest(20); climb(30, 2, 0.04f); fall(0.70f, 5.0f); rest(5);
    std::string s = m.TakeAttempt();
    printf("   %s\n", s.c_str());
    CHECK(fabs(field(s, "fall_m") - 2.40) < 0.15, "下落高度 %.2f ≈ 2.40", field(s, "fall_m"));
    CHECK(fabs(field(s, "impact_g") - 5.0) < 0.3, "冲击 %.1f ≈ 5", field(s, "impact_g"));
    CHECK(fabs(field(s, "climb_s") - 30) <= 2, "攀爬 %.0f 秒 ≈ 30", field(s, "climb_s"));
    CHECK(field(s, "swings") == 2, "晃动 %.0f 次", field(s, "swings"));
    CHECK(std::isnan(field(s, "rest_s")), "开机第一次没有休息时长");
    CHECK(events.empty(), "5g 不算重摔，没有安全事件");

    printf("② 第二次：歇 2 分钟（中间走动 10s）→ 爬 20s → 掉 0.5s（理论 1.23 米）冲击 10g → 30s 不动\n");
    rest(60); walk(10); rest(60); climb(20, 0, 0.12f); fall(0.5f, 10.0f);
    CHECK(events.find("hard_fall(10.0,1.2") == 0, "重摔回调：%s", events.c_str());
    still(29);
    CHECK(events.find("still") == std::string::npos, "29 秒还不报警");
    still(2);
    CHECK(events.find("still(30") != std::string::npos, "30 秒不动 → still 回调：%s", events.c_str());
    s = m.TakeAttempt();
    printf("   %s\n", s.c_str());
    CHECK(fabs(field(s, "fall_m") - 1.23) < 0.12, "下落高度 %.2f ≈ 1.23", field(s, "fall_m"));
    CHECK(fabs(field(s, "rest_s") - 135) <= 6, "休息 %.0f 秒 ≈ 135（上次掉下来 → 这次开始爬，含走动不连续的 2 分钟）", field(s, "rest_s"));
    CHECK(field(s, "smooth") < 60, "这次动作粗糙，流畅度 %.0f 偏低", field(s, "smooth"));

    printf("③ 重摔后起来走动：不报警\n");
    events.clear();
    rest(30); climb(15, 0, 0.04f); fall(0.4f, 9.0f); walk(12); still(40);
    CHECK(events.find("hard_fall") == 0 && events.find("still") == std::string::npos, "摔后起来走了 12 秒再坐下，不报警：%s", events.c_str());
    m.TakeAttempt();

    printf("③b 摔后翻滚 3 秒再不动：照样报警\n");
    events.clear();
    rest(30); climb(15, 0, 0.04f); fall(0.4f, 9.0f); walk(3); still(31);
    CHECK(events.find("still(30") != std::string::npos, "翻滚几秒不算能动：%s", events.c_str());
    m.TakeAttempt();

    printf("④ 完攀走下来（没有掉落）：只有攀爬时长\n");
    rest(90); climb(40, 1, 0.03f); rest(8);
    s = m.TakeAttempt();
    printf("   %s\n", s.c_str());
    CHECK(std::isnan(field(s, "fall_m")) && fabs(field(s, "climb_s") - 40) <= 2, "没有下落，攀爬 %.0f 秒", field(s, "climb_s"));
    CHECK(field(s, "smooth") > 55, "动作平稳，流畅度 %.0f（比①②高）", field(s, "smooth"));

    printf("⑤ 粉袋晃一下（失重 80ms）不算掉落；按键间隔太久不配对旧掉落\n");
    rest(10); for (int i = 0; i < 8; i++) step(0.2f, 50); rest(5);
    s = m.TakeAttempt();
    CHECK(s.find("fall_m") == std::string::npos, "没有误报掉落：{%s}", s.c_str());
    climb(10, 0, 0.04f); fall(0.6f, 4.0f); rest(150);
    s = m.TakeAttempt();
    CHECK(s.find("fall_m") == std::string::npos, "掉落 2.5 分钟后才按键，不算这次的：{%s}", s.c_str());

    printf("\n%s\n", fails ? "有失败 ❌" : "全部通过 ✅");
    return fails ? 1 : 0;
}
