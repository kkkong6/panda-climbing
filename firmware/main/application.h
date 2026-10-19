#ifndef _APPLICATION_H_
#define _APPLICATION_H_

#include <freertos/FreeRTOS.h>
#include <freertos/event_groups.h>
#include <freertos/task.h>
#include <esp_timer.h>

#include <string>
#include <mutex>
#include <deque>
#include <memory>
#include <functional>
#include <cstdint>
#include <vector>

#include "protocol.h"
#include "ota.h"
#include "audio_service.h"
#include "device_state.h"
#include "device_state_machine.h"
#include "notify/notify_player.h"

// Main event bits
#define MAIN_EVENT_SCHEDULE             (1 << 0)
#define MAIN_EVENT_SEND_AUDIO           (1 << 1)
#define MAIN_EVENT_WAKE_WORD_DETECTED   (1 << 2)
#define MAIN_EVENT_VAD_CHANGE           (1 << 3)
#define MAIN_EVENT_ERROR                (1 << 4)
#define MAIN_EVENT_ACTIVATION_DONE      (1 << 5)
#define MAIN_EVENT_CLOCK_TICK           (1 << 6)
#define MAIN_EVENT_NETWORK_CONNECTED    (1 << 7)
#define MAIN_EVENT_NETWORK_DISCONNECTED (1 << 8)
#define MAIN_EVENT_TOGGLE_CHAT          (1 << 9)
#define MAIN_EVENT_START_LISTENING      (1 << 10)
#define MAIN_EVENT_STOP_LISTENING       (1 << 11)
#define MAIN_EVENT_STATE_CHANGED        (1 << 12)
#define MAIN_EVENT_PLAYBACK_DRAINED     (1 << 13)


enum AecMode {
    kAecOff,
    kAecOnDeviceSide,
    kAecOnServerSide,
};

class Application {
public:
    static Application& GetInstance() {
        static Application instance;
        return instance;
    }
    // Delete copy constructor and assignment operator
    Application(const Application&) = delete;
    Application& operator=(const Application&) = delete;

    /**
     * Initialize the application
     * This sets up display, audio, network callbacks, etc.
     * Network connection starts asynchronously.
     */
    void Initialize();

    /**
     * Run the main event loop
     * This function runs in the main task and never returns.
     * It handles all events including network, state changes, and user interactions.
     */
    void Run();

    DeviceState GetDeviceState() const { return state_machine_.GetState(); }
    bool IsVoiceDetected() const { return audio_service_.IsVoiceDetected(); }
    
    /**
     * Request state transition
     * Returns true if transition was successful
     */
    bool SetDeviceState(DeviceState state);

    /**
     * Schedule a callback to be executed in the main task
     */
    void Schedule(std::function<void()>&& callback);

    /**
     * Alert with status, message, emotion and optional sound
     */
    void Alert(const char* status, const char* message, const char* emotion = "", const std::string_view& sound = "");
    void DismissAlert();

    void AbortSpeaking(AbortReason reason);

    /**
     * Toggle chat state (event-based, thread-safe)
     * Sends MAIN_EVENT_TOGGLE_CHAT to be handled in Run()
     */
    void ToggleChatState();

    /**
     * Start listening (event-based, thread-safe)
     * Sends MAIN_EVENT_START_LISTENING to be handled in Run()
     */
    void StartListening();

    /**
     * Stop listening (event-based, thread-safe)
     * Sends MAIN_EVENT_STOP_LISTENING to be handled in Run()
     */
    void StopListening();

    void Reboot();
    void WakeWordInvoke(const std::string& wake_word);
    bool UpgradeFirmware(const std::string& url, const std::string& version = "");
    bool CanEnterSleepMode();
    void SendMcpMessage(const std::string& payload);
    void RegisterMcpBroadcastCallback(std::function<void(const std::string&)> callback);
    void SetAecMode(AecMode mode);
    AecMode GetAecMode() const { return aec_mode_; }
    void PlaySound(const std::string_view& sound);
    AudioService& GetAudioService() { return audio_service_; }
    
    /**
     * Reset protocol resources (thread-safe)
     * Can be called from any task to release resources allocated after network connected
     * This includes closing audio channel, resetting protocol and ota objects
     */
    void ResetProtocol();

    /**
     * 攀搭：记录一次尝试（按键），可在任意任务调用
     * success=true 完攀，false 失败。先存在设备里，连上服务端后发送，收到确认才删除
     */
    void PandaRecordAttempt(bool success);

private:
    Application();
    ~Application();

    std::mutex mutex_;
    std::deque<std::function<void()>> main_tasks_;
    std::unique_ptr<Protocol> protocol_;
    EventGroupHandle_t event_group_ = nullptr;
    esp_timer_handle_t clock_timer_handle_ = nullptr;
    DeviceStateMachine state_machine_;
    ListeningMode listening_mode_ = kListeningModeAutoStop;
    AecMode aec_mode_ = kAecOff;
    std::string last_error_message_;
    AudioService audio_service_;
    NotifyPlayer notify_player_;
    uint32_t notification_playback_id_ = 0;
    std::unique_ptr<Ota> ota_;

    std::function<void(const std::string&)> mcp_broadcast_callback_;

    bool has_server_time_ = false;
    bool aborted_ = false;
    bool assets_version_checked_ = false;
    bool play_popup_on_listening_ = false;  // Flag to play popup sound after state changes to listening
    bool pending_listening_start_ = false;  // Waiting for playback to drain before starting listening (auto mode)
    int clock_ticks_ = 0;
    TaskHandle_t activation_task_handle_ = nullptr;

    // 攀搭：待确认的按键记录（id, JSON 字段）
    struct PandaEvent { std::string id; std::string fields; bool sent = false; };
    std::deque<PandaEvent> panda_pending_;
    uint32_t panda_boot_id_ = 0;
    uint32_t panda_seq_ = 0;
    bool panda_stop_after_connect_ = false;  // 按住说话时还没连上就松手了
    // 攀搭：3 秒改按窗口。窗口内再按任一键 = 改成这次的结果（同一个 id，带 fix），倒计时重来
    esp_timer_handle_t panda_fix_timer_ = nullptr;
    std::string panda_fix_id_;          // 窗口里那次按键的 id，空 = 没有窗口
    bool panda_fix_success_ = false;    // 窗口里当前的结果
    int panda_countdown_ = 0;           // 剩余秒数 3、2、1，0 = 窗口已关
    std::string panda_status_text_;     // 倒计时前面那段文字（先是"记录中"，收到确认后换成服务端的文字）
    std::string panda_route_;           // 当前线路「黄色V3」，给屏幕色块用
    void PandaFlush();
    void PandaHandleAck(const cJSON* root);
    void PandaCountdownTick();
    void PandaShowStatus();

    // 攀搭：安全提醒（固定规则，断网也能用）
    //   重摔 → 黄屏「还好吗？」+ 长震 2 下，告诉服务端（小搭关心一句）
    //   重摔后 30 秒几乎不动 → 红屏闪烁 + 持续震动，10 秒内没按键就放报警音（音量开到最大）
    //   任意键取消（这次按键不算记录）
    enum PandaAlert { kPandaAlertNone, kPandaAlertHard, kPandaAlertSos };
    PandaAlert panda_alert_ = kPandaAlertNone;
    esp_timer_handle_t panda_alert_timer_ = nullptr;  // 报警期间每 0.5 秒一次
    int panda_alert_ticks_ = 0;
    int panda_saved_volume_ = -1;
    void PandaOnMotionEvent(const std::string& event, float a, float b);
    bool PandaCancelAlert();
    void PandaAlertTick();
    void PandaStartAlertTimer();
    // 攀搭：发一条不需要确认的事件（重摔、报警、闲置），没连上就先连
    void PandaSendNow(const std::string& fields);
    // 攀搭：训练中 30 分钟没按键 → 问问是不是练完了
    esp_timer_handle_t panda_idle_timer_ = nullptr;
    bool panda_has_attempts_ = false;
    int64_t panda_status_until_ = 0;  // 限时状态提示到什么时候（秒，0 = 没有）
    bool panda_battery_low_ = false;  // 电量低提示是不是显示着
    void PandaResetIdleTimer();
    void PandaOnIdle();


    // Event handlers
    void HandleStateChangedEvent();
    void HandleToggleChatEvent();
    void HandleStartListeningEvent();
    void HandleStopListeningEvent();
    void HandleNetworkConnectedEvent();
    void HandleNetworkDisconnectedEvent();
    void HandleActivationDoneEvent();
    void HandleWakeWordDetectedEvent();
    void ContinueOpenAudioChannel(ListeningMode mode);
    void BeginWakeWordInvoke(const std::string& wake_word);
    void ContinueWakeWordInvoke(const std::string& wake_word);
    void StartListeningAudio();
    void ConfigureWakeWordForListening();
    void StartNotification(std::string audio_url, std::vector<NotifySubtitle> subtitles);
    void StopNotification();
    void HandleNotificationFinished(uint32_t playback_id, bool success);

    // Activation task (runs in background)
    void ActivationTask();

    // Helper methods
    void CheckAssetsVersion();
    void CheckNewVersion();
    void InitializeProtocol();
    void ShowActivationCode(const std::string& code, const std::string& message);
    void SetListeningMode(ListeningMode mode);
    ListeningMode GetDefaultListeningMode() const;
    
    // State change handler called by state machine
    void OnStateChanged(DeviceState old_state, DeviceState new_state);
};


class TaskPriorityReset {
public:
    TaskPriorityReset(BaseType_t priority) {
        original_priority_ = uxTaskPriorityGet(NULL);
        vTaskPrioritySet(NULL, priority);
    }
    ~TaskPriorityReset() {
        vTaskPrioritySet(NULL, original_priority_);
    }

private:
    BaseType_t original_priority_;
};

#endif // _APPLICATION_H_
