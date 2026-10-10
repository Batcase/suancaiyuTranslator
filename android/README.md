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

```
./gradlew :app:assembleDebug     → BUILD SUCCESSFUL
产物: app/build/outputs/apk/debug/app-debug.apk  (13.0 MB)
```

构建产物的核对结果：

- 包名 `com.suancaiyu.translator`，应用名 **酸菜鱼翻译器**，minSdk 29 / targetSdk 34
- 权限齐全：`RECORD_AUDIO`、`SYSTEM_ALERT_WINDOW`、`FOREGROUND_SERVICE`、
  `FOREGROUND_SERVICE_MEDIA_PROJECTION`、`POST_NOTIFICATIONS`
- 84 个类已编译进 9 个 dex（translator/asr/audio/data/mt/pipeline/service/ui）

## 尚未验证（需要真机）

编译通过不等于能跑。下面这些**必须在真机上试**，模拟器也测不准：

- [ ] AudioPlaybackCapture 能否真的采到直播 App 的声音
      （被采集的 App 可以声明拒绝，版权保护的影音一定会拒绝）
- [ ] 悬浮窗在具体 ROM 上能否盖住直播画面（部分 ROM 需要"后台弹出界面"权限）
- [ ] 前台服务在锁屏/切后台后能否持续运行（国产 ROM 会杀后台）
- [ ] 端到端延迟实测

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
