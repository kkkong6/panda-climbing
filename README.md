# 攀搭 · 啪嗒一下，搭子就位。

**磁吸在粉袋上的 AI 攀岩搭子：记录每次尝试，理解语气，回应努力，也提醒你留意身体。**

![攀搭产品展示](产品展示.png)

## 看项目

- [产品方案](06_参赛产品方案.md)
- [产品方案说明（PDF）](攀搭产品方案说明.pdf)
- [产品体验 Demo 图集（PDF）](攀搭_产品体验Demo_图集.pdf)
- [黑客松宣传海报](攀搭_黑客松宣传海报.jpg)
- [App 交互页面源码](app/攀搭App.html)
- [GitHub 上传与 Pages 开通步骤](docs/GITHUB发布步骤.md)

Pages 开通后，首页提供报告阅读、PDF 下载和 App 演示入口。静态 App 当前使用演示数据；真实 AI、设备连接和数据保存由 Python 服务端承担。

## 为什么做

抱石的进步藏在反复尝试中。手上有镁粉、刚落地还在喘气时，掏手机记结果容易打断节奏；只记完攀结果，又会漏掉有效爬法和过程变化。

攀搭通过按键与语音降低记录成本，用 Step 同时理解内容和声音情绪，再将训练档案连接到 App 的线路管理、复盘与后续建议。

## 项目结构

| 目录 | 用途 |
|---|---|
| `firmware/` | ESP32-S3 固件源码，包含攀搭按键、纠错、事件协议与屏幕交互 |
| `server/` | Python 服务端源码，包含 Step 模型适配、训练存储和线路工具 |
| `server-config.example.yaml` | 无真实密钥的配置模板 |
| `tests/` | 本地存储验证入口及三组回归用例 |
| `app/攀搭App.html` | 手机网页交互原型 |
| `docs/` | 安装、代码索引、协议、公开发布步骤 |
| `scripts/` | 环境配置与静态网站构建工具 |
| `分享图/` | 当前正式方案实际引用的自包含图版 |
| `SOURCE_SNAPSHOT.json` | 此次源码整理的来源和版本记录 |

## 当前代码覆盖

本次根据实际源码核对：

- Step ASR 已切换为 `stepaudio-3-asr-max` 并加入攀岩热词；声音情绪使用 `stepaudio-3-chat-preview`。
- `step-3.7-flash` 负责对话和工具调用，加入推理强度配置、卡顿重试与空回答兜底；`stepaudio-3-tts` 负责情感语音。
- SQLite 保存训练、线路、换线与尝试；线路登记支持补充信息、纠正、回到上一条及换线处理。
- 按键事件包含唯一 ID，支持重发去重、未知线路归属，以及 `fix` 改按更新。
- 固件包含按键长短按、离线待确认队列、3 秒改按窗口、线路推送及屏幕状态显示。

完整产品方案还包含 IMU 指标、身体状态、安全提醒、声音定制和 App 真实数据闭环等目标设计；仓库不把设计说明等同于全部能力已完成现场验证。

## 开始运行

### 服务端

建议 Python 3.10，并安装系统音频依赖 `ffmpeg`、`libopus`。在项目根目录：

```bash
python -m pip install -r server/requirements.txt
python scripts/configure_server.py --host 你的电脑局域网IP
python server/app.py
```

配置脚本会隐式输入 Step API Key，写入被 Git 忽略的 `server/data/.config.yaml`。完整步骤见 [运行说明](docs/运行说明.md)。

### 固件

准备 ESP-IDF 6.0.1 或更新版本（现有开发环境为 6.0.2），在 `firmware/` 运行：

```bash
python scripts/build.py m5stack/stick-s3 --name m5stack-stick-s3
```

编译和烧录按 [固件上游指南](firmware/README.md) 操作。原机完整 Flash 备份属于本地恢复资料，不随仓库分发。

### 本地验证

```bash
python tests/run_store_checks.py
python scripts/build_site.py
```

存储验证使用临时数据库，不调用 Step，不连接设备。`tests/legacy/` 只保留这三个存储回归用例，由统一入口调整路径后运行。

首版上传范围见 [上传清单](docs/首版上传清单.md)。历史文件和个人开发工具保留在本地，由 `.gitignore` 排除。

## 技术基础与来源

- 硬件：M5Stack StickS3 + Unit Vibrator。
- 固件基础：[xiaozhi-esp32](https://github.com/78/xiaozhi-esp32)，保留 `firmware/LICENSE`。
- 服务端基础：[xiaozhi-esp32-server](https://github.com/xinnan-tech/xiaozhi-esp32-server)，保留 `server/LICENSE`。
- 攀搭设计：粉袋场景交互、训练事件与存储、情绪语音编排、反馈策略、App 及产品图文方案。

照片与硬件图片按图注署名及原许可使用，图像素材不因同仓存放而自动变更许可。上游源码按各目录许可证使用；项目原创材料的额外授权由作者决定。
