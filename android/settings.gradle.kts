// 依赖仓库。
// google() / mavenCentral() 在国内直连通常较慢，所以额外挂阿里云镜像在前，
// 能明显加快首次同步（Android Studio 会按顺序尝试，镜像没有的包会回落到官方源）。
pluginManagement {
    repositories {
        maven("https://maven.aliyun.com/repository/gradle-plugin")
        maven("https://maven.aliyun.com/repository/google")
        maven("https://maven.aliyun.com/repository/public")
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        maven("https://maven.aliyun.com/repository/google")
        maven("https://maven.aliyun.com/repository/public")
        google()
        mavenCentral()
    }
}

rootProject.name = "酸菜鱼翻译器"
include(":app")
