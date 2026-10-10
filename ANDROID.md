# 安卓端移植方案（酸菜鱼翻译器 → Android）

目标：**后台常驻 + 小窗悬浮在直播软件前面 + 实时翻译手机播出来的声音**。

原型（Windows 版）已经把"内录 → 流式识别 → 流式翻译 → 悬浮字幕"整条链路和延迟模型验证过了。
本文给出安卓的对应实现：每个环节用什么 API、权限怎么申、哪些坑必须提前避、以及和原型的模块对应表。

---

## 0. 安卓端比 Windows 强的一点：能按应用内录

Windows 的 WASAPI 回环是**设备级**的——只能录"整个扬声器输出"，
没法只翻浏览器而不翻微信语音。

安卓 10（API 29）引入的 `AudioPlaybackCapture` 支持**按应用白名单采集**：

```kotlin
val projection = mediaProjectionManager.getMediaProjection(resultCode, data)

val config = AudioPlaybackCaptureConfiguration.Builder(projection)
    // 只采这些 App 的声音，其它 App 一律不采
    .addMatchingUsage(AudioAttributes.USAGE_MEDIA)
    .addMatchingUsage(AudioAttributes.USAGE_GAME)
    .addMatchingUsage(AudioAttributes.USAGE_UNKNOWN)
    // 也可以按包名精确指定：
    // .addMatchingUid(直播App的uid)
    .build()

val format = AudioFormat.Builder()
    .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
    .setSampleRate(16000)          // 云端 ASR 直接吃这个格式
    .setChannelMask(AudioFormat.CHANNEL_IN_MONO)
    .build()

val record = AudioRecord.Builder()
    .setAudioFormat(format)
    .setBufferSizeInBytes(bufferSize)
    .setAudioPlaybackCaptureConfig(config)
    .build()
record.startRecording()
```

**必须知道的两个限制（会直接导致"录不到声音"）：**

1. **被采集的 App 可以拒绝被录。** 如果它给自己的播放设置了
   `AudioAttributes.setAllowedCapturePolicy(ALLOW_CAPTURE_BY_NONE)`，你就一点声音都拿不到。
   直播类 App 极少这么做，但**带 DRM 的付费内容（Netflix 等）一定会**，这是系统级硬限制，
   任何方案都绕不过。遇到这种只能退化成"用麦克风对着喇叭录"。
2. **必须有 `MediaProjection` 授权**（会弹一个"是否允许录制/投射屏幕"的系统对话框）。
   虽然我们只采音频，但 API 设计上就要求走过屏幕捕获授权。
   - Android 14（API 34）起，每次投影会话都要重新授权，且 `MediaProjection` 必须
     在拿到后**立刻**在**前台服务**里使用，否则会抛 `SecurityException`。
   - 建议用 `MediaProjectionManager.createScreenCaptureIntent()`，
     并在 `onActivityResult` 里立刻 `startForegroundService` 把服务拉起来。

---

## 1. 整体架构

```
┌─────────────────────────────────────────────────────────────┐
│ MainActivity：引导授权（通知权限 / 悬浮窗 / 录音投射）、选语言、填 Key │
└───────────────────────────┬─────────────────────────────────┘
                            │ startForegroundService(intent + resultCode/data)
                            ▼
┌─────────────────────────────────────────────────────────────┐
│ 酸菜鱼翻译器Service : Service（foreground, type=mediaProjection） │
│  ├─ AudioCaptureEngine   AudioRecord(AudioPlaybackCapture)   │
│  │        │ 16kHz/mono/PCM16，20~40ms 一包                    │
│  │        ▼                                                  │
│  ├─ AsrClient            OkHttp WebSocket（Deepgram/OpenAI） │
│  │        │ 中间结果 + 定稿                                   │
│  │        ▼                                                  │
│  ├─ TranslateScheduler   单飞+合并+节流（对应原型 pipeline）   │
│  │        │                                                   │
│  │        ▼                                                   │
│  ├─ SubtitleState        线程安全状态（对应原型 state.py）     │
│  └─ OverlayController    WindowManager 悬浮窗（对应 overlay）  │
│         ▲                                                     │
│         └── 通知栏常驻（快捷暂停/显示隐藏/退出）               │
└─────────────────────────────────────────────────────────────┘
```

### 模块对应表（原型 → 安卓）

| 原型（Python） | 安卓（Kotlin） | 说明 |
|---|---|---|
| `酸菜鱼翻译器/wasapi.py` + `audio.py` | `AudioCaptureEngine` | `AudioRecord` + `AudioPlaybackCapture` |
| `酸菜鱼翻译器/asr.py` | `DeepgramAsrClient` / `OpenAiRealtimeAsrClient` | OkHttp WebSocket |
| `酸菜鱼翻译器/translate.py` | `Translator` + `TranslatorActor` | OkHttp/Retrofit，`/chat/completions` |
| `酸菜鱼翻译器/pipeline.py` | `SubtitlePipeline`（协程 + `Channel`） | 对应关系几乎是 1:1 |
| `酸菜鱼翻译器/state.py` | `SubtitleState`（`StateFlow`） | 用 Flow 替代锁，更简单 |
| `酸菜鱼翻译器/overlay.py` | `OverlayController` + `SubtitleOverlayView` | `WindowManager` + 自定义 View |
| `酸菜鱼翻译器/stats.py` | `LatencyStats` | 直接用 `SystemClock.elapsedRealtimeNanos()` |
| `酸菜鱼翻译器/config.py` | `SettingsRepository`（DataStore） | 加一个设置页 |
| `main.py` | `MainActivity` + `酸菜鱼翻译器Service` | |

**移植顺序建议**：先做 `OverlayController`（静态文字能挂上去）→ 再做 `AudioCaptureEngine`
（能看到采样电平）→ 接 ASR（能看到原文）→ 接翻译（看到译文）→ 最后做设置页与省电适配。

---

## 2. 权限与清单配置

```xml
<manifest xmlns:android="http://schemas.android.com/apk/res/android">

    <!-- 网络：访问云端识别/翻译 -->
    <uses-permission android:name="android.permission.INTERNET" />
    <uses-permission android:name="android.permission.ACCESS_NETWORK_STATE" />

    <!-- 后台常驻必须：前台服务 + 常驻通知 -->
    <uses-permission android:name="android.permission.FOREGROUND_SERVICE" />
    <uses-permission android:name="android.permission.FOREGROUND_SERVICE_MEDIA_PROJECTION" />
    <uses-permission android:name="android.permission.POST_NOTIFICATIONS" />

    <!-- 小窗悬浮：必须用户手动在系统设置里授予 -->
    <uses-permission android:name="android.permission.SYSTEM_ALERT_WINDOW" />

    <!-- 可选：兜底用麦克风对喇叭录音（DRM 内容录不到时） -->
    <uses-permission android:name="android.permission.RECORD_AUDIO" />

    <application ...>
        <activity
            android:name=".MainActivity"
            android:exported="true"
            android:launchMode="singleTask" />

        <!-- 悬浮窗权限申请的兜底入口（部分 ROM 必须走这里） -->
        <activity android:name=".PermissionActivity" android:exported="false" />

        <service
            android:name=".酸菜鱼翻译器Service"
            android:exported="false"
            android:foregroundServiceType="mediaProjection" />
    </application>
</manifest>
```

要点：

- **`android:foregroundServiceType="mediaProjection"`** 是 Android 10+ 的硬要求，
  少了它 `startForeground` 会直接抛异常。
- **Android 13+ 要单独申请 `POST_NOTIFICATIONS`**，否则前台服务的常驻通知看不见，
  用户会觉得"这软件在偷偷跑"，而且部分 ROM 会因此直接杀掉服务。
- **`SYSTEM_ALERT_WINDOW` 无法用运行时对话框申请**，只能 `Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION)`
  跳到系统设置页让用户手动打开，然后用 `Settings.canDrawOverlays(context)` 回查。
  这块引导做不好，用户会以为软件坏了——建议第一次进入就给一张带图示的引导页。

### 前台服务启动（Android 14 的正确姿势）

```kotlin
class 酸菜鱼翻译器Service : Service() {

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        // 1) 先起前台通知，再碰 MediaProjection —— 顺序反了会崩
        startForeground(NOTIFICATION_ID, buildNotification(), 
            ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION)

        val resultCode = intent?.getIntExtra(EXTRA_RESULT_CODE, Activity.RESULT_CANCELED) ?: return START_NOT_STICKY
        val data: Intent? = intent.getParcelableExtra(EXTRA_RESULT_DATA)
        if (data == null) { stopSelf(); return START_NOT_STICKY }

        val projection = mediaProjectionManager.getMediaProjection(resultCode, data)!!

        // 2) Android 14+ 必须为投影注册回调，否则系统会静默停止投影
        projection.registerCallback(object : MediaProjection.Callback() {
            override fun onStop() {
                // 用户从状态栏撤回了授权：清掉悬浮窗并停服务，别留个僵尸通知
                overlay.remove()
                stopSelf()
            }
        }, Handler(Looper.getMainLooper()))

        pipeline.start(projection)   // 开始采集
        return START_STICKY
    }
}
```

---

## 3. 音频采集：几个影响成败的细节

```kotlin
class AudioCaptureEngine(private val projection: MediaProjection) {

    fun open() {
        val config = AudioPlaybackCaptureConfiguration.Builder(projection)
            .addMatchingUsage(AudioAttributes.USAGE_MEDIA)
            .addMatchingUsage(AudioAttributes.USAGE_GAME)
            .build()

        val format = AudioFormat.Builder()
            .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
            .setSampleRate(16000)
            .setChannelMask(AudioFormat.CHANNEL_IN_MONO)
            .build()

        val minBuf = AudioRecord.getMinBufferSize(
            16000, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        // 缓冲给 4 倍最小值：太小会 underrun 丢数据，太大等于人为加延迟
        record = AudioRecord.Builder()
            .setAudioFormat(format)
            .setBufferSizeInBytes(minBuf * 4)
            .setAudioPlaybackCaptureConfig(config)
            .build()

        record.startRecording()
    }

    /** 20ms = 320 采样 = 640 字节；用阻塞读，读到就交给管线。 */
    suspend fun readLoop(onChunk: (ByteArray, Long) -> Unit) = withContext(Dispatchers.IO) {
        val buf = ByteArray(640)
        while (isActive) {
            val n = record.read(buf, 0, buf.size)     // 阻塞直到填满
            if (n > 0) onChunk(buf.copyOf(n), SystemClock.elapsedRealtimeNanos())
        }
    }
}
```

细节清单：

1. **采样率直接要 16000。** `AudioPlaybackCapture` 允许指定，系统会做重采样，
   比你自己在 Kotlin 里重采样省电也省延迟。真机上如果某些 ROM 拒绝了 16000，
   退化成 44100/48000 再用 `MediaCodec` 或简单抽取重采样。
2. **包大小 20~40ms。** 更小（10ms）在移动网络下反而更差：包太多、TCP/TLS 开销占比上升。
3. **别在主线程读，也别用 `AudioRecord.setOnRecordPositionUpdateListener` 回调里做重活。**
4. **`read()` 返回负值是错误码**（`ERROR_INVALID_OPERATION` 等），必须处理，
   否则会变成"静默采集失败、界面看起来在跑"。
5. **切后台/锁屏时 `AudioRecord` 可能被系统暂停**：监听 `AudioManager` 的
   `AudioManager.ACTION_AUDIO_BECOMING_NOISY` 或投影回调，必要时重启采集。
6. **省电**：在通知栏放一个"暂停字幕"按钮，暂停时 `record.stop()` 并断开 WebSocket，
   这比后台空转省电得多。

---

## 4. 网络层：把 Python 的调度逻辑照搬过来

原型里最值钱的三个设计要**原样移植**，它们是延迟和成本的关键：

### 4.1 单飞 + 合并的翻译调度

```kotlin
class TranslatorActor(
    private val translator: Translator,
    private val state: SubtitleState,
    private val stats: LatencyStats,
) {
    private val wake = Channel<Unit>(Channel.CONFLATED)
    @Volatile private var pending: Job? = null
    private var appliedSeq = 0L
    private var nextSeq = 0L

    /** 任意时候只保留"最新一次"请求：旧的直接覆盖，飞行中的结果按 seq 丢弃。 */
    fun submit(text: String, context: List<Pair<String, String>>,
               final: Boolean, capturedAtNanos: Long, sid: Long) {
        val seq = ++nextSeq
        pending = PendingJob(seq, text, context, final, capturedAtNanos, sid)
        wake.trySend(Unit)
    }

    suspend fun run() = coroutineScope {
        for (unit in wake) {
            val job = pending ?: continue
            pending = null
            if (job.seq <= appliedSeq) continue          // 已有更新的译文
            val result = runCatching { translator.translate(job.text, job.context) }
            if (job.seq <= appliedSeq) continue
            appliedSeq = job.seq
            result.onSuccess { state.patchTranslation(job.sid, it.text, job.final) }
                  .onFailure { state.markFailed(job.sid, it.message) }
        }
    }
}
```

### 4.2 中间结果的节流翻译

```kotlin
private const val SPEC_MIN_INTERVAL_MS = 350L
private const val SPEC_MIN_GROWTH = 6

fun onPartial(text: String, capturedAt: Long) {
    state.updateLive(text)
    val now = SystemClock.elapsedRealtime()
    if (text.length - lastSpecText.length >= SPEC_MIN_GROWTH &&
        now - lastSpecAt >= SPEC_MIN_INTERVAL_MS) {
        lastSpecAt = now; lastSpecText = text
        actor.submit(text, context(), final = false, capturedAt, sid)
    }
}
```

### 4.3 WebSocket 保活与重连

- Deepgram：长时间没音频要发 `{"type":"KeepAlive"}`，否则会被判空闲断开。
- 断线用**指数退避**重连（1s → 2s → 4s → … 上限 15s），并在通知栏体现状态。
- 用 `OkHttp` 的 `pingInterval(10, SECONDS)` 做底层心跳。
- **别在弱网下无限堆积**：发送队列设置上限，满了丢最旧（和原型的音频队列一个道理）。

---

## 5. 悬浮窗：WindowManager 实现

```kotlin
class OverlayController(private val context: Context) {

    private var view: SubtitleOverlayView? = null

    fun show() {
        if (!Settings.canDrawOverlays(context)) { /* 引导去授权 */ return }
        val params = WindowManager.LayoutParams(
            MATCH_PARENT_OR_FIXED_WIDTH,
            WRAP_CONTENT,
            WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,   // Android 8+ 必须用这个
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or       // 不抢焦点，不挡输入
            WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS or    // 允许超出屏幕（刘海/挖孔）
            WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN,
            PixelFormat.TRANSLUCENT,
        ).apply {
            gravity = Gravity.BOTTOM or Gravity.CENTER_HORIZONTAL
            y = (resources.displayMetrics.heightPixels * 0.07f).toInt()
        }
        view = SubtitleOverlayView(context).also {
            WindowManager(context).addView(it, params)
        }
    }

    /** 鼠标/触摸穿透：FLAG_NOT_TOUCHABLE 只在"关掉穿透"时移除 */
    fun setClickThrough(enabled: Boolean) {
        params.flags = if (enabled) params.flags or FLAG_NOT_TOUCHABLE
                       else params.flags and FLAG_NOT_TOUCHABLE.inv()
        windowManager.updateViewLayout(view, params)
    }
}
```

要点：

1. **窗口类型必须是 `TYPE_APPLICATION_OVERLAY`**（Android 8.0 起 `TYPE_PHONE`/`TYPE_SYSTEM_ALERT` 已被禁）。
2. **`FLAG_NOT_FOCUSABLE` 必加**：否则悬浮窗会抢走直播 App 的输入焦点，用户没法打字发弹幕。
3. **穿透用 `FLAG_NOT_TOUCHABLE`**，对应原型的 `WS_EX_TRANSPARENT`；
   建议在通知栏放一个"锁定/解锁位置"开关来切换，否则用户拖不动也点不到。
4. **横竖屏切换**：`onConfigurationChanged` 里重新计算宽度与 `y`。
5. **游戏/视频全屏**：如果对方 Activity 用了 `FLAG_FULLSCREEN` 且不是 `TYPE_APPLICATION_OVERLAY`，
   悬浮窗依然能盖住（这是 overlay 类型的特权），但**部分游戏会检测悬浮窗并拒绝运行**，无法规避。
6. **文字可读性**：照搬原型做法——**Canvas 手绘 + 黑色描边**，
   或给 `TextView` 设 `setShadowLayer(radius, 0, 0, Color.BLACK)`。纯色字在亮画面上看不清。
7. **别用 Compose 直接塞进 WindowManager**，需要 `ComposeView` + 独立的 `LifecycleOwner`/`SavedStateRegistry`
   才能正常工作，用自定义 `View` + Canvas 更省事也更快。

### 状态同步

服务与悬浮窗在**同一进程**时，直接共享 `SubtitleState`（内部用 `StateFlow` 或者
`@Volatile` + 主线程 post）；跨进程才需要 AIDL。建议：**Service 里持有 View 引用**，
最简单也最省电——原型里 UI 与管线分线程的做法在安卓上可以用协程取代。

---

## 6. 保活与省电（国内 ROM 是最大的坑）

安卓端"后台运行"成败基本取决于这一节。按优先级：

1. **前台服务 + 常驻通知**（必然要做）。通知里放：暂停/继续、锁定悬浮窗、退出。
2. **引导用户加白名单**，逐项做检查：
   - 电池优化白名单：`ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS`
     （注意 Google Play 对此有政策限制，自用/侧载没问题）
   - 各 ROM 的"自启动管理"：小米/红米、华为/荣耀、OPPO/一加/realme、vivo/iQOO、魅族
     —— 每个都要跳到各自的设置页，代码里做 `Build.MANUFACTURER` 分支。
   - "后台弹出界面"权限（小米/OPPO/vivo 需要，否则**悬浮窗弹不出来**）
   - 锁定后台任务（多任务界面下拉加锁）
3. **`START_STICKY` + `onTaskRemoved` 里重启**（配合前台服务）。
4. **申请 `REQUEST_IGNORE_BATTERY_OPTIMIZATIONS`** 时给用户解释用途，别硬弹。
5. **音频焦点的坑**：如果直播 App 正在播放，我们**不要**去申请
   `AudioManager` 的音频焦点（`requestAudioFocus`），否则会把直播声音打断。
   我们只做采集，不播放，全程不碰焦点。

---

## 7. 延迟优化清单（按收益排序）

| 优化项 | 收益 | 代价 |
|---|---|---|
| 采集包从 40ms 降到 20ms | −20 ms | 包数翻倍，流量略增 |
| Deepgram `endpointing` 从 300 降到 200ms | −100 ms | 句子更容易被切碎 |
| 用 `nova-3` / `gpt-4o-mini-transcribe` | −100~200 ms | 单价略高 |
| 翻译模型换更小的（`deepseek-chat` / `gpt-4o-mini`） | −200~400 ms | 质量略降 |
| 中间结果节流翻译（已在原型实现） | 感知延迟 −300ms+ | 少量重复翻译开销 |
| 服务端就近接入 + 复用连接（OkHttp 连接池） | −30~80 ms | 无 |
| 安卓端直连 WiFi 时用 PCM，移动网切 Opus | 省流量 90% | +20~40ms 编码延迟 |
| 关掉 `input_audio_noise_reduction`（OpenAI 路径） | −20~50 ms | 噪声环境识别率略降 |

**结论**：端到端 0.8~1.5s 是可以稳定做到的；想压到 0.5s 以内必须上"边识别边翻译"的
真流式方案（OpenAI Realtime 原生翻译、或 Google 的流式翻译），代价是失去原文/译文分离的灵活性。

---

## 8. 实施里程碑

| 阶段 | 交付 | 验收标准 |
|---|---|---|
| M1 | 工程骨架 + 权限引导 + 悬浮窗显示静态字幕 | 悬浮窗能挂在任意 App 前面，穿透/拖动正常，横竖屏不跑位 |
| M2 | `AudioPlaybackCapture` 采集 + 电平显示 | 播放视频时能看到实时电平波形，暂停后电平归零 |
| M3 | 接入 ASR，悬浮窗显示外文原文 | 原文延迟 < 600ms（含 endpointing） |
| M4 | 接入翻译，双语字幕（含未定稿的暖黄样式） | 端到端 < 1.5s（P90），字幕不串行错位 |
| M5 | 设置页（Key、语言、字号、位置、术语表）+ 通知栏控制 | 覆盖原型 `config.toml` 的全部可调项 |
| M6 | 保活适配（各 ROM 引导）+ 省电优化 + 崩溃恢复 | 锁屏 30 分钟、切后台 30 分钟仍持续工作 |

**M1~M4 是核心**，做完就已经是可用的产品；M5/M6 决定它能不能长期稳定用。

---

## 9. 与原型保持一致的"不要改"清单

这几条是原型里踩过坑才定下来的，移植时请保持一致：

1. **内录而不是麦克风**（除非是 DRM 内容的兜底场景）。
2. **音频队列满时丢最旧**，绝不阻塞采集。
3. **译文异步回来必须校验 sentence id**，否则会出现"上一句译文贴到下一句"。
4. **只用 F 键/固定键做全局热键**，别抢 `Ctrl+Shift+字母`（输入法冲突）——安卓端对应的是
   通知栏按钮，而不是全局按键（安卓没有全局热键，除非用无障碍服务，代价太大）。
5. **文字必须带描边/阴影**。
6. **延迟统计用真实时间戳**，不要用"代码执行了多久"糊弄自己。
