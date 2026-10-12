// 应用模块构建脚本。
//
// 几个刻意的选择：
//   * minSdk 29（Android 10）—— AudioPlaybackCapture 从 API 29 才有，
//     低于它的系统无法做"内录别的 App 的声音"，这是硬门槛。
//   * targetSdk 34 —— 前台服务 mediaProjection 的强制要求从 34 开始收紧，
//     直接对齐能避免上架/安装时的兼容问题。
//   * Java/Kotlin 目标 17 —— AGP 8.x 的最低要求，也是 Android Studio
//     自带运行时能保证的版本。
plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.suancaiyu.translator"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.suancaiyu.translator"
        minSdk = 29
        targetSdk = 34
        versionCode = 2
        // 与 PC 端保持一致（Android 的 versionName 允许字母后缀）
        versionName = "1.1beta"

        // 云端识别/翻译的默认配置（也可在 App 内设置页里改）
        buildConfigField("String", "DEFAULT_ASR_PROVIDER", "\"paraformer\"")
        buildConfigField("String", "DEFAULT_MT_PROVIDER", "\"deepseek\"")
        buildConfigField("String", "DEFAULT_SOURCE_LANG", "\"ja\"")
        buildConfigField("String", "DEFAULT_TARGET_LANG", "\"简体中文\"")
    }

    buildTypes {
        release {
            // 自用为主，先不混淆；真要发布再打开并配规则
            isMinifyEnabled = false
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
        }
        debug {
            applicationIdSuffix = ".debug"
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }

    buildFeatures {
        viewBinding = true
        buildConfig = true
    }

    packaging {
        resources.excludes += setOf(
            "META-INF/AL2.0",
            "META-INF/LGPL2.1",
            "META-INF/*.kotlin_module",
        )
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.google.android.material:material:1.12.0")
    implementation("androidx.constraintlayout:constraintlayout:2.1.4")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.4")
    implementation("androidx.lifecycle:lifecycle-service:2.8.4")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")

    // WebSocket（Paraformer / Deepgram 都走流式）与 HTTP（翻译、整句识别）
    implementation("com.squareup.okhttp3:okhttp:4.12.0")

    testImplementation("junit:junit:4.13.2")
}
