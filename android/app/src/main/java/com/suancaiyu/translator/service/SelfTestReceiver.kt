package com.suancaiyu.translator.service

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log

/**
 * 音频自测的入口。
 *
 * 为什么用广播：自测需要**已经持有 MediaProjection 的前台服务**在跑，
 * 而投影授权只能由 Activity 触发。所以流程是「先正常启动翻译 → 再发广播触发自测」。
 *
 * 用法：
 *   adb shell am broadcast -a com.suancaiyu.translator.SELFTEST -n <包名>/.service.SelfTestReceiver
 *
 * 这不是给普通用户用的功能，纯粹是排查"为什么没字幕"时的一把尺子。
 */
class SelfTestReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        Log.i("SelfTestReceiver", "收到自测请求")
        val svc = Intent(context, TranslatorService::class.java)
            .setAction(TranslatorService.ACTION_SELFTEST)
        runCatching { context.startService(svc) }
            .onFailure { Log.e("SelfTestReceiver", "启动自测失败", it) }
    }
}
