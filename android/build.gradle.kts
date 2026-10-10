// 顶层构建文件。插件版本集中在这里声明，子模块只 apply。
//
// 版本选择说明（2026-10）：
//   AGP 8.5.x + Kotlin 1.9.24 —— 这一对兼容性最稳，且要求的 JDK 是 17/21，
//   正好是 Android Studio 自带的 JBR 版本（不用额外装 JDK）。
plugins {
    id("com.android.application") version "8.5.2" apply false
    id("org.jetbrains.kotlin.android") version "1.9.24" apply false
}
