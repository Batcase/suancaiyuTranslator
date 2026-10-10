package com.suancaiyu.translator

import android.app.Application
import android.app.NotificationChannel
import android.app.NotificationManager
import android.os.Build

/**
 * 应用入口。
 *
 * 只做一件事：提前建好通知渠道。
 * 必须在使用前台服务之前建好，否则 Android 8+ 上通知会直接不显示，
 * 而前台服务没有可见通知会被系统判定为异常并杀掉。
 */
class App : Application() {

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val manager = getSystemService(NotificationManager::class.java) ?: return
        if (manager.getNotificationChannel(CHANNEL_ID) != null) return

        val channel = NotificationChannel(
            CHANNEL_ID,
            getString(R.string.notif_channel_name),
            // LOW：不响铃、不震动，但通知可见（前台服务只要求"可见"）
            NotificationManager.IMPORTANCE_LOW
        ).apply {
            description = getString(R.string.notif_channel_desc)
            setShowBadge(false)
        }
        manager.createNotificationChannel(channel)
    }

    companion object {
        const val CHANNEL_ID = "suancaiyu_translator"
    }
}
