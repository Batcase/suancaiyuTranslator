"""酸菜鱼翻译器 —— 直播实时翻译。

包结构：
    config      配置加载（TOML + 环境变量 + CLI 覆盖）
    audio       WASAPI loopback 系统内录采集
    asr         流式语音识别客户端（Deepgram / OpenAI Realtime）
    translate   流式翻译（增量翻译 + 稳定化 + 上下文）
    overlay     置顶半透明悬浮字幕窗
    pipeline    编排层：把音频→识别→翻译→字幕串起来
    stats       端到端延迟统计
"""

__version__ = "1.1beta"
__all__ = ["__version__"]
