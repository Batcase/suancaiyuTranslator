package com.suancaiyu.translator.service

import android.app.Notification
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import com.suancaiyu.translator.App
import com.suancaiyu.translator.R
import com.suancaiyu.translator.Settings
import com.suancaiyu.translator.audio.AudioCaptureEngine
import com.suancaiyu.translator.data.RunState
import com.suancaiyu.translator.data.SubtitleStore
import com.suancaiyu.translator.pipeline.TranslationPipeline
import com.suancaiyu.translator.ui.MainActivity
import com.suancaiyu.translator.ui.OverlayController
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.collectLatest
import kotlinx.coroutines.launch

/**
 * 前台服务 —— 真正干活的地方。
 *
 * 为什么必须是前台服务：Android 10+ 起，MediaProjection 只能在**前台服务**里使用；
 * 而且要声明 `foregroundServiceType="mediaProjection"`，否则 startForeground 直接抛异常。
 *
 * 启动顺序（**顺序反了会崩**）：
 *   1. 先 startForeground 把通知挂上
 *   2. 再拿 MediaProjection 并注册回调
 *   3. 最后才开始采集
 */
class TranslatorService : Service() {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private val main = Handler(Looper.getMainLooper())

    private lateinit var settings: Settings
    /** 类内引用；真正的单例在 companion 里（见 store） */
    private lateinit var subtitleStore: SubtitleStore
    private lateinit var overlay: OverlayController
    private lateinit var capture: AudioCaptureEngine
    private var pipeline: TranslationPipeline? = null
    private var projection: MediaProjection? = null

    companion object {
        private const val TAG = "TranslatorService"
        private const val NOTIF_ID = 1001

        const val ACTION_START = "com.suancaiyu.translator.START"
        const val ACTION_STOP = "com.suancaiyu.translator.ACTION_STOP"
        const val ACTION_SELFTEST = "com.suancaiyu.translator.SELFTEST"
        const val EXTRA_RESULT_CODE = "result_code"
        const val EXTRA_RESULT_DATA = "result_data"
        const val EXTRA_USE_MIC = "use_mic"

        /**
         * 字幕状态的**进程级单例**。
         *
         * 为什么不能只在 Service.onCreate 里创建：
         *   Activity 在 `refreshUi()` 时就要订阅它，而那时 Service 可能还没起来，
         *   拿到的是 null —— 结果是 **Activity 永远订阅不上**（旧代码里
         *   `store != null && !collecting` 一次不成立就再也不重试），
         *   服务启动后界面照样显示"未启动"。用户反馈的"App 内未生效"就是这个。
         *
         * 改成"首次访问就创建"：Activity 与 Service 拿到的一定是同一个实例。
         */
        private val _store: SubtitleStore by lazy { SubtitleStore(maxLines = 2) }

        val store: SubtitleStore get() = _store

        /** 按配置调整面板行数与历史长度（Service 启动时调用） */
        fun configureStore(maxLines: Int, historyLines: Int) {
            _store.maxLines = maxLines.coerceIn(1, 8)
            _store.historyLines = historyLines.coerceAtLeast(maxLines + 2)
        }

        @Volatile
        var running: Boolean = false
            private set

        internal fun setRunning(value: Boolean) {
            running = value
        }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        settings = Settings(this)
        // 用进程级单例，并按要求调整行数/历史长度
        configureStore(settings.maxLines, settings.historyLines)
        subtitleStore = _store
        overlay = OverlayController(this)
        capture = AudioCaptureEngine(scope)

        // 把字幕状态推给悬浮窗。
        //
        // ★ 必须 post 到主线程：Android 里 View 只能在创建它的线程上更新，
        //   在后台协程里直接 render() 会抛 CalledFromWrongThreadException 并让整个服务崩溃。
        //   （这个崩溃是在真机上跑出来的，编译期完全看不出来。）
        scope.launch {
            subtitleStore.state.collectLatest { s ->
                main.post { runCatching { overlay.render(s) } }
            }
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> {
                stopEverything()
                return START_NOT_STICKY
            }
            ACTION_SELFTEST -> {
                // 音频自测：播一段已知响度的正弦波，看内录能不能采到。
                // 用来区分"没声音在放"和"采集真的坏了"这两种完全不同的情况。
                runSelfTest()
                return START_NOT_STICKY
            }
            else -> {
                // 1) 先起前台通知 —— 必须在碰 MediaProjection 之前
                startForegroundCompat()

                val useMic = intent?.getBooleanExtra(EXTRA_USE_MIC, false) ?: false
                if (useMic) {
                    startWithMic()
                } else {
                    val resultCode = intent?.getIntExtra(EXTRA_RESULT_CODE, 0) ?: 0
                    @Suppress("DEPRECATION")
                    val data: Intent? = intent?.getParcelableExtra(EXTRA_RESULT_DATA)
                    if (data == null) {
                        Log.e(TAG, "缺少 MediaProjection 授权数据，无法启动")
                        subtitleStore.bumpError("没有拿到录制授权")
                        stopEverything()
                        return START_NOT_STICKY
                    }
                    startWithProjection(resultCode, data)
                }
            }
        }
        return START_STICKY
    }

    // ---------------------------------------------------------------

    private fun startForegroundCompat() {
        val notification = buildNotification(paused = false)
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                ServiceCompat.startForeground(
                    this,
                    NOTIF_ID,
                    notification,
                    // Android 14 起必须与清单里声明的类型一致
                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
                        ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION or
                            ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE
                    } else {
                        ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION
                    },
                )
            } else {
                startForeground(NOTIF_ID, notification)
            }
            setRunning(true)
        } catch (t: Throwable) {
            // 最常见原因：没有 FOREGROUND_SERVICE_MEDIA_PROJECTION 权限，或没拿到投影授权
            Log.e(TAG, "startForeground 失败", t)
            subtitleStore.bumpError("前台服务启动失败：${t.javaClass.simpleName}")
            stopEverything()
        }
    }

    private fun startWithProjection(resultCode: Int, data: Intent) {
        try {
            val mgr = getSystemService(Context.MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
            val p = mgr.getMediaProjection(resultCode, data)
            if (p == null) {
                subtitleStore.bumpError("MediaProjection 获取失败（授权可能已失效）")
                stopEverything()
                return
            }
            projection = p
            // 必须注册回调：用户从状态栏"停止投射"时，系统会回调这里，
            // 我们要清理悬浮窗并停服务，否则会留下僵尸通知
            p.registerCallback(
                object : MediaProjection.Callback() {
                    override fun onStop() {
                        Log.i(TAG, "用户停止了投射")
                        stopEverything()
                    }
                },
                main,
            )

            subtitleStore.setState(RunState.STARTING, "正在启动内录…")
            val ok = capture.openPlayback(p)
            if (!ok) {
                subtitleStore.bumpError("内录打开失败，请重试或改用麦克风模式")
                stopEverything()
                return
            }
            Log.i(TAG, "内录已打开：${capture.stats()}")
            afterCaptureReady()
        } catch (t: Throwable) {
            Log.e(TAG, "启动投射失败", t)
            subtitleStore.bumpError("启动失败：${t.javaClass.simpleName}")
            stopEverything()
        }
    }

    private fun startWithMic() {
        subtitleStore.setState(RunState.STARTING, "正在启动麦克风…")
        val ok = capture.openMic()
        if (!ok) {
            subtitleStore.bumpError("麦克风打开失败（检查录音权限）")
            stopEverything()
            return
        }
        afterCaptureReady()
    }

    private fun afterCaptureReady() {
        // 周期打印采集统计：内录失败最常见、最难查的表现就是"界面在跑但没声音"，
        // 有这行日志一眼就能看出是采集问题还是识别问题
        scope.launch {
            var lastFrames = 0L
            var quietTicks = 0
            while (true) {
                kotlinx.coroutines.delay(2000)
                val frames = capture.frameCount
                val delta = frames - lastFrames
                lastFrames = frames
                val stats = "frames=$frames(+$delta) peak=${"%.4f".format(capture.peakRms)} " +
                    "last=${"%.4f".format(capture.lastRms)} bytes=${capture.capturedBytes} " +
                    "dropped=${capture.droppedCount}"
                Log.i(TAG, "采集统计 $stats")
                if (capture.peakRms < 0.001f) {
                    quietTicks += 1
                    if (quietTicks == 5) {
                        val msg = "连续 10 秒没采到声音 —— 可能是没在播放、" +
                            "被采集的 App 禁止录制、或**换了输出设备（例如插上蓝牙耳机）**。" +
                            "可改用麦克风模式"
                        Log.w(TAG, msg)
                        // 也反映到界面上：否则用户只看到"在跑但没字幕"，无从判断
                        subtitleStore.setState(
                            RunState.RUNNING,
                            "没采到声音：可试麦克风模式",
                        )
                    }
                } else {
                    quietTicks = 0
                }
            }
        }

        // 显示悬浮窗（没有权限时只提示，不中断翻译——用户仍可在主界面看字幕）
        val shown = overlay.show(
            showPanel = settings.overlayPanel,
            maxLines = settings.maxLines,
            bottomOffsetPx = settings.overlayBottomOffset,
            locked = settings.overlayLocked,
        )
        if (!shown) {
            subtitleStore.bumpError("没有悬浮窗权限，字幕将只在 App 内显示")
        }

        // 启动管线
        val p = TranslationPipeline(settings, store)
        if (!p.start()) {
            Log.e(TAG, "管线启动失败")
            stopEverything()
            return
        }
        pipeline = p

        // 把采集到的音频喂给识别
        scope.launch {
            capture.frames.collect { frame ->
                if (subtitleStore.state.value.state == RunState.PAUSED) return@collect
                pipeline?.let { pipe ->
                    // 管线内部持有 asr 客户端；这里通过扩展方法转发
                    pipe.feedAudio(frame.pcm, frame.capturedAtMs)
                    // 顺便驱动一次"静默兜底"检查：服务端迟迟不给 sentence_end 时，
                    // 由它把攒着的整句发出去，否则字幕会一直空着。
                    // 采集是 ~50 帧/秒，真的检查由管线内部自己按时间判断，这里只负责给时机。
                    pipe.onTick()
                }
            }
        }
    }

    // ---------------------------------------------------------------
    // 音频自测
    // ---------------------------------------------------------------

    /**
     * 播放一段正弦波并观察内录的响度读数。
     *
     * 判定逻辑：
     *   * 播放前 peak 已经很高 → 本来就有声音，说明采集通路是通的
     *   * 播放后 peak 明显上升 → **内录确实能采到播放的音频**（这是我们想证实的）
     *   * 播放后 peak 仍是 0 → 采集通路有问题（或被系统策略拒绝）
     */
    private fun runSelfTest() {
        scope.launch {
            val before = capture.peakRms
            Log.i(TAG, "=== 音频自测开始 === 播放前 peak=${"%.4f".format(before)}")

            // 优先播**内置日语样本**：这样能顺带走完"内录 → 识别 → 翻译"整条链路，
            // 直接在日志里看到识别结果对不对（正弦波只能证明采到音频了）。
            val resId = resources.getIdentifier("ja_sample", "raw", packageName)
            val played = if (resId != 0) {
                Log.i(TAG, "音频自测：播放内置日语样本")
                com.suancaiyu.translator.audio.TonePlayer.playAsset(this@TranslatorService, resId)
            } else {
                Log.i(TAG, "音频自测：没有内置样本，退回正弦波")
                com.suancaiyu.translator.audio.TonePlayer.play(
                    freqHz = 440.0, seconds = 4.0, amplitude = 0.7
                )
            }
            if (!played) {
                Log.e(TAG, "=== 音频自测失败 === 测试音频没能播放（设备音频输出有问题）")
                return@launch
            }
            // 等采集侧统计刷新
            kotlinx.coroutines.delay(1500)
            val after = capture.peakRms
            val ok = after > 0.005f
            Log.i(
                TAG,
                "=== 音频自测结果 === 播放前=${"%.4f".format(before)} " +
                    "播放后=${"%.4f".format(after)} " +
                    "内录frames=${capture.frameCount} dropped=${capture.droppedCount} " +
                    "→ ${if (ok) "✅ 内录能采到播放中的音频" else "❌ 内录没采到任何声音"}"
            )
        }
    }

    // ---------------------------------------------------------------

    private fun buildNotification(paused: Boolean): Notification {
        val open = PendingIntent.getActivity(
            this, 0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val stop = PendingIntent.getService(
            this, 1,
            Intent(this, TranslatorService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        return NotificationCompat.Builder(this, App.CHANNEL_ID)
            .setContentTitle(getString(R.string.notif_title))
            .setContentText(subtitleStore.state.value.detail.ifEmpty { getString(R.string.state_running) })
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setContentIntent(open)
            .addAction(0, getString(R.string.notif_action_stop), stop)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .build()
    }

    private fun updateNotification() {
        runCatching {
            val nm = getSystemService(NotificationManager::class.java)
            nm?.notify(NOTIF_ID, buildNotification(paused = subtitleStore.state.value.state == RunState.PAUSED))
        }
    }

    private fun stopEverything() {
        Log.i(TAG, "停止服务")
        runCatching { pipeline?.stop() }
        pipeline = null
        runCatching { capture.release() }
        runCatching { overlay.hide() }
        runCatching { projection?.stop() }
        projection = null
        subtitleStore.setState(RunState.IDLE, "")
        setRunning(false)
        ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    override fun onDestroy() {
        runCatching { capture.release() }
        runCatching { overlay.hide() }
        runCatching { pipeline?.stop() }
        scope.cancel()
        setRunning(false)
        super.onDestroy()
    }
}

/** 用户从系统状态栏停止投射时会走这里（部分 ROM 会发这个广播）。 */
class MediaProjectionStopReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        context.startService(
            Intent(context, TranslatorService::class.java).setAction(TranslatorService.ACTION_STOP)
        )
    }
}
