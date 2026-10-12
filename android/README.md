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
| **真实端到端（真实 Key）** | ✅ 21 秒日语样本 → 5 段完整中文字幕，见下方 |
| **内录能采到音频内容** | ✅ 见下方自测日志 |
| 系统投射授权流程 | ✅ 弹出 `MediaProjectionPermissionActivity`，授权后内录打开 |
| 悬浮窗 | ✅ `自检：visible=true size=1600x55 isShown=true` |
| **App 内状态实时更新** | ✅ 状态栏显示 `识别中 · 阿里云 Paraformer · 1187ms` |
| 识别降级 | ✅ 首选 401 失败 → 沿降级链尝试 → 全部失败后如实报错 |
| 翻译降级 | ✅ 多家引擎依次尝试预热 |
| 采集稳定性 | ✅ 持续采集 `dropped=0` |
| 崩溃 | ✅ 无 `FATAL EXCEPTION` |

### 真实端到端结果（21 秒日语样本，阿里云 Paraformer + DeepSeek）

```
翻译 546ms -> 好，大家好。
翻译 660ms -> 今天想给大家介绍一款新游戏。
翻译 440ms -> 这款游戏是开放世界的，画面真的特别牛。
翻译 895ms -> 总之，先玩个前30分钟试试。
翻译 404ms -> 你们是从哪儿开始看的？在聊天里告诉我一下。

翻译请求总数: 5
```

**每一句都是完整句、且只翻一次。** 这是"等断句整句翻"的效果（见下方"改动记录"）。
改之前是逐片段翻的：

```
嗯。 / 好，大家好 / 今天 / 今天，给大家 / 今天要给大家介绍一款新游戏
/ 今天想给大家介绍一款新游戏。 / 这个？ / 这个游戏是开放世...
```

同一句话被翻了好几遍，而且每次只看到半句。

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

### 真机测试中发现并修复的问题

这些都是编译期完全看不出来的，只有真跑才会暴露：

1. **后台线程更新 View 导致崩溃**
   `ViewRootImpl$CalledFromWrongThreadException` —— 服务里在后台协程直接调用
   `SubtitleOverlayView.render()`，而 Android 的 View 只能在创建它的线程上更新。
   修复：所有渲染 `Handler.post` 到主线程。

2. **识别引擎失败后不会降级**
   Paraformer 返回 401 后只记了错误就停住，没有切到下一家（翻译层有降级，识别层漏了）。
   修复：按降级链依次尝试；且只在"这家还没成功连上过"时才换，
   避免一次网络抖动就把好用的引擎换掉。

3. **"给了权限但悬浮窗不显示"**
   旧实现在 `show()` 最前面 `if (!canDraw) return false`，把
   `Settings.canDrawOverlays()` 当**闸门**。这个判断在某些 ROM / 分身 / 多用户下
   即使授权了也会返回 false，于是窗口**永远不会被创建**。
   另外 `MainActivity` 里也有一道同样的闸门，导致**整个翻译都启动不了**。

   修复：
   * 权限判断**不再当闸门**。改成"先尝试 `addView`，失败再判定"——
     没权限时系统会抛 `SecurityException`，这是最权威的判据（不会和 ROM 的
     状态缓存不一致）。`canDrawOverlays()` 只用来给用户提示。
   * 去掉 `MainActivity` 里的启动闸门：没检测到权限只提示一次，**照样启动**。
   * 去掉 `FLAG_LAYOUT_NO_LIMITS` —— 实测在有底部导航栏/手势条的设备上，
     它会让 `Gravity.BOTTOM + y` 的窗口被推到屏幕外，看起来就是"没显示"。
   * 添加后**自检一次**并把结果写进 `lastError`：
     `悬浮窗自检：visible=true size=1600x55 isShown=true`
     `addView` 不抛异常不代表真的看得见，必须回查。个别 ROM 会接受窗口但把它放到屏幕外。

4. **"App 内未生效"（界面一直显示未启动）**
   `TranslatorService.store` 只在服务的 `onCreate` 里创建，而 Activity 订阅时
   它还是 `null`：

   ```kotlin
   val store = TranslatorService.store          // 服务没起来时是 null
   if (store != null && !collecting) { ...订阅... }   // 一次不成立就再也不订阅
   ```

   于是 Activity **永远订阅不上**，服务启动后界面照样显示"未启动"。

   修复：`store` 改成**进程级单例**（`by lazy`，首次访问就创建），
   Activity 与 Service 拿到的一定是同一个实例；订阅改为无条件。

5. **音频采集只匹配 3 种 usage**（蓝牙耳机场景容易采不到）
   `AudioPlaybackCaptureConfiguration` 原来只加了
   `MEDIA / GAME / UNKNOWN`。蓝牙（尤其 LE Audio / A2DP）的音频在部分 ROM 上
   会被标成别的 usage，只匹配这三项就一条都采不到。

   修复：扩到 10 项"播放类" usage，并做**分级尝试**
   （全量 → 常用三项），个别 ROM 拒绝全量时自动回退。
   实测日志：`内录已打开（全量用途，16000Hz，usage 10 项）`。

6. **翻译没有"等断句"** —— 安卓端一直在逐片段翻译，快语速下碎片化。
   修复：移植 PC 端的"等断句整句翻"（服务端 `sentence_end` / `speech_final`
   为触发点，另有"攒够 60 字符""静默 1.2 秒""句末标点"三个兜底）。
   实测翻译请求 20+ → **5 次**，每句都是完整句。

7. **采不到声音时用户无从判断**
   连续 10 秒没采到声音时，除了写日志，现在也会把原因显示到界面
   （"没采到声音：可试麦克风模式"），并新增**麦克风模式**按钮作为兜底
   （被采集方拒绝录制、或换了输出设备时用；音质差但能用）。

## 尚未验证（需要真机 + 真实直播环境）

模拟器验证了通路，但下面这些模拟器测不准：

- [ ] 真实直播 App 的声音能否采到（被采集的 App 可以声明拒绝，版权保护的影音一定会拒绝）
- [ ] **蓝牙耳机场景**：回环能否采到（已按最宽松配置 + 麦克风模式兜底，但没在真蓝牙上验证过）
- [ ] 悬浮窗在具体 ROM 上能否盖住全屏直播画面
- [ ] 长时间后台运行会不会被国产 ROM 杀进程
- [ ] 真实网络下的端到端延迟（Windows 端实测 687ms；安卓模拟器实测 1187ms）

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
