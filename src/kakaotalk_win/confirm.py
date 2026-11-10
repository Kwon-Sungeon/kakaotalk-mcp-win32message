"""Native Yes/No confirmation shown to the human before a message is sent.

The dialog belongs to this process, not to the AI agent, so an agent cannot
answer it. "No" is the default button; no answer within the timeout counts as No.
"""

import ctypes
import os
import threading
import time

from . import win32 as w

MB_YESNO = 0x00000004
MB_ICONQUESTION = 0x00000020
MB_DEFBUTTON2 = 0x00000100
MB_SYSTEMMODAL = 0x00001000
MB_SETFOREGROUND = 0x00010000
MB_TOPMOST = 0x00040000
IDYES = 6
IDNO = 7
BM_CLICK = 0x00F5
CAPTION = "kakaotalk-win 전송 확인"

w.user32.MessageBoxW.argtypes = [w.HWND, w.wintypes.LPCWSTR, w.wintypes.LPCWSTR, w.UINT]
w.user32.MessageBoxW.restype = ctypes.c_int
w.user32.GetDlgItem.argtypes = [w.HWND, ctypes.c_int]
w.user32.GetDlgItem.restype = w.HWND


def _preview(text, limit=400):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text if len(text) <= limit else text[:limit] + f"\n… (총 {len(text)}자)"


def ask_send(room, text, timeout=120, notes=()):
    """Return True only if the human pressed Yes."""
    warning = "".join(f"\n⚠ {note}" for note in notes)
    message = (f"아래 메시지를 카카오톡으로 보낼까요?\n\n받는 방: {room}\n글자 수: {len(text)}{warning}\n\n"
               f"{_preview(text)}\n\n[예]를 누르면 즉시 전송됩니다. {timeout}초 안에 답하지 않으면 취소됩니다.")
    result = {}

    def show():
        result["answer"] = w.user32.MessageBoxW(None, message, CAPTION, MB_YESNO | MB_ICONQUESTION | MB_DEFBUTTON2 |
                                                MB_SETFOREGROUND | MB_TOPMOST | MB_SYSTEMMODAL)

    thread = threading.Thread(target=show, name="kakaotalk-win-confirm", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        _press_no()
        thread.join(5)
        return False
    return result.get("answer") == IDYES


def _press_no():
    me = os.getpid()
    for hwnd in w.top_windows():
        if w.process_id(hwnd)[0] == me and w.class_name(hwnd) == "#32770" and w.window_text(hwnd) == CAPTION:
            button = w.handle(w.user32.GetDlgItem(hwnd, IDNO))
            if button:
                w.user32.PostMessageW(button, BM_CLICK, 0, 0)
    time.sleep(0.2)
