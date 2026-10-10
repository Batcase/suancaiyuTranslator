# 自用为主，release 目前不开混淆（isMinifyEnabled = false）。
# 真要发布时打开混淆，再把下面这些保留规则放开。

# OkHttp 在低版本上有反射调用，需要保留
-dontwarn okhttp3.**
-dontwarn okio.**
-dontwarn org.conscrypt.**
-dontwarn org.bouncycastle.**
-dontwarn org.openjsse.**

# WebSocket 与 JSON 解析用到的类
-keep class com.suancaiyu.translator.asr.** { *; }
-keep class com.suancaiyu.translator.mt.** { *; }

# Kotlin 元数据
-keep class kotlin.Metadata { *; }
