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
    private lateinit var store: SubtitleStore
    private lateinit var overlay: OverlayController
    private lateinit var capture: AudioCaptureEngine
    private var pipeline: TranslationPipeline? = null
    private var projection: MediaProjection? = null

    companion object {
        private const val TAG = "TranslatorService"
        private const val NOTIF_ID = 1001

        const val ACTION_START = "com.suancaiyu.translator.START"
        const val ACTION_STOP = "com.suancaiyu.translator.ACTION_STOP"
        const val EXTRA_RESULT_CODE = "result_code"
        const val EXTRA_RESULT_DATA = "result_data"
        const val EXTRA_USE_MIC = "use_mic"

        /** 全局单例，方便 Activity/悬浮窗读取状态（同进程，不需要 AIDL） */
        @Volatile
        var store: SubtitleStore? = null
            private set

        @Volatile
        var running: Boolean = false
            private set
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        settings = Settings(this)
        store = SubtitleStore(maxLines = settings.maxLines).also { Companion.store = it }
        overlay = OverlayController(this)
        capture = AudioCaptureEngine(scope)

        // 把字幕状态推给悬浮窗（同进程，直接观察 StateFlow 即可）
        scope.launch {
            store.state.collectLatest { s -> overlay.render(s) }
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> {
                stopEverything()
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
                        store.bumpError("没有拿到录制授权")
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
            running = true
        } catch (t: Throwable) {
            // 最常见原因：没有 FOREGROUND_SERVICE_MEDIA_PROJECTION 权限，或没拿到投影授权
            Log.e(TAG, "startForeground 失败", t)
            store.bumpError("前台服务启动失败：${t.javaClass.simpleName}")
            stopEverything()
        }
    }

    private fun startWithProjection(resultCode: Int, data: Intent) {
        try {
            val mgr = getSystemService(Context.MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
            val p = mgr.getMediaProjection(resultCode, data)
            if (p == null) {
                store.bumpError("MediaProjection 获取失败（授权可能已失效）")
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

            store.setState(RunState.STARTING, "正在启动内录…")
            val ok = capture.openPlayback(p)
            if (!ok) {
                store.bumpError("内录打开失败，请重试或改用麦克风模式")
                stopEverything()
                return
            }
            Log.i(TAG, "内录已打开：${capture.stats()}")
            afterCaptureReady()
        } catch (t: Throwable) {
            Log.e(TAG, "启动投射失败", t)
            store.bumpError("启动失败：${t.javaClass.simpleName}")
            stopEverything()
        }
    }

    private fun startWithMic() {
        store.setState(RunState.STARTING, "正在启动麦克风…")
        val ok = capture.openMic()
        if (!ok) {
            store.bumpError("麦克风打开失败（检查录音权限）")
            stopEverything()
            return
        }
        afterCaptureReady()
    }

    private fun afterCaptureReady() {
        // 显示悬浮窗（没有权限时只提示，不中断翻译——用户仍可在主界面看字幕）
        val shown = overlay.show(
            showPanel = settings.overlayPanel,
            maxLines = settings.maxLines,
            bottomOffsetPx = settings.overlayBottomOffset,
            locked = settings.overlayLocked,
        )
        if (!shown) {
            store.bumpError("没有悬浮窗权限，字幕将只在 App 内显示")
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
                if (store.state.value.state == RunState.PAUSED) return@collect
                pipeline?.let { pipe ->
                    // 管线内部持有 asr 客户端；这里通过扩展方法转发
                    pipe.feedAudio(frame.pcm, frame.capturedAtMs)
                }
            }
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
            .setContentText(store.state.value.detail.ifEmpty { getString(R.string.state_running) })
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
            nm?.notify(NOTIF_ID, buildNotification(paused = store.state.value.state == RunState.PAUSED))
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
        store.setState(RunState.IDLE, "")
        running = false
        ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    override fun onDestroy() {
        runCatching { capture.release() }
        runCatching { overlay.hide() }
        runCatching { pipeline?.stop() }
        scope.cancel()
        running = false
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
