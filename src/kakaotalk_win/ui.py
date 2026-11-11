"""Feature map of the official KakaoTalk PC window tree.

Every feature is mapped to a specific native window found by class name or by
the app's internal pane caption (e.g. ``ChatRoomListView_0x...``), which do not
depend on the UI language. The agent never walks the tree itself; it calls one
tool per feature.

    main window (EVA_Window_Dblclk, has OnlineMainView_*)
    ├─ ContactListView_*     friends tab
    ├─ ChatRoomListView_*    chats tab
    │   ├─ ChatRoomListCtrl_*  room list (owner-drawn, no text API -> capture/OCR)
    │   ├─ SearchListCtrl_*    search results
    │   └─ Edit                room search box (WM_SETTEXT + Enter opens the top result)
    └─ LockModeView_*        app lock screen (never touched)

    chat window (EVA_Window_Dblclk, one per open room, title = room name)
    ├─ EVA_VH_ListControl_Dblclk  message list (Ctrl+A, Ctrl+C -> clipboard text)
    │   └─ _EVA_CustomScrollCtrl   (WM_VSCROLL on the list loads older messages)
    ├─ RICHEDIT50W                composer (WM_GETTEXT / WM_SETTEXT, Enter sends)
    └─ Edit                       in-room search box (unused; search is done locally)

    "전체보기" viewer (EVA_Window_Dblclk, title "<sender>님의 메시지", opened by the
    button under a message longer than 500 characters)
    └─ EVA_ChildWindow_Dblclk     full message text (Ctrl+A, Ctrl+C -> clipboard text)
"""

import os
from dataclasses import dataclass, field

from . import win32 as w

PROCESS_NAME = "kakaotalk.exe"
TOP_CLASSES = ("EVA_Window_Dblclk", "EVA_Window")
MESSAGE_LIST_CLASS = "EVA_VH_ListControl_Dblclk"
COMPOSER_CLASS = "RICHEDIT50W"
EDIT_CLASS = "Edit"
MAIN_VIEW = "OnlineMainView_"
CHAT_LIST_VIEW = "ChatRoomListView_"
CHAT_LIST_CTRL = "ChatRoomListCtrl_"
CHAT_SEARCH_LIST = "SearchListCtrl_"
CONTACT_VIEW = "ContactListView_"
LOCK_VIEW = "LockModeView_"
VIEWER_BODY_CLASS = "EVA_ChildWindow_Dblclk"
VIEW_ALL_LABELS = ("전체보기", "View All", "See All")
VIEWER_TITLE_SUFFIXES = ("님의 메시지", "'s message", "'s Message")

# The empty composer reports its placeholder through WM_GETTEXT.
DEFAULT_PLACEHOLDERS = ("메시지 입력", "챗봇에게 메시지 입력", "Enter a message", "Type a message", "メッセージを入力")


def placeholders():
    extra = os.environ.get("KAKAOTALK_WIN_PLACEHOLDERS", "")
    return set(DEFAULT_PLACEHOLDERS) | {value for value in extra.split("|") if value}


class KakaoNotRunning(RuntimeError):
    reason = "kakaotalk_not_running"


class SeveralKakaoProcesses(KakaoNotRunning):
    reason = "multiple_kakaotalk_processes"


@dataclass
class ChatWindow:
    hwnd: int
    title: str
    message_list: int
    composer: int
    search_edit: int = 0

    @property
    def minimized(self):
        return bool(w.user32.IsIconic(self.hwnd))

    def draft(self):
        """Composer text, or "" when only the placeholder is shown."""
        text = w.control_text(self.composer)
        return "" if text in placeholders() else text


@dataclass
class MainWindow:
    hwnd: int
    chat_view: int = 0
    chat_list: int = 0
    search_list: int = 0
    search_edit: int = 0
    contact_view: int = 0
    lock_view: int = 0

    @property
    def locked(self):
        return bool(self.lock_view and w.user32.IsWindowVisible(self.lock_view))

    @property
    def visible(self):
        return bool(w.user32.IsWindowVisible(self.hwnd))

    @property
    def minimized(self):
        return bool(w.user32.IsIconic(self.hwnd))


@dataclass
class Snapshot:
    pid: int
    image: str
    main: MainWindow = None
    chats: list = field(default_factory=list)

    def chats_titled(self, title):
        return [chat for chat in self.chats if chat.title == title]


def kakao_processes():
    seen = {}
    for hwnd in w.top_windows():
        pid, _ = w.process_id(hwnd)
        if pid in seen:
            continue
        image = w.process_image(pid)
        if os.path.basename(image).lower() == PROCESS_NAME:
            seen[pid] = image
    return seen


def _direct_children(hwnd):
    return [child for child in w.child_windows(hwnd) if w.handle(w.user32.GetParent(child)) == hwnd]


def _caption_child(children, prefix):
    for child in children:
        if w.window_text(child).startswith(prefix):
            return child
    return 0


def snapshot():
    """Locate the main window and every open chat window of the running app."""
    processes = kakao_processes()
    if not processes:
        raise KakaoNotRunning("KakaoTalk PC is not running")
    if len(processes) > 1:
        raise SeveralKakaoProcesses("More than one KakaoTalk process is running")
    pid, image = next(iter(processes.items()))
    state = Snapshot(pid=pid, image=image)
    for hwnd in w.top_windows():
        if w.process_id(hwnd)[0] != pid or w.class_name(hwnd) not in TOP_CLASSES:
            continue
        descendants = w.child_windows(hwnd)
        main_view = _caption_child(descendants, MAIN_VIEW)
        if main_view:
            main = MainWindow(hwnd=hwnd)
            main.chat_view = _caption_child(descendants, CHAT_LIST_VIEW)
            main.chat_list = _caption_child(descendants, CHAT_LIST_CTRL)
            main.contact_view = _caption_child(descendants, CONTACT_VIEW)
            main.lock_view = _caption_child(descendants, LOCK_VIEW)
            if main.chat_view:
                view_children = _direct_children(main.chat_view)
                main.search_list = _caption_child(view_children, CHAT_SEARCH_LIST)
                edits = [child for child in view_children if w.class_name(child) == EDIT_CLASS]
                main.search_edit = edits[0] if len(edits) == 1 else 0
            state.main = main
            continue
        if not w.user32.IsWindowVisible(hwnd) and not w.user32.IsIconic(hwnd):
            continue
        direct = _direct_children(hwnd)
        by_class = {}
        for child in direct:
            by_class.setdefault(w.class_name(child), []).append(child)
        composers = by_class.get(COMPOSER_CLASS, [])
        lists = by_class.get(MESSAGE_LIST_CLASS, [])
        if len(composers) == 1 and len(lists) == 1:
            edits = by_class.get(EDIT_CLASS, [])
            state.chats.append(ChatWindow(hwnd=hwnd, title=w.window_text(hwnd), message_list=lists[0],
                                          composer=composers[0], search_edit=edits[0] if len(edits) == 1 else 0))
    return state
