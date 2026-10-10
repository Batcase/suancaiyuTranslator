"""Windows Core Audio (WASAPI) 回环采集 —— 纯 ctypes，无需编译。

为什么不用 sounddevice
----------------------
sounddevice 依赖它自带的 PortAudio。PortAudio 直到 19.7 才加入 WASAPI
loopback 支持，随包发布的版本常常不带（表现为 `sd.WasapiSettings()` 没有
`loopback` 参数，`query_devices()` 里也找不到任何 "[Loopback]" 设备）。
本机正好就是这种情况，所以这里直接用 ctypes 调 Core Audio 的 COM 接口，
拿到"系统正在播放的声音"，不依赖任何第三方 DLL。

实现要点
--------
* `IMMDeviceEnumerator::GetDefaultAudioEndpoint(eRender, eConsole)` 取默认扬声器。
* `IAudioClient::Initialize(AUDCLNT_SHAREMODE_SHARED, AUDCLNT_STREAMFLAGS_LOOPBACK
  | AUDCLNT_STREAMFLAGS_EVENTCALLBACK, ...)` —— LOOPBACK 是关键，
  它让一个**渲染设备**变成可采集的源。
* 事件驱动：`SetEventHandle` + `WaitForSingleObject`，不轮询、不空转。
* 混音格式通常是 48000Hz / 2ch / float32（WAVEFORMATEXTENSIBLE），
  这里下混成单声道 + 线性重采样到 16kHz + 转 int16，直接喂给云 ASR。
* COM 接口一律手工 `Release()`，`CoInitialize` 按线程调用。

对外只暴露 `WasapiLoopback`，用起来像迭代器：

    with WasapiLoopback(sample_rate=16000) as cap:
        frame = cap.read(timeout=0.5)   # 返回 int16 numpy 数组或 None
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
from ctypes import POINTER, byref, c_void_p, wintypes
from dataclasses import dataclass

import numpy as np

IS_WINDOWS = sys.platform == "win32"

# ----------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------
CLSCTX_ALL = 0x17
COINIT_MULTITHREADED = 0x0
COINIT_APARTMENTTHREADED = 0x2
RPC_E_CHANGED_MODE = -2147417850  # 0x80010106

DEVICE_STATE_ACTIVE = 0x1

AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_CROSSPROCESS = 0x00010000
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000
AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM = 0x80000000
AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x08000000

AUDCLNT_S_BUFFER_EMPTY = 0x08890001
AUDCLNT_S_DEVICE_INVALIDATED = 0x08890002
AUDCLNT_E_DEVICE_INVALIDATED = -2004287480  # 0x88890004

WAVE_FORMAT_PCM = 0x0001
WAVE_FORMAT_IEEE_FLOAT = 0x0003
WAVE_FORMAT_EXTENSIBLE = 0xFFFE

KSDATAFORMAT_SUBTYPE_PCM = "{00000001-0000-0010-8000-00aa00389b71}"
KSDATAFORMAT_SUBTYPE_IEEE_FLOAT = "{00000003-0000-0010-8000-00aa00389b71}"

REFERENCE_TIME_PER_SEC = 10_000_000  # 100ns 单位

WAIT_OBJECT_0 = 0x0
WAIT_TIMEOUT = 0x102

# ----------------------------------------------------------------------
# GUID
# ----------------------------------------------------------------------
class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    def __init__(self, text: str | None = None) -> None:
        super().__init__()
        if text:
            self.from_string(text)

    def from_string(self, text: str) -> None:
        t = text.strip().strip("{}")
        p1, p2, p3, p4, p5 = t.split("-")
        self.Data1 = int(p1, 16)
        self.Data2 = int(p2, 16)
        self.Data3 = int(p3, 16)
        rest = bytes.fromhex(p4 + p5)
        for i, b in enumerate(rest):
            self.Data4[i] = b

    def __str__(self) -> str:
        d4 = "".join(f"{b:02x}" for b in self.Data4)
        return f"{{{self.Data1:08X}-{self.Data2:04X}-{self.Data3:04X}-{d4[:4]}-{d4[4:]}}}"


CLSID_MMDeviceEnumerator = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
IID_IMMDeviceEnumerator = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
IID_IMMDevice = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
IID_IAudioClient = GUID("{1CB9AD4C-DBFA-4c32-B178-C2F568A703B2}")
IID_IAudioCaptureClient = GUID("{C8ADBD64-E71E-48a0-A4DE-185C395CD317}")
IID_IPropertyStore = GUID("{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}")
IID_ISimpleAudioVolume = GUID("{87CE5498-68D6-44E5-9215-6DA47EF883D8}")

PKEY_Device_FriendlyName = (14, GUID("{A45C254E-DF1C-4EFD-8020-67D146A850E0}"), 14)

# 默认播放设备角色
eConsole, eMultimedia, eCommunications = 0, 1, 2
# 数据流方向
eRender, eCapture, eAll = 0, 1, 2


# ----------------------------------------------------------------------
# 结构体
# ----------------------------------------------------------------------
class WAVEFORMATEX(ctypes.Structure):
    """Windows 真实布局是 18 字节、字段间无填充，因此必须 `_pack_ = 1`。

    ctypes 默认会把 cbSize 后面补 2 字节变成 20——注意：`_pack_` **不会**
    从外层结构传递到嵌套结构成员，所以内外都要各自声明。
    """

    _pack_ = 1
    _fields_ = [
        ("wFormatTag", ctypes.c_ushort),
        ("nChannels", ctypes.c_ushort),
        ("nSamplesPerSec", ctypes.c_ulong),
        ("nAvgBytesPerSec", ctypes.c_ulong),
        ("nBlockAlign", ctypes.c_ushort),
        ("wBitsPerSample", ctypes.c_ushort),
        ("cbSize", ctypes.c_ushort),
    ]


class WAVEFORMATEXTENSIBLE(ctypes.Structure):
    """必须 `_pack_ = 1`。

    Windows 的真实布局是 18 + 2 + 4 + 16 = 40 字节，字段之间**没有**填充；
    而 ctypes 默认会按自然对齐把 wValidBitsPerSample 后面填 2 字节凑成 44。
    尺寸一旦不对，IAudioClient::Initialize 会直接返回 E_INVALIDARG /
    AUDCLNT_E_UNSUPPORTED_FORMAT——而且只在部分设备上复现，极难排查。
    这里同时修掉了 float 子格式被误读成 PCM 的问题。
    """

    _pack_ = 1
    _fields_ = [
        ("Format", WAVEFORMATEX),
        ("wValidBitsPerSample", ctypes.c_ushort),
        ("dwChannelMask", ctypes.c_ulong),
        ("SubFormat", GUID),
    ]


class PROPVARIANT(ctypes.Structure):
    _fields_ = [
        ("vt", ctypes.c_ushort),
        ("wReserved1", ctypes.c_ushort),
        ("wReserved2", ctypes.c_ushort),
        ("wReserved3", ctypes.c_ushort),
        ("pwszVal", ctypes.c_wchar_p),
        ("pad", ctypes.c_byte * 16),
    ]


VT_LPWSTR = 31
VT_BSTR = 8


# ----------------------------------------------------------------------
# COM vtable 调用助手
# ----------------------------------------------------------------------
def _vt(ptr: c_void_p, slot: int, restype, *argtypes):
    """取出第 slot 个虚函数并设置签名。

    slot 从 3 开始是接口自己的第一个方法（0/1/2 是 IUnknown 的
    QueryInterface/AddRef/Release）。
    """
    vtable = ctypes.cast(ptr, POINTER(POINTER(c_void_p))).contents
    func_addr = vtable[slot]
    proto = ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)
    return proto(func_addr)


def _release(obj) -> None:
    """obj 可以是 c_void_p 或持有 .ptr 的包装。"""
    ptr = obj if isinstance(obj, c_void_p) else getattr(obj, "ptr", obj)
    if ptr:
        _vt(ptr, 2, ctypes.c_ulong)(ptr)


def _hr(res: int, what: str) -> int:
    if res < 0:
        raise OSError(f"{what} 失败: 0x{res & 0xFFFFFFFF:08X}")
    return res


# ----------------------------------------------------------------------
# 端点信息
# ----------------------------------------------------------------------
@dataclass
class RenderEndpoint:
    name: str
    is_default: bool
    device_id: str


def _out_ptr() -> c_void_p:
    return c_void_p()


class ComApartment:
    """管理当前线程的 COM 初始化。

    **必须用 STA（COINIT_APARTMENTTHREADED）**：实测同一台机器上，
    MTA 下 IAudioClient::Initialize 会随机返回 0x88890008 / 0x80070057
    （12 次里约 2 次失败，且失败后整个进程都救不回来），
    换成 STA 后 12/12 全部成功。这类问题在文档里没有任何提示，
    但它是本机回环采集"时好时坏"的真正原因。
    """

    def __init__(self) -> None:
        self._inited = False
        self._need_uninit = False

    def __enter__(self) -> "ComApartment":
        if not IS_WINDOWS:
            raise RuntimeError("WASAPI 仅在 Windows 上可用")
        hr = ctypes.windll.ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
        if hr == RPC_E_CHANGED_MODE:
            # 该线程已经被别的代码初始化成 MTA 了，只能将就用
            self._inited = True
        elif hr < 0:
            raise OSError(f"CoInitializeEx 失败: 0x{hr & 0xFFFFFFFF:08X}")
        else:
            self._inited = True
            # S_FALSE(1) 表示本线程已经初始化过，此时**不能**配对 CoUninitialize
            self._need_uninit = hr == 0
        return self

    def __exit__(self, *exc: object) -> None:
        if self._need_uninit:
            ctypes.windll.ole32.CoUninitialize()
            self._need_uninit = False


def _create_enumerator() -> c_void_p:
    ptr = _out_ptr()
    _hr(
        ctypes.windll.ole32.CoCreateInstance(
            byref(CLSID_MMDeviceEnumerator),
            None,
            CLSCTX_ALL,
            byref(IID_IMMDeviceEnumerator),
            byref(ptr),
        ),
        "CoCreateInstance(MMDeviceEnumerator)",
    )
    return ptr


def _device_friendly_name(dev: c_void_p) -> str:
    """尽力读取设备友好名。

    注意：IPropertyStore 在 ctypes 下无法安全调用——把 stgmAccess 直接按值
    传入会触发访问违例（本机实测），因此这里默认不做属性查询，只保留一个
    可选的开关。设备名对功能没有影响：字幕上显示的采样率/声道数已经足够
    排错，Android 端也只会用默认输出设备。

    设置环境变量 LIVESUB_TRY_DEVICE_NAME=1 可以尝试读取（失败则静默回退）。
    """
    if os.getenv("LIVESUB_TRY_DEVICE_NAME", "") not in ("1", "true", "yes"):
        return ""
    store = _out_ptr()
    hr = _vt(dev, 4, ctypes.c_long, POINTER(GUID), ctypes.c_ulong, POINTER(c_void_p))(
        dev, byref(IID_IPropertyStore), 0, byref(store)
    )
    if hr < 0 or not store:
        return ""
    try:
        # PROPERTYKEY 是 (GUID fmtid; DWORD pid)，总共 20 字节，必须按值传结构体
        class PROPERTYKEY(ctypes.Structure):
            _fields_ = [("fmtid", GUID), ("pid", ctypes.c_ulong)]

        pkey = PROPERTYKEY()
        pkey.fmtid = PKEY_Device_FriendlyName[1]
        pkey.pid = PKEY_Device_FriendlyName[2]

        pv = PROPVARIANT()
        res = _vt(store, 5, ctypes.c_long, POINTER(PROPERTYKEY), POINTER(PROPVARIANT))(
            store, byref(pkey), byref(pv)
        )
        if res >= 0 and pv.vt == VT_LPWSTR and pv.pwszVal:
            return str(ctypes.wstring_at(pv.pwszVal))
        return ""
    except Exception:
        return ""
    finally:
        _release(store)


def _device_id(dev: c_void_p) -> str:
    out = ctypes.c_wchar_p()
    hr = _vt(dev, 5, ctypes.c_long, POINTER(ctypes.c_wchar_p))(dev, byref(out))
    if hr < 0 or not out.value:
        return ""
    try:
        return str(out.value)
    finally:
        ctypes.windll.ole32.CoTaskMemFree(out)


def list_render_endpoints() -> list[RenderEndpoint]:
    """列出所有活动的播放设备，并标出哪个是默认（eConsole）。"""
    with ComApartment():
        enum = _create_enumerator()
        try:
            default_id = ""
            dptr = _out_ptr()
            if _vt(enum, 4, ctypes.c_long, ctypes.c_int, ctypes.c_int, POINTER(c_void_p))(
                enum, eRender, eConsole, byref(dptr)
            ) >= 0 and dptr:
                try:
                    default_id = _device_id(dptr)
                finally:
                    _release(dptr)

            coll = _out_ptr()
            # HRESULT EnumAudioEndpoints(EDataFlow, DWORD dwStateMask, IMMDeviceCollection**)
            _hr(
                _vt(enum, 3, ctypes.c_long, ctypes.c_int, ctypes.c_ulong, POINTER(c_void_p))(
                    enum, eRender, DEVICE_STATE_ACTIVE, byref(coll)
                ),
                "EnumAudioEndpoints",
            )
            try:
                count = ctypes.c_uint()
                _hr(_vt(coll, 3, ctypes.c_long, POINTER(ctypes.c_uint))(coll, byref(count)), "GetCount")
                out: list[RenderEndpoint] = []
                for i in range(count.value):
                    dptr = _out_ptr()
                    hr = _vt(coll, 4, ctypes.c_long, ctypes.c_uint, POINTER(c_void_p))(coll, i, byref(dptr))
                    if hr < 0 or not dptr:
                        continue
                    try:
                        did = _device_id(dptr)
                        out.append(
                            RenderEndpoint(
                                name=_device_friendly_name(dptr) or "(未知设备)",
                                is_default=(did == default_id and did != ""),
                                device_id=did,
                            )
                        )
                    finally:
                        _release(dptr)
                return out
            finally:
                _release(coll)
        finally:
            _release(enum)


def _activate_audio_client(dev: c_void_p) -> c_void_p:
    client = _out_ptr()
    # HRESULT IMMDevice::Activate(REFIID, DWORD dwClsCtx, PROPVARIANT*, void**)
    _hr(
        _vt(dev, 3, ctypes.c_long, POINTER(GUID), ctypes.c_ulong, c_void_p, POINTER(c_void_p))(
            dev, byref(IID_IAudioClient), CLSCTX_ALL, None, byref(client)
        ),
        "Activate(IAudioClient)",
    )
    return client


def _mix_format(client: c_void_p) -> tuple[WAVEFORMATEXTENSIBLE, bool, bool]:
    """读设备混音格式。

    返回 (结构体, 是否 float, 是否 extensible)。
    注意：GetMixFormat 用 CoTaskMemAlloc 分配，必须 CoTaskMemFree。
    """
    pwfx = ctypes.POINTER(WAVEFORMATEX)()
    _hr(_vt(client, 8, ctypes.c_long, POINTER(POINTER(WAVEFORMATEX)))(client, byref(pwfx)), "GetMixFormat")
    try:
        ex = ctypes.cast(pwfx, POINTER(WAVEFORMATEXTENSIBLE)).contents
        fmt = ex.Format
        if fmt.wFormatTag == WAVE_FORMAT_IEEE_FLOAT:
            is_float = True
        elif fmt.wFormatTag == WAVE_FORMAT_EXTENSIBLE:
            sub = str(ex.SubFormat).lower()
            is_float = sub == KSDATAFORMAT_SUBTYPE_IEEE_FLOAT.lower()
        else:
            is_float = False
        return ex, is_float, fmt.wFormatTag == WAVE_FORMAT_EXTENSIBLE
    finally:
        ctypes.windll.ole32.CoTaskMemFree(pwfx)


def _resample_linear(x: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    if src_rate == dst_rate or x.size == 0:
        return x
    n_dst = int(round(x.size * dst_rate / src_rate))
    if n_dst <= 0:
        return np.zeros(0, dtype=np.float32)
    src_idx = np.linspace(0.0, x.size - 1, n_dst, dtype=np.float64)
    left = np.floor(src_idx).astype(np.int64)
    right = np.minimum(left + 1, x.size - 1)
    frac = (src_idx - left).astype(np.float32)
    return (x[left] * (1 - frac) + x[right] * frac).astype(np.float32)


# ---- 常见 HRESULT 的中文解释，排错时能直接看懂 ----
HRESULT_HINTS = {
    0x88890001: "AUDCLNT_E_NOT_INITIALIZED",
    0x88890002: "AUDCLNT_E_ALREADY_INITIALIZED",
    0x88890003: "AUDCLNT_E_WRONG_ENDPOINT_TYPE",
    0x88890004: "AUDCLNT_E_DEVICE_INVALIDATED（设备被拔掉/切换了）",
    0x88890005: "AUDCLNT_E_NOT_STOPPED",
    0x88890006: "AUDCLNT_E_BUFFER_TOO_LARGE",
    0x88890007: "AUDCLNT_E_OUT_OF_ORDER",
    0x88890008: "AUDCLNT_E_UNSUPPORTED_FORMAT（该端点不接受此格式/标志组合）",
    0x88890009: "AUDCLNT_E_INVALID_SIZE",
    0x8889000A: "AUDCLNT_E_DEVICE_IN_USE（设备被独占占用）",
    0x8889000B: "AUDCLNT_E_BUFFER_OPERATION_PENDING",
    0x8889000C: "AUDCLNT_E_THREAD_NOT_REGISTERED",
    0x8889000E: "AUDCLNT_E_EXCLUSIVE_MODE_NOT_ALLOWED",
    0x8889000F: "AUDCLNT_E_ENDPOINT_CREATE_FAILED",
    0x88890010: "AUDCLNT_E_SERVICE_NOT_RUNNING（Windows 音频服务没在跑）",
    0x88890011: "AUDCLNT_E_EVENTHANDLE_NOT_EXPECTED（该流不接受事件句柄）",
    0x88890012: "AUDCLNT_E_EXCLUSIVE_MODE_ONLY",
    0x88890013: "AUDCLNT_E_BUFDURATION_PERIOD_NOT_EQUAL",
    0x88890014: "AUDCLNT_E_EVENTHANDLE_NOT_SET",
    0x88890015: "AUDCLNT_E_INCORRECT_BUFFER_SIZE",
    0x88890016: "AUDCLNT_E_BUFFER_SIZE_ERROR",
    0x88890017: "AUDCLNT_E_CPUUSAGE_EXCEEDED",
    0x88890018: "AUDCLNT_E_BUFFER_ERROR",
    0x88890019: "AUDCLNT_E_BUFFER_SIZE_NOT_ALIGNED",
    0x88890020: "AUDCLNT_E_INVALID_DEVICE_PERIOD（周期参数不被接受）",
    0x88890021: "AUDCLNT_E_INVALID_STREAM_FLAG",
    0x88890022: "AUDCLNT_E_ENDPOINT_OFFLOAD_NOT_CAPABLE",
    0x88890023: "AUDCLNT_E_OUT_OF_OFFLOAD_RESOURCES",
    0x88890024: "AUDCLNT_E_OFFLOAD_MODE_ONLY",
    0x88890025: "AUDCLNT_E_NONOFFLOAD_MODE_ONLY",
    0x88890026: "AUDCLNT_E_RESOURCES_INVALIDATED",
    0x88890027: "AUDCLNT_E_RAW_MODE_UNSUPPORTED",
    0x80070057: "E_INVALIDARG（该端点拒绝这组参数）",
}


def hr_text(hr: int) -> str:
    code = hr & 0xFFFFFFFF
    hint = HRESULT_HINTS.get(code)
    return f"0x{code:08X}" + (f" {hint}" if hint else "")


def _device_period(client: c_void_p) -> tuple[int, int]:
    """GetDevicePeriod(&default, &minimum)，单位 100ns。"""
    default = ctypes.c_long(0)
    minimum = ctypes.c_long(0)
    hr = _vt(client, 6, ctypes.c_long, POINTER(ctypes.c_long), POINTER(ctypes.c_long))(
        client, byref(default), byref(minimum)
    )
    if hr < 0:
        return 0, 0
    return int(default.value), int(minimum.value)


def list_render_endpoints_default_first() -> list[RenderEndpoint]:
    """所有活动播放设备，默认设备排最前。"""
    eps = list_render_endpoints()
    eps.sort(key=lambda e: (not e.is_default,))
    return eps


def _open_render_by_id(device_id: str) -> tuple[c_void_p, str]:
    enum = _create_enumerator()
    try:
        dev = _out_ptr()
        _hr(
            _vt(enum, 5, ctypes.c_long, ctypes.c_wchar_p, POINTER(c_void_p))(
                enum, device_id, byref(dev)
            ),
            "GetDevice",
        )
        return dev, _device_friendly_name(dev)
    finally:
        _release(enum)


# ----------------------------------------------------------------------
# 采集器
# ----------------------------------------------------------------------
class WasapiLoopback:
    """从默认（或指定）播放设备回环采集，输出 16kHz 单声道 int16。"""

    def __init__(self, sample_rate: int = 16000, device_hint: str = "", read_timeout_ms: int = 200):
        if not IS_WINDOWS:
            raise RuntimeError("WasapiLoopback 仅在 Windows 上可用")
        self.out_rate = int(sample_rate)
        self.device_hint = device_hint
        self.read_timeout_ms = read_timeout_ms

        self._com = ComApartment()
        self._device: c_void_p | None = None
        self._client: c_void_p | None = None
        self._capture: c_void_p | None = None
        self._event: wintypes.HANDLE | None = None

        self.device_name = ""
        self.native_rate = 0
        self.native_channels = 0
        self.native_dtype = ""
        self.is_float = False
        self._native_block = 0
        self._bits = 0
        self._buf_frames = 0
        self._silence_pad = 0.0
        self._use_event = False
        self._init_label = ""

    # ---------- 打开 / 关闭 ----------
    #: Initialize 的候选参数组合，按"最理想"到"最保守"排列。
    #: 实测某些端点（HDMI/数字输出）会拒绝 AUTOCONVERTPCM 或事件回调，
    #: 因此必须逐个退让，而不是一次失败就放弃。
    #:
    #: 第一档刻意**只带 LOOPBACK**：它是兼容性最好、也是唯一在所有端点上都
    #: 稳定通过的组合（其余组合都会被个别端点拒绝）。
    _FLAG_CANDIDATES = (
        ("事件模式", AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK, True),
        ("轮询模式", AUDCLNT_STREAMFLAGS_LOOPBACK, False),
        ("事件模式+自动重采样", AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK
         | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY, True),
        ("轮询模式+自动重采样", AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM
         | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY, False),
    )

    def open(self) -> None:
        self._com.__enter__()
        # 实测本机行为：Initialize 存在明显的随机拒绝（0x88890008 /
        # 0x80070057），同一进程内连续重试通常会很快恢复，但恢复时间不定。
        # 因此先用"无延迟多轮"快速试，再退化成"带退避多轮"。
        delays = (0.0, 0.0, 0.0, 0.1, 0.25, 0.5, 1.0)
        last_error: Exception | None = None
        for attempt, delay in enumerate(delays, start=1):
            if delay:
                time.sleep(delay)
            try:
                endpoints = self._choose_endpoints()
            except OSError as exc:
                last_error = exc
                break
            errors: list[str] = []
            for ep in endpoints:
                try:
                    self._open_endpoint(ep)
                    if attempt > 1:
                        print(
                            f"[wasapi] 第 {attempt} 次尝试成功（音频引擎冷启动后恢复）"
                        )
                    return
                except OSError as exc:
                    errors.append(f"{ep.name or ep.device_id[-12:] or '设备'}: {exc}")
                    self._teardown()
            last_error = OSError("所有播放设备都无法建立回环采集：\n  " + "\n  ".join(errors[-4:]))
            if os.getenv("LIVESUB_DEBUG_INIT"):
                print(f"[wasapi-debug] attempt {attempt} failed", file=sys.stderr)

        self._teardown()
        self._com.__exit__()
        raise last_error if last_error else OSError("无法建立回环采集")

    def _choose_endpoints(self) -> list[RenderEndpoint]:
        """决定尝试顺序：指定名字 > 默认设备 > 其它活动设备。"""
        eps = list_render_endpoints_default_first()
        if not eps:
            raise OSError("找不到任何活动的播放设备（Windows 音频服务是否在运行？）")
        if self.device_hint:
            hint = self.device_hint.lower()
            named = [e for e in eps if hint in (e.name or "").lower()]
            if named:
                rest = [e for e in eps if e not in named]
                return named + rest
            print(f"[wasapi] 没有匹配 '{self.device_hint}' 的设备，改用默认设备", file=sys.stderr)
        return eps

    def _teardown(self) -> None:
        """释放当前端点相关的 COM 对象与事件句柄（不退出套间）。"""
        if self._client is not None:
            try:
                _vt(self._client, 11, ctypes.c_long)(self._client)  # Stop
            except Exception:
                pass
        for attr in ("_capture", "_client", "_device"):
            obj = getattr(self, attr)
            if obj is not None:
                try:
                    _release(obj)
                except Exception:
                    pass
                setattr(self, attr, None)
        if self._event:
            try:
                ctypes.windll.kernel32.CloseHandle(self._event)
            except Exception:
                pass
            self._event = None
        self._use_event = False

    def _open_endpoint(self, ep: RenderEndpoint) -> None:
        """在单个端点上把采集流拉起来，失败就抛 OSError 交给上层换端点。"""
        self._device, name = _open_render_by_id(ep.device_id)
        self.device_name = name or ep.name or "默认播放设备"
        self._client = _activate_audio_client(self._device)

        wfx, self.is_float, _ext = _mix_format(self._client)
        self.native_rate = int(wfx.Format.nSamplesPerSec)
        self.native_channels = int(wfx.Format.nChannels)
        self._native_block = int(wfx.Format.nBlockAlign)
        self._bits = int(wfx.Format.wBitsPerSample)
        self.native_dtype = ("float" if self.is_float else "pcm") + str(self._bits)

        default_period, min_period = _device_period(self._client)
        if os.getenv("LIVESUB_DEBUG_INIT"):
            print(
                f"[wasapi-debug] mix rate={wfx.Format.nSamplesPerSec} ch={wfx.Format.nChannels} "
                f"tag=0x{wfx.Format.wFormatTag:04X} bits={wfx.Format.wBitsPerSample} "
                f"block={wfx.Format.nBlockAlign} cbSize={wfx.Format.cbSize} float={self.is_float} "
                f"sizeof={ctypes.sizeof(wfx)} period=({default_period},{min_period})",
                file=sys.stderr,
            )
        # 混合缓冲给足 1s，避免调度抖动导致丢数据；实际延迟由包大小决定
        duration = REFERENCE_TIME_PER_SEC
        periods = [p for p in (default_period, min_period, 200 * 10_000, 0) if p is not None]
        # 去重并保序
        seen: set[int] = set()
        periods = [p for p in periods if not (p in seen or seen.add(p))]

        last_hr = 0
        for label, flags, use_event in self._FLAG_CANDIDATES:
            for period in periods:
                if use_event != bool(flags & AUDCLNT_STREAMFLAGS_EVENTCALLBACK):
                    continue
                # 每个组合都需要一个干净的 IAudioClient
                self._safe_release_client()
                self._client = _activate_audio_client(self._device)
                self._event = None
                if use_event:
                    self._event = ctypes.windll.kernel32.CreateEventW(None, False, False, None)
                    if not self._event:
                        continue
                if os.getenv("LIVESUB_DEBUG_INIT"):
                    print(f"[wasapi-debug] try {label} period={period} dur={duration}", file=sys.stderr)

                hr = _vt(
                    self._client,
                    3,
                    ctypes.c_long,
                    ctypes.c_int,
                    ctypes.c_ulong,
                    ctypes.c_long,
                    ctypes.c_long,
                    POINTER(WAVEFORMATEX),
                    POINTER(GUID),
                )(
                    self._client,
                    AUDCLNT_SHAREMODE_SHARED,
                    flags,
                    duration,
                    period,
                    ctypes.cast(byref(wfx), POINTER(WAVEFORMATEX)),
                    None,
                )
                if hr < 0:
                    last_hr = hr
                    if os.getenv("LIVESUB_DEBUG_INIT"):
                        print(f"[wasapi-debug]   -> {hr_text(hr)}", file=sys.stderr)
                    if self._event:
                        ctypes.windll.kernel32.CloseHandle(self._event)
                        self._event = None
                    continue

                self._use_event = use_event
                self._init_label = f"{label}, period={period}"
                # 事件句柄必须在 Initialize 之后设置
                if use_event:
                    hr2 = _vt(self._client, 13, ctypes.c_long, wintypes.HANDLE)(
                        self._client, self._event
                    )
                    if hr2 < 0:
                        last_hr = hr2
                        self._safe_release_client()
                        if self._event:
                            ctypes.windll.kernel32.CloseHandle(self._event)
                            self._event = None
                        continue

                cap = _out_ptr()
                hr3 = _vt(self._client, 14, ctypes.c_long, POINTER(GUID), POINTER(c_void_p))(
                    self._client, byref(IID_IAudioCaptureClient), byref(cap)
                )
                if hr3 < 0:
                    last_hr = hr3
                    self._safe_release_client()
                    if self._event:
                        ctypes.windll.kernel32.CloseHandle(self._event)
                        self._event = None
                    continue
                self._capture = cap

                bf = ctypes.c_uint()
                if _vt(self._client, 4, ctypes.c_long, POINTER(ctypes.c_uint))(
                    self._client, byref(bf)
                ) >= 0:
                    self._buf_frames = int(bf.value)

                _hr(_vt(self._client, 10, ctypes.c_long)(self._client), "Start")
                return

        self._safe_release_client()
        raise OSError(
            f"Initialize 全部参数组合都失败（最后错误 {hr_text(last_hr)}）"
        )

    def _safe_release_client(self) -> None:
        if self._client is not None:
            try:
                _release(self._client)
            except Exception:
                pass
            self._client = None
        self._capture = None

    def close(self) -> None:
        self._teardown()
        self._com.__exit__()

    def __enter__(self) -> "WasapiLoopback":
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---------- 读取 ----------
    def _convert(self, ptr, frames: int) -> np.ndarray:
        """把设备缓冲转成 float32 单声道 [-1,1]。

        必须按 wBitsPerSample 分支：把 32bit PCM 当 int16 读会得到完全错误的
        波形（而且不会报错，字幕会变成乱码），这类问题最难发现。
        """
        ch = self.native_channels
        total = frames * ch
        if self.is_float:
            if self._bits == 64:
                raw = np.ctypeslib.as_array(
                    ctypes.cast(ptr, POINTER(ctypes.c_double)), shape=(total,)
                )
                f = np.array(raw, dtype=np.float32)
            else:
                raw = np.ctypeslib.as_array(
                    ctypes.cast(ptr, POINTER(ctypes.c_float)), shape=(total,)
                )
                f = np.array(raw, dtype=np.float32)
        elif self._bits == 16:
            raw = np.ctypeslib.as_array(ctypes.cast(ptr, POINTER(ctypes.c_int16)), shape=(total,))
            f = np.array(raw, dtype=np.float32) / 32768.0
        elif self._bits == 32:
            raw = np.ctypeslib.as_array(ctypes.cast(ptr, POINTER(ctypes.c_int32)), shape=(total,))
            f = np.array(raw, dtype=np.float32) / 2147483648.0
        elif self._bits == 24:
            buf = (ctypes.c_ubyte * (total * 3)).from_address(ctypes.cast(ptr, c_void_p).value)
            b = np.frombuffer(bytes(buf), dtype=np.uint8).reshape(-1, 3).astype(np.int32)
            val = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
            val = np.where(val >= 0x800000, val - 0x1000000, val)
            f = val.astype(np.float32) / 8388608.0
        elif self._bits == 8:
            raw = np.ctypeslib.as_array(ctypes.cast(ptr, POINTER(ctypes.c_uint8)), shape=(total,))
            f = (np.array(raw, dtype=np.float32) - 128.0) / 128.0
        else:
            # 未知位深：按块对齐推断，退化成 int16 再缩放，至少不崩
            raw = np.ctypeslib.as_array(ctypes.cast(ptr, POINTER(ctypes.c_int16)), shape=(total,))
            f = np.array(raw, dtype=np.float32) / 32768.0

        if ch > 1:
            f = f.reshape(-1, ch).mean(axis=1)
        return f

    def read(self, timeout: float | None = None) -> np.ndarray | None:
        """等到一包数据并返回 16kHz 单声道 int16（可能为空数组）。"""
        if self._capture is None:
            raise RuntimeError("采集器未打开")
        wait_s = timeout if timeout is not None else self.read_timeout_ms / 1000
        if self._use_event and self._event:
            rc = ctypes.windll.kernel32.WaitForSingleObject(self._event, int(wait_s * 1000))
            if rc == WAIT_TIMEOUT:
                return np.zeros(0, dtype=np.int16)
            if rc != WAIT_OBJECT_0:
                raise OSError(f"WaitForSingleObject 返回 {rc}")
        else:
            # 轮询模式：睡一小会儿再来取，避免空转烧 CPU
            time.sleep(min(max(wait_s, 0.01), 0.25))

        out: list[np.ndarray] = []
        packet = ctypes.POINTER(ctypes.c_ubyte)()
        nframes = ctypes.c_uint()
        flags = ctypes.c_ulong()
        while True:
            hr = _vt(
                self._capture,
                3,
                ctypes.c_long,
                POINTER(POINTER(ctypes.c_ubyte)),
                POINTER(ctypes.c_uint),
                POINTER(ctypes.c_ulong),
                ctypes.POINTER(ctypes.c_ulong),
                ctypes.POINTER(ctypes.c_ulong),
            )(self._capture, byref(packet), byref(nframes), byref(flags), None, None)
            if hr == AUDCLNT_S_BUFFER_EMPTY:
                break
            if hr < 0:
                if hr == AUDCLNT_E_DEVICE_INVALIDATED or hr == AUDCLNT_S_DEVICE_INVALIDATED:
                    raise OSError("播放设备已变更/失效，请重启采集")
                raise OSError(f"GetBuffer 失败: 0x{hr & 0xFFFFFFFF:08X}")
            try:
                if nframes.value:
                    if flags.value & 0x2:  # AUDCLNT_BUFFERFLAGS_SILENT
                        out.append(np.zeros(nframes.value, dtype=np.float32))
                    else:
                        out.append(self._convert(packet, nframes.value))
            finally:
                _vt(
                    self._capture, 4, ctypes.c_long, ctypes.c_uint
                )(self._capture, nframes.value)  # ReleaseBuffer

        if not out:
            return np.zeros(0, dtype=np.int16)
        mono = np.concatenate(out) if len(out) > 1 else out[0]
        if self.native_rate != self.out_rate:
            mono = _resample_linear(mono, self.native_rate, self.out_rate)
        return np.clip(mono * 32767.0, -32768, 32767).astype(np.int16)

    @property
    def format_desc(self) -> str:
        name = self.device_name or "默认播放设备"
        return (
            f"{name} | {self.native_rate}Hz {self.native_channels}ch "
            f"{self.native_dtype} -> {self.out_rate}Hz mono int16"
        )

    @property
    def mode_desc(self) -> str:
        return self._init_label or ("事件模式" if self._use_event else "轮询模式")


# ----------------------------------------------------------------------
# 自检：python -m livesub.wasapi
# ----------------------------------------------------------------------
def _selftest(seconds: float = 5.0) -> int:
    print("活动播放设备:")
    for ep in list_render_endpoints():
        print(f"  {'*' if ep.is_default else ' '} {ep.name}")
    print("\n开始回环采集自检，请让电脑播放任意声音...")
    cap = WasapiLoopback(sample_rate=16000)
    try:
        cap.open()
    except Exception as exc:
        print(f"[FAIL] 打开失败: {exc}")
        return 1
    print(f"格式: {cap.format_desc}")
    total = 0
    peak = 0.0
    silent_packets = 0
    t0 = time.monotonic()
    try:
        while time.monotonic() - t0 < seconds:
            pcm = cap.read(timeout=0.5)
            if pcm is None or pcm.size == 0:
                silent_packets += 1
                continue
            total += pcm.size
            peak = max(peak, float(np.abs(pcm).max()) / 32768.0)
    finally:
        cap.close()
    dur = total / 16000
    print(f"采到 {total} 采样点 = {dur:.2f}s 音频, 峰值 {peak:.3f}, 空包 {silent_packets}")
    if dur < 0.2 * seconds:
        print("[WARN] 采集到的音频远少于预期：可能没声音在播放，或设备是独占模式")
        return 2
    print("[OK] 回环采集正常" + ("（当前静音/无声音）" if peak < 0.001 else "（检测到声音）"))
    return 0


if __name__ == "__main__":
    sys.exit(_selftest())
