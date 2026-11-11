"""Creating a chat room and inviting people, through the app's own "대화상대 선택" dialog.

    new chat      Ctrl+N on the room list            -> picker
    invite        chat window "≡" menu -> "초대"      -> picker

    picker (EVA_Window_Dblclk, one Edit = name search)
    ├─ type a name into the Edit (WM_SETTEXT), the list shows the matching friends
    ├─ click the row whose name is exactly that name (window messages; the mouse stays put)
    └─ click "확인"; a group may ask for its name in a second dialog, confirmed the same way

Rows, buttons and menu entries are owner-drawn, so they are located by OCR of a
capture of the dialog. A friend is picked only when exactly one row shows exactly
the given name; otherwise the dialog is cancelled and nothing is created.
"""

import time

from . import capture as cap
from . import checks as check
from . import safety
from . import ui
from . import win32 as w
from .operations import _restore_visibility as _hide, _show_without_activation as _show

SOURCE = "kakaotalk_pc_win32"
VK_N = 0x4E
MENU_CLASS = "EVA_Menu"
CONFIRM_LABELS = ("확인", "OK", "Confirm")
INVITE_LABELS = ("초대", "Invite")
MENU_BUTTON = (32, 87)  # the "≡" button: pixels from the right and top edge of a chat window at 144 DPI
MOST_PEOPLE = 20


def _blocked(reason, **extra):
    return {"status": "blocked", "source": SOURCE, "reason": reason, **extra}


def names(values):
    if not isinstance(values, list) or not values or len(values) > MOST_PEOPLE:
        raise ValueError(f"names: give 1-{MOST_PEOPLE} friend names exactly as KakaoTalk shows them")
    for value in values:
        check.text(value, "names[]", 100)
    if len(set(values)) < len(values):
        raise ValueError("names: each name may appear only once")
    return values


# ---------------------------------------------------------------------- looking and clicking

def _lines(hwnd):
    """OCR lines of a window as it is drawn now, in the window's own pixel coordinates."""
    path = cap.save_png(cap.grab(hwnd))
    try:
        found = cap.ocr(path, zoom=1 if w.window_scale(hwnd) >= 0.9 else 2)
    finally:
        cap.discard([path])
    return found.get("lines", []) if found.get("status") == "ok" else None


def _click(window, x, y):
    """Click the window point (x, y): the message goes to the innermost visible child under it."""
    with w.window_dpi(window):
        left, top, _, _ = w.rect(window)
        target, area = window, None
        for child in w.child_windows(window):
            if not w.user32.IsWindowVisible(child):
                continue
            box = w.rect(child)
            size = (box[2] - box[0]) * (box[3] - box[1])
            if box[0] <= left + x < box[2] and box[1] <= top + y < box[3] and (area is None or size < area):
                target, area = child, size
        origin = w.rect(target)
        w.post_click(target, left + x - origin[0], top + y - origin[1])


def _same(shown, name):
    """OCR puts spaces inside some names; compare without them."""
    return shown.replace(" ", "") == name.replace(" ", "")


def _center(line):
    return (line["left"] + line["right"]) // 2, (line["top"] + line["bottom"]) // 2


def _press(window, labels):
    """Click the lowest line whose text is one of `labels` (a button). False when none is drawn."""
    wanted = {label.casefold() for label in labels}
    hits = [line for line in _lines(window) or [] if line["text"].replace(" ", "").casefold() in wanted]
    if not hits:
        return False
    _click(window, *_center(max(hits, key=lambda line: line["top"])))
    return True


def _new_window(pid, before, classes, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for hwnd in w.top_windows():
            if hwnd not in before and w.process_id(hwnd)[0] == pid and w.class_name(hwnd) in classes \
                    and w.user32.IsWindowVisible(hwnd):
                time.sleep(0.4)  # let it lay out
                return hwnd
        time.sleep(0.1)
    return None


def _search_box(window):
    edits = [child for child in w.child_windows(window)
             if w.class_name(child) == ui.EDIT_CLASS and w.user32.IsWindowVisible(child)]
    return edits[0] if len(edits) == 1 else None


def _is_chat(window):
    return any(w.class_name(child) == ui.COMPOSER_CLASS for child in w.child_windows(window))


def _close(window):
    if window and w.user32.IsWindow(window):
        w.post(window, w.WM_CLOSE)


# ---------------------------------------------------------------------- the picker

def _pick(picker, people):
    """Tick every name in the picker. Returns None, or a blocked result (nothing is confirmed then)."""
    box = _search_box(picker)
    if not box:
        return _blocked("friend_picker_not_recognized")
    for name in people:
        w.set_control_text(box, name)
        time.sleep(0.7)
        lines = _lines(picker)
        if lines is None:
            return _blocked("ocr_failed")
        with w.window_dpi(picker):
            below = w.rect(box)[3] - w.rect(picker)[1]  # result rows are drawn under the search box
        rows = [line for line in lines if line["top"] > below and _same(line["text"], name)]
        if len(rows) != 1:
            return _blocked("friend_not_found" if not rows else "several_friends_with_this_name", name=name,
                            detail="Give the name exactly as the friend list shows it.")
        _click(picker, *_center(rows[0]))
        time.sleep(0.3)
    w.set_control_text(box, "")
    time.sleep(0.4)
    with w.window_dpi(picker):
        above = w.rect(box)[1] - w.rect(picker)[1]
    # The dialog's title carries the number of people ticked ("대화상대 선택 2").
    if not any(line["top"] < above and line["text"].strip().endswith(str(len(people))) for line in _lines(picker) or []):
        return _blocked("selection_not_confirmed", detail="The dialog does not show all names as selected.")
    return None


def _confirm(ops, pid, picker, chats_before, room_name=""):
    """Press "확인" until the dialog is done (a group's name dialog included). Returns the new
    chat window when one opened, else None."""
    windows_before = set(w.top_windows())
    current, named = picker, False
    for _ in range(3):
        if not _press(current, CONFIRM_LABELS):
            break
        deadline = time.monotonic() + 3
        follow_up = None
        while time.monotonic() < deadline:
            time.sleep(0.2)
            fresh = [chat for chat in ops._state().chats if chat.hwnd not in chats_before]
            if fresh:
                return fresh[0]
            follow_up = next((hwnd for hwnd in w.top_windows()
                              if hwnd not in windows_before and hwnd != picker and w.process_id(hwnd)[0] == pid
                              and w.class_name(hwnd) in ui.TOP_CLASSES and w.user32.IsWindowVisible(hwnd)
                              and not _is_chat(hwnd)), None)
            if follow_up or not w.user32.IsWindow(current):
                break
        if follow_up:
            current = follow_up
        elif not w.user32.IsWindow(current):
            return None  # the dialog closed without a new window (an invite into the same room)
        box = _search_box(current)
        if room_name and box and not named:  # the group's name dialog
            w.replace_all_text(box, room_name)
            named = True
            time.sleep(0.3)
    return None


# ---------------------------------------------------------------------- operations

def create_room(ops, people, room_name=""):
    names(people)
    check.text(room_name, "room_name", 50, allow_empty=True)
    with safety.process_lock("ui", wait_seconds=10):
        state = ops._state()
        main = state.main
        if not main:
            return _blocked("main_window_not_found")
        if main.locked:
            return _blocked("kakaotalk_locked")
        if not main.chat_list or not w.user32.IsWindowVisible(main.chat_view):
            return _blocked("chats_tab_not_visible",
                            detail="Open the Chats tab in the KakaoTalk main window once, then retry.")
        limited = safety.check_open_allowed()
        if limited:
            return limited
        chats_before = {chat.hwnd for chat in state.chats}
        windows_before = set(w.top_windows())
        safety.record_open()
        w.post_chord(main.chat_list, [w.VK_CONTROL], [VK_N], timeout=1.0)
        picker = _new_window(state.pid, windows_before, ui.TOP_CLASSES)
        if not picker or _is_chat(picker):
            return _blocked("friend_picker_not_opened")
        try:
            problem = _pick(picker, people)
            if problem:
                return problem
            chat = _confirm(ops, state.pid, picker, chats_before, room_name)
        finally:
            _close(picker)
        if chat is None:
            return _blocked("room_window_not_opened", detail="The dialog was confirmed but no chat window appeared.")
        return {"status": "ok", "source": SOURCE, "windowTitle": w.window_text(chat.hwnd), "members": people,
                "note": "Send with this windowTitle. A new group is usually listed for the others once the "
                        "first message is sent."}


def invite(ops, window_title, people):
    check.room(window_title)
    names(people)
    with safety.process_lock("ui", wait_seconds=10):
        state, chat, blocked = ops._find_chat(window_title)
        if blocked:
            return blocked
        if chat is None:
            return _blocked("room_window_not_open", detail="Open it first with open_room")
        chats_before = {item.hwnd for item in state.chats}
        shown = _show(chat.hwnd)
        picker = None
        try:
            windows_before = set(w.top_windows())
            with w.window_dpi(chat.hwnd):
                left, _, right, _ = w.rect(chat.hwnd)
            scale = w.window_scale(chat.hwnd)
            _click(chat.hwnd, right - left - round(MENU_BUTTON[0] * scale), round(MENU_BUTTON[1] * scale))
            menu = _new_window(state.pid, windows_before, (MENU_CLASS,), timeout=2.0)
            if not menu:
                return _blocked("room_menu_not_opened")
            entry = next((line for line in _lines(menu) or []
                          if any(label.casefold() in line["text"].casefold() for label in INVITE_LABELS)), None)
            if not entry:
                _close(menu)
                return _blocked("invite_not_available", detail="This room's menu has no invite entry.")
            windows_before = set(w.top_windows())
            _click(menu, *_center(entry))
            picker = _new_window(state.pid, windows_before, ui.TOP_CLASSES)
            if not picker or _is_chat(picker):
                return _blocked("friend_picker_not_opened")
            problem = _pick(picker, people)
            if problem:
                return problem
            created = _confirm(ops, state.pid, picker, chats_before)
            time.sleep(0.5)
            if w.user32.IsWindow(picker):
                return _blocked("invite_not_confirmed", detail="The dialog did not close; nobody was invited.")
        finally:
            _close(picker)
            _hide(chat.hwnd, shown)
        result = {"status": "ok", "source": SOURCE, "windowTitle": window_title, "invited": people}
        if created:  # inviting into a one-to-one chat opens a new group room instead
            result["newRoomWindowTitle"] = w.window_text(created.hwnd)
        return result

