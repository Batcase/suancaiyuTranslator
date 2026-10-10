# 酸菜鱼翻译器 · 安卓版

看外语直播时的实时中文字幕，以**小窗悬浮在直播软件前面**，后台常驻。

这是 Windows 端的移植版，[ANDROID.md](../ANDROID.md) 里记录了完整的实施方案与踩坑清单。

---

## 已实现

| 模块 | 状态 | 说明 |
|---|---|---|
| `audio/AudioCaptureEngine` | ✅ | AudioPlaybackCapture 内录（不采麦克风）+ 麦克风兜底模式 |
| `asr/ParaformerClient` | ✅ | 阿里云百炼，原生 WebSocket 流式（国内直连） |
| `asr/DeepgramClient` | ✅ | Deepgram nova-2 流式（备用） |
| `asr/SenseVoiceClient` | ✅ | 硅基流动整句识别（国内，自研静音断句） |
| `mt/Translator` | ✅ | 6 家 OpenAI 兼容接口 + **自动降级** + 语言提示预热 |
| `pipeline/TranslationPipeline` | ✅ | 中间结果节流翻译、单飞合并、sid 校验、上下文滑窗 |
| `ui/OverlayController` | ✅ | TYPE_APPLICATION_OVERLAY 悬浮窗 + 鼠标穿透 + 可拖动 |
| `ui/SubtitleOverlayView` | ✅ | 自绘**描边文字**（亮暗底都清晰）+ 状态栏 |
| `service/TranslatorService` | ✅ | 前台服务 + mediaProjection 类型 + 通知栏控制 |
| `ui/MainActivity` | ✅ | 内嵌字幕预览 + 配置面板 + 权限引导 + 启动/停止 |

## 已验证

在 **MuMu 模拟器（Android 12 / API 32 / x86_64）** 上真机跑过：

```
./gradlew :app:assembleDebug     → BUILD SUCCESSFUL
产物: app/build/outputs/apk/debug/app-debug.apk  (13.0 MB)
```

| 验证项 | 结果 |
|---|---|
| 安装与启动 | ✅ `Displayed MainActivity: +762ms`，界面完整渲染 |
| **内录能采到音频内容** | ✅ 见下方自测日志 |
| 系统投射授权流程 | ✅ 弹出 `MediaProjectionPermissionActivity`，授权后内录打开 |
| 悬浮窗 | ✅ 挂在桌面之上，状态栏实时更新（截图见 `shots/android-overlay.png`） |
| 识别降级 | ✅ 首选 401 失败 → 沿降级链尝试 → 全部失败后如实报错 |
| 翻译降级 | ✅ 多家引擎依次尝试预热 |
| 采集稳定性 | ✅ 持续采集 `dropped=0` |
| 崩溃 | ✅ 无 `FATAL EXCEPTION` |

### 内录验证的原始日志

内录失败最难查的表现是"界面在跑但没字幕"。所以内置了一个**音频自测**：
播一段已知响度的正弦波，看采集侧的响度读数是否跟着变。

```powershell
# 先正常启动翻译（拿到投射授权），然后：
adb shell am broadcast -a com.suancaiyu.translator.SELFTEST `
  -n com.suancaiyu.translator.debug/com.suancaiyu.translator.service.SelfTestReceiver
```

实测输出：

```
音频自测开始   播放前 peak=0.0000
TonePlayer     开始播放测试音：440.0Hz 4.0s 振幅=0.7
采集统计        peak=0.4990  last=0.4936  dropped=0
音频自测结果   → ✅ 内录能采到播放中的音频
```

响度从 `0.0000` 跳到 `0.4990`，与播放振幅 0.7 相符（正弦波 RMS ≈ 振幅/√2 ≈ 0.495）。
**证明 AudioPlaybackCapture 真的拿到了播放中的音频数据**，而不只是"打开成功"。

### 真机测试中发现并修复的两个 Bug

这两个都是编译期完全看不出来的，只有真跑才会暴露：

1. **后台线程更新 View 导致崩溃**
   `ViewRootImpl$CalledFromWrongThreadException` —— 服务里在后台协程直接调用
   `SubtitleOverlayView.render()`，而 Android 的 View 只能在创建它的线程上更新。
   修复：所有渲染 `Handler.post` 到主线程。

2. **识别引擎失败后不会降级**
   Paraformer 返回 401 后只记了错误就停住，没有切到下一家（翻译层有降级，识别层漏了）。
   修复：按降级链依次尝试；且只在"这家还没成功连上过"时才换，
   避免一次网络抖动就把好用的引擎换掉。

## 尚未验证（需要真机 + 真实直播环境）

模拟器验证了通路，但下面这些模拟器测不准：

- [ ] 真实直播 App 的声音能否采到（被采集的 App 可以声明拒绝，版权保护的影音一定会拒绝）
- [ ] 悬浮窗在具体 ROM 上能否盖住全屏直播画面
- [ ] 长时间后台运行会不会被国产 ROM 杀进程
- [ ] 真实网络下的端到端延迟（Windows 端实测 687ms，安卓待测）

## 怎么构建

### 用 Android Studio（推荐）

1. Android Studio → **Open** → 选择本目录（`android/`）
2. 首次打开会自动同步 Gradle 与下载 SDK 组件
3. 连上手机（开启 USB 调试），点 ▶ 运行

### 用命令行

```powershell
# 需要 JDK 17 或 21（AGP 8.5 不支持 JDK 25）
$env:JAVA_HOME = "C:\Program Files\Android\Android Studio\jbr"   # 或你自己的 JDK 21
$env:ANDROID_HOME = "$env:LOCALAPPDATA\Android\Sdk"

.\gradlew.bat :app:assembleDebug
# 产物：app\build\outputs\apk\debug\app-debug.apk
```

安装到手机：

```powershell
adb install -r app\build\outputs\apk\debug\app-debug.apk
```

> `local.properties` 里记录了本机 SDK 路径，**不要提交**（已在 .gitignore）。
> 换机器时删掉它，Android Studio 会重新生成。

## 首次使用

1. 打开 App → 填入 Key（至少一家翻译 + 一家识别）
2. 系统会要几个权限，逐个给：
   - **通知**（Android 13+）——前台服务需要可见通知，否则会被系统限制
   - **显示在其他应用上层** —— 悬浮窗必需。这个**只能手动在系统设置里开**，
     App 会跳转过去；回来后会显示绿色提示说明已就绪
   - **录制/投射** —— 点「启动翻译」时系统会弹，选「立即开始」
3. 点「启动翻译」→ 字幕挂到屏幕下方 → 打开直播 App 看效果

## 已知限制（照搬 Windows 端的经验）

- **有些 App 录不到声音**：对方声明了 `setAllowedCapturePolicy(ALLOW_CAPTURE_BY_NONE)`
  （带 DRM 的付费内容一定会），这是系统级限制，任何软件都绕不过。
  此时可改用「麦克风模式」（对着喇叭录，音质差但能用）。
- **国产 ROM 会杀后台**：需要手动加白名单，见 [ANDROID.md](../ANDROID.md) 第 6 节。
- **悬浮窗与独占全屏游戏冲突**：游戏用独占全屏时盖不上去，改成无边框窗口即可。
- **Android 10 以下不支持**：`AudioPlaybackCapture` 从 API 29 才有，这是硬门槛。

## 工程结构

```
android/
  app/src/main/java/com/suancaiyu/translator/
    App.kt                    应用入口（建通知渠道）
    Providers.kt              ★ 厂商注册表：加新厂商只改这里
    Settings.kt               本地设置与密钥存储
    data/Subtitle.kt          字幕状态（StateFlow，跨线程）
    audio/AudioCaptureEngine  内录 / 麦克风采集
    asr/                      AsrClient 接口 + 3 个引擎实现
    mt/Translator.kt          6 家翻译 + 自动降级
    pipeline/TranslationPipeline  编排：音频→识别→翻译→字幕
    ui/MainActivity           主界面
    ui/OverlayController      悬浮窗管理
    ui/SubtitleOverlayView    自绘描边字幕
    service/TranslatorService 前台服务
```
