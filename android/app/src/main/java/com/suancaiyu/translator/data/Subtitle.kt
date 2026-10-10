package com.suancaiyu.translator.data

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update

/**
 * 字幕状态 —— 管线线程与 UI/悬浮窗之间的唯一桥梁。
 *
 * 用 StateFlow 而不是回调和锁：Android 上跨线程更新 UI 很容易出错，
 * StateFlow 天然线程安全，悬浮窗和服务各收各的，不需要互相持有引用。
 *
 * 行模型与 Windows 端一致：一个列表，最后一行是"正在生成中"的那句。
 * 每行有唯一 sid，异步回来的译文必须校验 sid —— 否则快速说话时会出现
 * "上一句的译文贴到下一句上"的错位（这个坑在 Windows 端踩过）。
 */
data class SubtitleLine(
    val sid: Long,
    val source: String = "",
    val translated: String = "",
    val final: Boolean = false,
    val failed: Boolean = false,
    /** 从"这段音频被播放"到译文出现的毫秒数 */
    val e2eMs: Long = 0L,
)

enum class RunState { IDLE, STARTING, RUNNING, PAUSED, ERROR }

data class SubtitleUiState(
    val lines: List<SubtitleLine> = emptyList(),
    val state: RunState = RunState.IDLE,
    /** 当前实际在用的引擎（降级后可能与首选不同） */
    val activeAsr: String = "",
    val activeMt: String = "",
    val detail: String = "",
    val errors: Int = 0,
    val e2eMs: Long = 0L,
)

class SubtitleStore(private val maxLines: Int = 2) {

    private val _state = MutableStateFlow(SubtitleUiState())
    val state: StateFlow<SubtitleUiState> = _state.asStateFlow()

    private var sidCounter = 0L

    /** 开一句新的：上一句若还挂着就定稿它 */
    @Synchronized
    fun beginSentence(): Long {
        sidCounter += 1
        val sid = sidCounter
        _state.update { s ->
            val closed = s.lines.mapIndexed { i, l ->
                if (i == s.lines.lastIndex && !l.final) l.copy(final = true) else l
            }
            val trimmed = (closed + SubtitleLine(sid = sid)).let {
                if (it.size > maxLines + 2) it.takeLast(maxLines + 2) else it
            }
            s.copy(lines = trimmed)
        }
        return sid
    }

    @Synchronized
    fun setSource(sid: Long, source: String, final: Boolean = false) {
        _state.update { s ->
            s.copy(lines = s.lines.map {
                if (it.sid == sid) it.copy(source = source, final = final || it.final) else it
            })
        }
    }

    /** 写入译文。sid 不匹配说明这句已被顶掉，直接丢弃（避免错位）。 */
    @Synchronized
    fun patchTranslation(
        sid: Long,
        translated: String,
        final: Boolean,
        e2eMs: Long = 0L,
        failed: Boolean = false,
    ): Boolean {
        var hit = false
        _state.update { s ->
            s.copy(
                lines = s.lines.map {
                    if (it.sid == sid) {
                        hit = true
                        it.copy(
                            translated = translated,
                            final = final || it.final,
                            e2eMs = if (e2eMs > 0) e2eMs else it.e2eMs,
                            failed = failed,
                        )
                    } else it
                },
                e2eMs = if (hit && e2eMs > 0) e2eMs else s.e2eMs,
            )
        }
        return hit
    }

    @Synchronized
    fun setState(state: RunState, detail: String = "") {
        _state.update { it.copy(state = state, detail = detail) }
    }

    @Synchronized
    fun setActive(asr: String? = null, mt: String? = null) {
        _state.update {
            it.copy(activeAsr = asr ?: it.activeAsr, activeMt = mt ?: it.activeMt)
        }
    }

    @Synchronized
    fun bumpError(detail: String = "") {
        _state.update { it.copy(errors = it.errors + 1, detail = detail.ifEmpty { it.detail }) }
    }

    @Synchronized
    fun clear() {
        _state.update { SubtitleUiState(state = it.state, activeAsr = it.activeAsr, activeMt = it.activeMt) }
    }
}
