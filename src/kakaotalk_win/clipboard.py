"""Clipboard capture that never loses the user's clipboard and never races KakaoTalk.

Reading a chat on Windows means asking the official app to copy its own list
(Ctrl+A, Ctrl+C) and reading the text it puts on the clipboard. The clipboard
is shared with every other program, so each capture:

1. snapshots every clipboard format the user currently has (text, HTML, images,
   files, application-private formats) without leaving the clipboard open,
2. waits for KakaoTalk's copy by watching GetClipboardSequenceNumber, which
   needs no clipboard lock (polling with OpenClipboard is what collides with the
   app while it is writing),
3. accepts the data only if the clipboard owner is the KakaoTalk process,
4. restores the snapshot only if nobody else changed the clipboard after our
   read — a newer copy by the user always wins and is never overwritten.

All of this runs under one process-wide lock so captures never overlap.
"""

import ctypes
import threading
import time
import winreg

from . import win32 as w

CF_TEXT = 1
CF_BITMAP = 2
CF_METAFILEPICT = 3
CF_OEMTEXT = 7
CF_DIB = 8
CF_PALETTE = 9
CF_UNICODETEXT = 13
CF_ENHMETAFILE = 14
CF_HDROP = 15
CF_LOCALE = 16
CF_DIBV5 = 17
CF_OWNERDISPLAY = 0x0080
CF_DSPTEXT = 0x0081
CF_DSPBITMAP = 0x0082
CF_DSPMETAFILEPICT = 0x0083
CF_DSPENHMETAFILE = 0x008E
GMEM_MOVEABLE = 0x0002

# Formats whose handle is not an HGLOBAL we can copy byte-for-byte, or that
# Windows synthesizes from another format that we do keep.
_HANDLE_FORMATS = {CF_BITMAP, CF_METAFILEPICT, CF_PALETTE, CF_OWNERDISPLAY,
                   CF_DSPBITMAP, CF_DSPMETAFILEPICT, CF_DSPENHMETAFILE}
_SYNTHESIZED_FROM_UNICODE = {CF_TEXT, CF_OEMTEXT}

_lock = threading.RLock()


class ClipboardBusy(RuntimeError):
    """Another program kept the clipboard open."""


class ClipboardConflict(RuntimeError):
    """The clipboard changed for a reason other than our capture."""


class _OwnerThread(threading.Thread):
    """Hidden message-only clipboard owner window with its own message loop.

    After we restore the user's data we are the clipboard owner. The next
    EmptyClipboard() by any program (KakaoTalk's own copy included) sends
    WM_DESTROYCLIPBOARD to the owner synchronously. If the owner's thread does
    not pump messages, that program stalls with the clipboard open and only
    finishes its copy after our process exits — which once left chat text on
    the user's clipboard. A dedicated pumping thread prevents that.
    """

    def __init__(self):
        super().__init__(name="kakaotalk-win-clipboard-owner", daemon=True)
        self.ready = threading.Event()
        self.hwnd = None
        self.error = None

    def run(self):
        user32 = w.user32
        create = user32.CreateWindowExW
        create.restype = w.HWND
        create.argtypes = [w.DWORD, w.wintypes.LPCWSTR, w.wintypes.LPCWSTR, w.DWORD, ctypes.c_int,
                           ctypes.c_int, ctypes.c_int, ctypes.c_int, w.HWND, w.wintypes.HMENU,
                           w.wintypes.HINSTANCE, ctypes.c_void_p]
        user32.GetMessageW.argtypes = [ctypes.POINTER(w.wintypes.MSG), w.HWND, w.UINT, w.UINT]
        user32.GetMessageW.restype = ctypes.c_int
        user32.TranslateMessage.argtypes = [ctypes.POINTER(w.wintypes.MSG)]
        user32.DispatchMessageW.argtypes = [ctypes.POINTER(w.wintypes.MSG)]
        user32.DispatchMessageW.restype = w.LRESULT
        hwnd = create(0, "STATIC", "kakaotalk-win clipboard", 0, 0, 0, 0, 0, w.HWND(-3), None, None, None)
        if not hwnd:
            self.error = OSError(ctypes.get_last_error(), "Could not create clipboard owner window")
            self.ready.set()
            return
        self.hwnd = hwnd
        self.ready.set()
        message = w.wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))


_owner_lock = threading.Lock()
_owner = None


def _owner_window():
    global _owner
    with _owner_lock:
        if _owner is None or not _owner.is_alive():
            _owner = _OwnerThread()
            _owner.start()
            _owner.ready.wait(5)
        if _owner.error or not _owner.hwnd:
            raise _owner.error or OSError("Clipboard owner window unavailable")
        return _owner.hwnd


def _holder_pid():
    holder = w.handle(w.user32.GetOpenClipboardWindow())
    return w.process_id(holder)[0] if holder else 0


class _Open:
    def __init__(self, timeout=1.0):
        self.timeout = timeout

    def __enter__(self):
        owner = _owner_window()
        deadline = time.monotonic() + self.timeout
        delay = 0.005
        while not w.user32.OpenClipboard(owner):
            if time.monotonic() >= deadline:
                raise ClipboardBusy("Another program is holding the clipboard open")
            time.sleep(delay)
            delay = min(delay * 2, 0.05)
        return self

    def __exit__(self, *_):
        w.user32.CloseClipboard()
        return False


def sequence():
    return w.user32.GetClipboardSequenceNumber()


def _formats():
    found, current = [], 0
    while True:
        current = w.user32.EnumClipboardFormats(current)
        if not current:
            return found
        found.append(current)


def _global_bytes(handle):
    size = w.kernel32.GlobalSize(handle)
    pointer = w.kernel32.GlobalLock(handle)
    if not pointer:
        return None
    try:
        return ctypes.string_at(pointer, size)
    finally:
        w.kernel32.GlobalUnlock(handle)


def _global_from_bytes(data):
    handle = w.kernel32.GlobalAlloc(GMEM_MOVEABLE, max(1, len(data)))
    if not handle:
        raise MemoryError("GlobalAlloc failed")
    pointer = w.kernel32.GlobalLock(handle)
    ctypes.memmove(pointer, data, len(data))
    w.kernel32.GlobalUnlock(handle)
    return handle


def format_name(fmt):
    buffer = ctypes.create_unicode_buffer(256)
    if w.user32.GetClipboardFormatNameW(fmt, buffer, 256):
        return buffer.value
    return {CF_TEXT: "CF_TEXT", CF_BITMAP: "CF_BITMAP", CF_UNICODETEXT: "CF_UNICODETEXT",
            CF_DIB: "CF_DIB", CF_DIBV5: "CF_DIBV5", CF_HDROP: "CF_HDROP", CF_LOCALE: "CF_LOCALE",
            CF_OEMTEXT: "CF_OEMTEXT", CF_ENHMETAFILE: "CF_ENHMETAFILE"}.get(fmt, f"format_{fmt}")


class Snapshot:
    def __init__(self, items, skipped, sequence_number):
        self.items = items          # list[(format, kind, bytes)]
        self.skipped = skipped      # names of formats that cannot be preserved
        self.sequence = sequence_number


def snapshot():
    """Copy every preservable clipboard format into process memory."""
    with _Open():
        before = sequence()
        formats = _formats()
        present = set(formats)
        items, skipped = [], []
        for fmt in formats:
            if fmt in _SYNTHESIZED_FROM_UNICODE and CF_UNICODETEXT in present:
                continue
            if fmt == CF_BITMAP and (CF_DIB in present or CF_DIBV5 in present):
                continue  # Windows re-synthesizes the bitmap from the DIB.
            if fmt == CF_METAFILEPICT and CF_ENHMETAFILE in present:
                continue
            if fmt in _HANDLE_FORMATS or 0x0200 <= fmt <= 0x03FF:
                skipped.append(format_name(fmt))
                continue
            handle = w.user32.GetClipboardData(fmt)
            if not handle:
                skipped.append(format_name(fmt))
                continue
            if fmt == CF_ENHMETAFILE:
                size = w.gdi32.GetEnhMetaFileBits(handle, 0, None)
                buffer = ctypes.create_string_buffer(size)
                w.gdi32.GetEnhMetaFileBits(handle, size, buffer)
                items.append((fmt, "emf", buffer.raw))
                continue
            data = _global_bytes(handle)
            if data is None:
                skipped.append(format_name(fmt))
                continue
            items.append((fmt, "global", data))
        return Snapshot(items, skipped, before)


def _write(items):
    """Put the items on the (open, emptied) clipboard; returns the names of formats that could not be set."""
    failed = []
    for fmt, kind, data in items:
        if kind == "emf":
            handle = w.gdi32.SetEnhMetaFileBits(len(data), data)
        else:
            handle = _global_from_bytes(data)
        if not w.user32.SetClipboardData(fmt, handle):
            failed.append(format_name(fmt))
            if kind == "global":
                w.kernel32.GlobalFree(handle)
    return failed


def restore(saved, expected_sequence):
    """Put the snapshot back unless someone else changed the clipboard since our read."""
    with _Open():
        if sequence() != expected_sequence:
            return {"restored": False, "reason": "clipboard_changed_by_another_program"}
        if not w.user32.EmptyClipboard():
            return {"restored": False, "reason": "clipboard_not_written"}
        failed = _write(saved.items)
    if failed and len(failed) == len(saved.items):
        return {"restored": False, "reason": "clipboard_not_written", "failedFormats": failed}
    result = {"restored": True, "formats": len(saved.items), "unpreservedFormats": saved.skipped}
    if failed:
        result["failedFormats"] = failed
    return result


def owner_process():
    owner = w.handle(w.user32.GetClipboardOwner())
    return w.process_id(owner)[0] if owner else 0


def read_unicode():
    handle = w.user32.GetClipboardData(CF_UNICODETEXT)
    if not handle:
        return None
    data = _global_bytes(handle)
    if data is None:
        return None
    return data.decode("utf-16-le", errors="replace").split("\x00", 1)[0]


def capture_text(trigger, expected_pid, timeout=2.0):
    """Run ``trigger(changed)`` and return the text KakaoTalk put on the clipboard.

    ``trigger`` receives a ``changed()`` callable it can poll while it holds
    modifier keys. Returns ``(text, info)``; raises ClipboardConflict when the
    clipboard was changed by anything other than KakaoTalk during the capture.
    """
    with _lock:
        saved = snapshot()
        baseline = sequence()
        if baseline != saved.sequence:
            raise ClipboardConflict("Clipboard changed while it was being saved")

        def changed():
            return sequence() != baseline

        started = time.monotonic()
        try:
            trigger(changed)
            deadline = time.monotonic() + timeout
            while not changed() and time.monotonic() < deadline:
                time.sleep(0.01)
        except BaseException:
            _restore_if_ours(saved, expected_pid)
            raise
        if not changed():
            # Never return while KakaoTalk is mid-copy: a late write would leave chat
            # text on the user's clipboard after we gave up.
            settle = time.monotonic() + 5
            while time.monotonic() < settle and not changed() and _holder_pid() == expected_pid:
                time.sleep(0.02)
            if changed():
                _restore_if_ours(saved, expected_pid)
                return None, {"copied": False, "restore": {"restored": True, "reason": "late_copy_reverted"}}
            return None, {"copied": False, "restore": {"restored": True, "reason": "clipboard_untouched"}}
        try:
            # KakaoTalk may still hold the clipboard open while it writes; _Open retries.
            with _Open(timeout=1.5):
                after = sequence()
                owner = owner_process()
                text = read_unicode() if owner == expected_pid else None
            if owner == expected_pid:
                restored = restore(saved, after)
        except BaseException:
            _restore_if_ours(saved, expected_pid)  # chat text must not stay on the user's clipboard
            raise
        if owner != expected_pid:
            # Somebody else copied in the meantime: their data wins, ours is discarded.
            raise ClipboardConflict("Clipboard was changed by another program during the capture")
        return text, {"copied": text is not None, "restore": restored,
                      "clipboardMs": round((time.monotonic() - started) * 1000, 1)}


def _restore_if_ours(saved, expected_pid):
    try:
        with _Open(timeout=1.0):
            current = sequence()
            ours = owner_process() == expected_pid
        if ours:
            restore(saved, current)
    except Exception:  # pragma: no cover - best effort while propagating another error
        pass


def history_settings():
    """Report Windows clipboard history / cloud sync, which would keep copied chat text."""
    values = {}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Clipboard") as key:
            for name in ("EnableClipboardHistory", "EnableCloudClipboard", "CloudClipboardAutomaticUpload"):
                try:
                    values[name] = winreg.QueryValueEx(key, name)[0]
                except OSError:
                    pass
    except OSError:
        pass
    return {"historyEnabled": values.get("EnableClipboardHistory") == 1,
            "cloudSyncEnabled": values.get("EnableCloudClipboard") == 1,
            "cloudAutoUpload": values.get("CloudClipboardAutomaticUpload") == 1}
