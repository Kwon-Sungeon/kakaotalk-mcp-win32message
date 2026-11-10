"""Minimal ctypes bindings for the Win32 calls this tool uses.

Only public, documented user32/kernel32/gdi32 APIs are used. Nothing here
injects code into, hooks, or reads the memory of the KakaoTalk process.
"""

import ctypes
import sys
import time
from ctypes import wintypes

if sys.platform != "win32":  # pragma: no cover - import guard for other platforms
    raise ImportError("kakaotalk_win requires Windows")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

HWND = wintypes.HWND
UINT = wintypes.UINT
BOOL = wintypes.BOOL
DWORD = wintypes.DWORD
WPARAM = wintypes.WPARAM
LPARAM = wintypes.LPARAM
LRESULT = ctypes.c_ssize_t
HANDLE = wintypes.HANDLE

WNDENUMPROC = ctypes.WINFUNCTYPE(BOOL, HWND, LPARAM)

# Window messages
WM_SETTEXT = 0x000C
WM_GETTEXT = 0x000D
WM_GETTEXTLENGTH = 0x000E
WM_CLOSE = 0x0010
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_CHAR = 0x0102
WM_VSCROLL = 0x0115
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_MOUSEWHEEL = 0x020A
MK_LBUTTON = 0x0001
EM_SETSEL = 0x00B1
EM_REPLACESEL = 0x00C2
SMTO_ABORTIFHUNG = 0x0002

VK_RETURN = 0x0D
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_ESCAPE = 0x1B
VK_PRIOR = 0x21
VK_NEXT = 0x22
VK_END = 0x23
VK_HOME = 0x24
VK_A = 0x41
VK_C = 0x43
VK_V = 0x56

SW_SHOWNOACTIVATE = 4
SW_SHOWMINNOACTIVE = 7
GW_HWNDPREV = 3
PW_RENDERFULLCONTENT = 0x00000002

_SIGNATURES = {
    (user32, "EnumWindows"): (BOOL, [WNDENUMPROC, LPARAM]),
    (user32, "EnumChildWindows"): (BOOL, [HWND, WNDENUMPROC, LPARAM]),
    (user32, "GetWindowThreadProcessId"): (DWORD, [HWND, ctypes.POINTER(DWORD)]),
    (user32, "GetClassNameW"): (ctypes.c_int, [HWND, wintypes.LPWSTR, ctypes.c_int]),
    (user32, "GetWindowTextW"): (ctypes.c_int, [HWND, wintypes.LPWSTR, ctypes.c_int]),
    (user32, "GetWindowTextLengthW"): (ctypes.c_int, [HWND]),
    (user32, "IsWindow"): (BOOL, [HWND]),
    (user32, "IsWindowVisible"): (BOOL, [HWND]),
    (user32, "IsWindowEnabled"): (BOOL, [HWND]),
    (user32, "IsIconic"): (BOOL, [HWND]),
    (user32, "GetParent"): (HWND, [HWND]),
    (user32, "GetAncestor"): (HWND, [HWND, UINT]),
    (user32, "GetWindow"): (HWND, [HWND, UINT]),
    (user32, "GetForegroundWindow"): (HWND, []),
    (user32, "GetWindowRect"): (BOOL, [HWND, ctypes.POINTER(wintypes.RECT)]),
    (user32, "GetClientRect"): (BOOL, [HWND, ctypes.POINTER(wintypes.RECT)]),
    (user32, "ShowWindow"): (BOOL, [HWND, ctypes.c_int]),
    (user32, "PostMessageW"): (BOOL, [HWND, UINT, WPARAM, LPARAM]),
    (user32, "SendMessageTimeoutW"): (LRESULT, [HWND, UINT, WPARAM, LPARAM, UINT, UINT,
                                                ctypes.POINTER(ctypes.c_size_t)]),
    (user32, "AttachThreadInput"): (BOOL, [DWORD, DWORD, BOOL]),
    (user32, "GetKeyboardState"): (BOOL, [ctypes.POINTER(ctypes.c_ubyte)]),
    (user32, "SetKeyboardState"): (BOOL, [ctypes.POINTER(ctypes.c_ubyte)]),
    (user32, "MapVirtualKeyW"): (UINT, [UINT, UINT]),
    (user32, "GetClipboardSequenceNumber"): (DWORD, []),
    (user32, "OpenClipboard"): (BOOL, [HWND]),
    (user32, "CloseClipboard"): (BOOL, []),
    (user32, "EmptyClipboard"): (BOOL, []),
    (user32, "EnumClipboardFormats"): (UINT, [UINT]),
    (user32, "GetClipboardData"): (HANDLE, [UINT]),
    (user32, "SetClipboardData"): (HANDLE, [UINT, HANDLE]),
    (user32, "GetClipboardFormatNameW"): (ctypes.c_int, [UINT, wintypes.LPWSTR, ctypes.c_int]),
    (user32, "RegisterClipboardFormatW"): (UINT, [wintypes.LPCWSTR]),
    (user32, "GetClipboardOwner"): (HWND, []),
    (user32, "GetOpenClipboardWindow"): (HWND, []),
    (user32, "GetDC"): (wintypes.HDC, [HWND]),
    (user32, "ReleaseDC"): (ctypes.c_int, [HWND, wintypes.HDC]),
    (user32, "PrintWindow"): (BOOL, [HWND, wintypes.HDC, UINT]),
    (user32, "SetProcessDpiAwarenessContext"): (BOOL, [HANDLE]),
    (user32, "GetWindowDpiAwarenessContext"): (HANDLE, [HWND]),
    (user32, "GetDpiForWindow"): (UINT, [HWND]),
    (user32, "SetThreadDpiAwarenessContext"): (HANDLE, [HANDLE]),
    (kernel32, "GetCurrentThreadId"): (DWORD, []),
    (kernel32, "GlobalAlloc"): (HANDLE, [UINT, ctypes.c_size_t]),
    (kernel32, "GlobalLock"): (ctypes.c_void_p, [HANDLE]),
    (kernel32, "GlobalUnlock"): (BOOL, [HANDLE]),
    (kernel32, "GlobalSize"): (ctypes.c_size_t, [HANDLE]),
    (kernel32, "GlobalFree"): (HANDLE, [HANDLE]),
    (kernel32, "OpenProcess"): (HANDLE, [DWORD, BOOL, DWORD]),
    (kernel32, "CloseHandle"): (BOOL, [HANDLE]),
    (kernel32, "QueryFullProcessImageNameW"): (BOOL, [HANDLE, DWORD, wintypes.LPWSTR,
                                                      ctypes.POINTER(DWORD)]),
    (gdi32, "CreateCompatibleDC"): (wintypes.HDC, [wintypes.HDC]),
    (gdi32, "CreateCompatibleBitmap"): (wintypes.HBITMAP, [wintypes.HDC, ctypes.c_int, ctypes.c_int]),
    (gdi32, "SelectObject"): (wintypes.HGDIOBJ, [wintypes.HDC, wintypes.HGDIOBJ]),
    (gdi32, "DeleteObject"): (BOOL, [wintypes.HGDIOBJ]),
    (gdi32, "DeleteDC"): (BOOL, [wintypes.HDC]),
    (gdi32, "GetDIBits"): (ctypes.c_int, [wintypes.HDC, wintypes.HBITMAP, UINT, UINT, ctypes.c_void_p,
                                          ctypes.c_void_p, UINT]),
    (gdi32, "GetEnhMetaFileBits"): (UINT, [HANDLE, UINT, ctypes.c_void_p]),
    (gdi32, "SetEnhMetaFileBits"): (HANDLE, [UINT, ctypes.c_void_p]),
}
for (_dll, _name), (_restype, _argtypes) in _SIGNATURES.items():
    try:
        _function = getattr(_dll, _name)
    except AttributeError:  # pragma: no cover - very old Windows
        continue
    _function.restype = _restype
    _function.argtypes = _argtypes


def handle(value):
    """Normalize HWND values (ctypes may return None for NULL)."""
    return int(value or 0)


def class_name(hwnd):
    buffer = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buffer, 256)
    return buffer.value


def window_text(hwnd):
    """Title of a top-level window or caption of a child (no cross-process text query)."""
    length = user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(max(1, length + 1))
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def process_id(hwnd):
    pid = DWORD()
    thread = user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value, thread


def process_image(pid):
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    process = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not process:
        return ""
    try:
        size = DWORD(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(size)):
            return buffer.value
        return ""
    finally:
        kernel32.CloseHandle(process)


def top_windows():
    found = []

    @WNDENUMPROC
    def collect(hwnd, _):
        found.append(handle(hwnd))
        return True

    user32.EnumWindows(collect, 0)
    return found


def child_windows(parent):
    found = []

    @WNDENUMPROC
    def collect(hwnd, _):
        found.append(handle(hwnd))
        return True

    user32.EnumChildWindows(parent, collect, 0)
    return found


def send_message(hwnd, message, wparam=0, lparam=0, timeout_ms=2000):
    """SendMessageTimeout so a hung KakaoTalk can never hang this process."""
    result = ctypes.c_size_t()
    ok = user32.SendMessageTimeoutW(hwnd, message, wparam, lparam, SMTO_ABORTIFHUNG, timeout_ms,
                                    ctypes.byref(result))
    if not ok:
        raise TimeoutError("KakaoTalk did not answer a window message in time")
    return result.value


def control_text_length(hwnd):
    return send_message(hwnd, WM_GETTEXTLENGTH)


def control_text(hwnd, limit=65536):
    length = min(control_text_length(hwnd), limit)
    buffer = ctypes.create_unicode_buffer(length + 1)
    send_message(hwnd, WM_GETTEXT, length + 1, ctypes.addressof(buffer))
    return buffer.value


def set_control_text(hwnd, text):
    buffer = ctypes.create_unicode_buffer(text)
    return send_message(hwnd, WM_SETTEXT, 0, ctypes.addressof(buffer))


def replace_all_text(hwnd, text):
    """Replace the whole content the way typing does (EM_REPLACESEL, undoable).

    KakaoTalk PC (2026-11) shows text set by WM_SETTEXT but does not send it;
    text inserted through the selection-replace path is treated as input.
    """
    set_control_text(hwnd, "")
    send_message(hwnd, EM_SETSEL, 0, -1)
    buffer = ctypes.create_unicode_buffer(text)
    return send_message(hwnd, EM_REPLACESEL, 1, ctypes.addressof(buffer))


def post_plain_key(hwnd, vk):
    """WM_KEYDOWN/WM_KEYUP with lParam 0 (the variant observed to send in KakaoTalk PC)."""
    post(hwnd, WM_KEYDOWN, vk, 0)
    time.sleep(0.02)
    post(hwnd, WM_KEYUP, vk, 0)


def key_lparam(vk, up=False):
    scan = user32.MapVirtualKeyW(vk, 0) & 0xFF
    value = 1 | (scan << 16)
    if up:
        value |= (1 << 30) | (1 << 31)
    return value


def post(hwnd, message, wparam=0, lparam=0):
    if not user32.PostMessageW(hwnd, message, wparam, lparam):
        raise OSError(ctypes.get_last_error(), "PostMessage failed")


def post_key(hwnd, vk):
    post(hwnd, WM_KEYDOWN, vk, key_lparam(vk))
    post(hwnd, WM_KEYUP, vk, key_lparam(vk, up=True))


def post_click(hwnd, x, y):
    """Left click at client point (x, y) delivered as window messages.

    The real mouse cursor does not move and no window is activated. Coordinates
    are in the target window's own DPI space (see ``window_dpi``).
    """
    point = (x & 0xFFFF) | ((y & 0xFFFF) << 16)
    post(hwnd, WM_MOUSEMOVE, 0, point)
    time.sleep(0.05)
    post(hwnd, WM_LBUTTONDOWN, MK_LBUTTON, point)
    time.sleep(0.05)
    post(hwnd, WM_LBUTTONUP, 0, point)


def window_scale(hwnd):
    """The app's drawing scale relative to 144 DPI (150%), the scale the layout constants were measured at."""
    try:
        dpi = user32.GetDpiForWindow(hwnd)
    except AttributeError:  # pragma: no cover
        dpi = 0
    return (dpi or 96) / 144


class modifier_state:
    """Make GetKeyState() in the target's input queue report modifiers as held.

    PostMessage cannot carry modifier state, and KakaoTalk checks GetKeyState
    for Ctrl+A / Ctrl+C. Attaching our thread's input state to the target
    thread and setting the key state affects only those two queues; no global
    keyboard input is synthesized and the target window is not activated.
    The previous state is always restored.
    """

    def __init__(self, hwnd, modifiers):
        self.hwnd = hwnd
        self.modifiers = tuple(modifiers)
        self.attached = False
        self.previous = None

    def __enter__(self):
        _, target_thread = process_id(self.hwnd)
        self.me = kernel32.GetCurrentThreadId()
        self.target = target_thread
        self.attached = bool(user32.AttachThreadInput(self.me, self.target, True))
        if not self.attached:
            raise OSError(ctypes.get_last_error(), "AttachThreadInput failed")
        state = (ctypes.c_ubyte * 256)()
        user32.GetKeyboardState(state)
        self.previous = bytes(state)
        for vk in self.modifiers:
            state[vk] |= 0x80
        user32.SetKeyboardState(state)
        return self

    def __exit__(self, *_):
        try:
            if self.previous is not None:
                restore = (ctypes.c_ubyte * 256).from_buffer_copy(self.previous)
                for vk in self.modifiers:
                    restore[vk] &= 0x7F
                user32.SetKeyboardState(restore)
        finally:
            if self.attached:
                user32.AttachThreadInput(self.me, self.target, False)
        return False


def post_chord(hwnd, modifiers, keys, until=None, timeout=1.5, settle=0.03):
    """Post modifier+key chords while the target's input queue sees the modifiers held.

    Posted messages are processed asynchronously, so the modifier state is held
    until ``until()`` reports the expected effect (or ``timeout`` elapses).
    Returns whether the effect was observed (always True without ``until``).
    """
    with modifier_state(hwnd, modifiers):
        for vk in keys:
            post_key(hwnd, vk)
            time.sleep(settle)
        if until is None:
            time.sleep(0.15)
            return True
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if until():
                return True
            time.sleep(0.01)
        return bool(until())


def rect(hwnd):
    value = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(value))
    return value.left, value.top, value.right, value.bottom


def file_version(path):
    """Product version string of an executable (VS_FIXEDFILEINFO), or None."""
    version = ctypes.WinDLL("version")
    version.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(DWORD)]
    version.GetFileVersionInfoSizeW.restype = DWORD
    version.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, DWORD, DWORD, ctypes.c_void_p]
    version.GetFileVersionInfoW.restype = BOOL
    version.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p),
                                       ctypes.POINTER(UINT)]
    version.VerQueryValueW.restype = BOOL
    size = version.GetFileVersionInfoSizeW(path, None)
    if not size:
        return None
    data = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(path, 0, size, data):
        return None
    pointer, length = ctypes.c_void_p(), UINT()
    if not version.VerQueryValueW(data, "\\", ctypes.byref(pointer), ctypes.byref(length)) or length.value < 52:
        return None
    fields = (ctypes.c_uint32 * 13).from_address(pointer.value)
    ms, ls = fields[4], fields[5]  # dwProductVersionMS / LS
    return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"


def remote_session():
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    return bool(user32.GetSystemMetrics(0x1000))  # SM_REMOTESESSION


class window_dpi:
    """Measure, capture and point in the target window's own coordinates.

    KakaoTalk is system-DPI aware. On a monitor whose scale differs from the
    system scale, Windows stretches its output, so the on-screen rect is not
    the size the app draws at. Inside this block our thread uses the app's DPI
    context: GetWindowRect, PrintWindow and mouse-message coordinates all match
    what the app itself draws.
    """

    def __init__(self, hwnd):
        self.hwnd = hwnd
        self.previous = None

    def __enter__(self):
        try:
            context = user32.GetWindowDpiAwarenessContext(self.hwnd)
            if context:
                self.previous = user32.SetThreadDpiAwarenessContext(context)
        except AttributeError:  # pragma: no cover - Windows before 10 1607
            pass
        return self

    def __exit__(self, *_):
        if self.previous:
            user32.SetThreadDpiAwarenessContext(self.previous)
        return False


def enable_dpi_awareness():
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
    except Exception:  # pragma: no cover
        pass
