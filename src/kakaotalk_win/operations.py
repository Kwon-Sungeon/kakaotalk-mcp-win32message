"""High-level KakaoTalk PC operations, one per feature:

    windows         -> list open chat windows (EnumWindows)
    rooms / search_rooms -> room list capture + Windows OCR (the list has no text API)
    open_room       -> main-window room search Edit: WM_SETTEXT + Enter, title verified
    close_room      -> WM_CLOSE on the chat window (drafts preserved)
    read_messages   -> Ctrl+A/Ctrl+C on the message list, parsed (sender, time, date)
    read_attachment -> capture: PrintWindow PNG of the chat window (+ optional OCR)
    read_many       -> sequential reads (the clipboard is a single shared resource)
    load_more       -> WM_VSCROLL SB_PAGEUP (WM_MOUSEWHEEL fallback) on the list, view restored
    search_messages -> local search over the loaded messages
    send_message    -> composer EM_REPLACESEL + Enter, confirmed by re-reading (sender.py)
    send_result     -> journal lookup + read-only re-check

Every operation goes through the official app's own UI. Nothing talks to Kakao
servers, reads Kakao's files or memory, or modifies the app.
"""

import json
import os
import re
import subprocess
import time

from . import capture as cap
from . import clipboard as cb
from . import parser
from . import safety
from . import ui
from . import win32 as w

SOURCE = "kakaotalk_pc_win32"
SB_PAGEUP = 2
SB_BOTTOM = 7
SB_TOP = 6


def _title(value, name="window_title"):
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise ValueError(f"{name} must be the exact room name (1..512 characters)")
    return value


def _integer(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{name} must be an integer between {low} and {high}")
    return value


def _boolean(value, name):
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def _blocked(reason, **extra):
    return {"status": "blocked", "source": SOURCE, "reason": reason, **extra}


def _normalize(text):
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


class KakaoTalkWin:
    def __init__(self):
        w.enable_dpi_awareness()

    # ------------------------------------------------------------------ helpers

    def _state(self):
        return ui.snapshot()

    def _find_chat(self, title, state=None):
        state = state or self._state()
        matches = state.chats_titled(title)
        if len(matches) > 1:
            return state, None, _blocked("duplicate_room_windows", detail="Several open windows share this title")
        return state, (matches[0] if matches else None), None

    def _copy(self, chat, pid):
        """Return the message list text via the app's own copy, preserving the clipboard."""
        def trigger(changed):
            w.post_chord(chat.message_list, [w.VK_CONTROL], [w.VK_A, w.VK_C], until=changed, timeout=1.5)

        last_error = None
        for attempt in range(3):
            try:
                text, info = cb.capture_text(trigger, pid)
            except (cb.ClipboardConflict, cb.ClipboardBusy) as error:
                last_error = error
                time.sleep(0.3 * (attempt + 1))
                continue
            if text is not None:
                return text, info
            last_error = RuntimeError("The app did not copy its message list")
        raise RuntimeError(str(last_error) if last_error else "copy failed")

    def _read_items(self, chat, pid):
        raw, info = self._copy(chat, pid)
        return raw, parser.parse(raw, safety.self_name()), info

    # ------------------------------------------------------------------ status / windows

    def status(self):
        result = {"status": "ok", "source": SOURCE, "platform": "windows"}
        try:
            state = self._state()
        except ui.KakaoNotRunning as error:
            return {**result, "status": "blocked", "reason": "kakaotalk_not_running", "detail": str(error)}
        result.update(kakaoTalk={"processId": state.pid, "image": state.image,
                                 "version": w.file_version(state.image), "signature": _signature(state.image)},
                      mainWindow=None if not state.main else {
                          "found": True, "visible": state.main.visible, "minimized": state.main.minimized,
                          "locked": state.main.locked, "roomSearchAvailable": bool(state.main.search_edit)},
                      openChatWindows=len(state.chats),
                      clipboard=cb.history_settings(),
                      remoteSession=w.remote_session(),
                      safety={"limits": safety.limits(), "confirmSend": safety.confirm_mode(),
                              "selfNameConfigured": bool(safety.self_name()),
                              "safetyStop": safety.breaker_state() or {"tripped": False}})
        warnings = []
        if result["kakaoTalk"]["signature"].get("status") != "Valid":
            warnings.append("KakaoTalk.exe is not validly signed by Kakao; use only the official installer.")
        if result["remoteSession"]:
            warnings.append("Running inside a remote desktop session; use your own physical PC.")
        if result["clipboard"]["historyEnabled"] or result["clipboard"]["cloudSyncEnabled"]:
            warnings.append("Windows clipboard history/sync is on: copied chat text may be kept there.")
        if state.main and state.main.locked:
            warnings.append("KakaoTalk is locked; unlock it in the app.")
        result["warnings"] = warnings
        return result

    def windows(self):
        """List open chat windows. Does not read messages or change anything."""
        state = self._state()
        items = []
        for chat in state.chats:
            try:
                draft = bool(chat.draft())
            except TimeoutError:
                draft = None
            items.append({"windowTitle": chat.title, "minimized": chat.minimized, "hasDraft": draft,
                          "readable": True})
        return {"status": "ok", "source": SOURCE, "items": items, "total": len(items),
                "mainWindow": bool(state.main), "locked": bool(state.main and state.main.locked)}

    # ------------------------------------------------------------------ rooms (capture + OCR)

    def rooms(self, query="", ocr=True, output_path=None):
        """Capture the visible room list (optionally filtered by the app's search) and OCR it."""
        if not isinstance(query, str) or len(query) > 256:
            raise ValueError("query must be a string of at most 256 characters")
        _boolean(ocr, "ocr")
        with safety.process_lock("ui"):
            state = self._state()
            main = state.main
            if not main:
                return _blocked("main_window_not_found")
            if main.locked:
                return _blocked("kakaotalk_locked")
            if not main.chat_view or not main.chat_list:
                return _blocked("chat_list_unavailable")
            previous_query = ""
            if main.search_edit:
                previous_query = w.control_text(main.search_edit)
            shown = _show_without_activation(main.hwnd)
            try:
                if query and not main.search_edit:
                    return _blocked("room_search_unavailable")
                if main.search_edit and query != previous_query:
                    w.set_control_text(main.search_edit, query)
                    time.sleep(0.7)
                if not w.user32.IsWindowVisible(main.chat_view):
                    return _blocked("chats_tab_not_visible",
                                    detail="Open the Chats tab in the KakaoTalk main window once, then retry.")
                target = main.search_list if query and main.search_list and \
                    w.user32.IsWindowVisible(main.search_list) else main.chat_list
                time.sleep(0.2)
                image = cap.grab(main.hwnd)
                box = _relative_box(main.hwnd, target)
                listing = cap.crop(image, box)
                path = cap.save_png(listing, output_path)
            finally:
                if main.search_edit and query != previous_query:
                    w.set_control_text(main.search_edit, previous_query)
                _restore_visibility(main.hwnd, shown)
        result = {"status": "ok", "source": SOURCE, "query": query, "imagePath": path,
                  "scope": "visible_rows_only", "list": "search_results" if target == main.search_list else "chats",
                  "temporarilyShownMainWindow": shown is not None}
        if ocr:
            recognized = cap.ocr(path)
            result["ocr"] = {"status": recognized.get("status"), "engine": "Windows.Media.Ocr"}
            if recognized.get("status") == "ok":
                result["items"] = _rows_from_ocr(recognized.get("lines", []), listing[0])
                result["total"] = len(result["items"])
        result["note"] = ("Names come from OCR of the visible rows and can contain recognition errors. "
                          "Check the image; open_room needs the exact room name.")
        return result

    def search_rooms(self, query):
        if not isinstance(query, str):
            raise ValueError("query must be a string")
        return self.rooms(query=query)

    # ------------------------------------------------------------------ open / close

    def open_room(self, window_title, require_existing=True):
        """Open an existing room by exact name via the app's room search; never creates a room."""
        _title(window_title)
        _boolean(require_existing, "require_existing")
        with safety.process_lock("ui"):
            state, chat, blocked = self._find_chat(window_title)
            if blocked:
                return blocked
            if chat:
                return {"status": "ok", "source": SOURCE, "windowTitle": window_title, "alreadyOpen": True}
            if not state.main:
                return _blocked("main_window_not_found")
            if state.main.locked:
                return _blocked("kakaotalk_locked")
            if not state.main.search_edit:
                return _blocked("room_search_unavailable")
            limited = safety.check_open_allowed()
            if limited:
                return limited
            before = {item.hwnd for item in state.chats}
            previous = w.control_text(state.main.search_edit)
            safety.record_open()
            try:
                w.set_control_text(state.main.search_edit, window_title)
                time.sleep(0.6)
                w.post_key(state.main.search_edit, w.VK_RETURN)
                opened = None
                deadline = time.monotonic() + 4
                while time.monotonic() < deadline and opened is None:
                    time.sleep(0.1)
                    current = self._state()
                    fresh = [item for item in current.chats if item.hwnd not in before]
                    if fresh:
                        time.sleep(0.3)  # let the title settle
                        opened = fresh[0]
                        opened.title = w.window_text(opened.hwnd)
            finally:
                w.set_control_text(state.main.search_edit, previous)
            if opened is None:
                return _blocked("room_not_found", detail="The app's room search returned nothing to open")
            if opened.title != window_title:
                closed = self._close_window(opened)
                return _blocked("search_top_result_mismatch", openedTitle=opened.title, closedAgain=closed,
                                detail="The top search result was a different room; it was closed again. "
                                       "Retry with the exact openedTitle if that is the intended room.")
            result = {"status": "ok", "source": SOURCE, "windowTitle": window_title, "alreadyOpen": False}
            if require_existing:
                try:
                    _, items, _ = self._read_items(opened, state.pid)
                    result["existingHistory"] = any(item["kind"] != "unclassified" for item in items)
                except RuntimeError:
                    result["existingHistory"] = None
            return result

    def _close_window(self, chat):
        try:
            if chat.draft():
                return False
        except TimeoutError:
            return False
        w.post(chat.hwnd, w.WM_CLOSE)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if not w.user32.IsWindow(chat.hwnd):
                return True
            time.sleep(0.05)
        return False

    def close_room(self, window_title):
        """Close the chat window only (not leaving the room). Keeps windows with a draft open."""
        _title(window_title)
        with safety.process_lock("ui"):
            _, chat, blocked = self._find_chat(window_title)
            if blocked:
                return blocked
            if not chat:
                return {"status": "ok", "source": SOURCE, "windowTitle": window_title, "alreadyClosed": True}
            if chat.draft():
                return _blocked("existing_draft_preserved")
            if not self._close_window(chat):
                return _blocked("close_not_observed")
            return {"status": "ok", "source": SOURCE, "windowTitle": window_title, "alreadyClosed": False}

    # ------------------------------------------------------------------ reading

    def read_messages(self, window_title, limit=50, offset=0, snapshot_id="", open_if_needed=False):
        """Read the messages currently loaded in an open chat window, newest page first."""
        _title(window_title)
        _integer(limit, "limit", 1, 500)
        _integer(offset, "offset", 0, 100000)
        _boolean(open_if_needed, "open_if_needed")
        if not isinstance(snapshot_id, str) or snapshot_id and not re.fullmatch(r"[0-9a-f]{24}", snapshot_id):
            raise ValueError("snapshot_id must be the snapshotId returned by a previous read")
        if offset and not snapshot_id:
            raise ValueError("snapshot_id is required for offset > 0; read the first page again")
        with safety.process_lock("ui", wait_seconds=10):
            state, chat, blocked = self._find_chat(window_title)
            if blocked:
                return blocked
            if not chat and open_if_needed:
                opened = self.open_room(window_title)
                if opened.get("status") != "ok":
                    return opened
                state, chat, blocked = self._find_chat(window_title)
            if not chat:
                return _blocked("room_window_not_open", detail="Open it first with open_room (or open_if_needed=true)")
            started = time.monotonic()
            raw, items, info = self._read_items(chat, state.pid)
        snapshot = parser.snapshot_id(raw)
        if snapshot_id and snapshot_id != snapshot:
            return {"status": "snapshot_changed", "source": SOURCE, "snapshotId": snapshot, "items": [],
                    "reason": "The loaded messages changed; read again from offset 0"}
        end = max(0, len(items) - offset)
        start = max(0, end - limit)
        page = items[start:end]
        warnings = []
        if any(item["kind"] in ("system", "unclassified") and safety.mentions_restriction(item.get("text"))
               for item in items[-20:]):
            warnings.append("A recent notice mentions a restriction/protection measure; check the app.")
        return {"status": "ok", "source": SOURCE, "windowTitle": window_title, "items": page,
                "total": len(items), "limit": limit, "offset": offset, "hasMore": start > 0,
                "nextOffset": offset + (end - start) if start > 0 else None, "snapshotId": snapshot,
                "messageCount": sum(item["kind"] in ("text", "media", "deleted") for item in items),
                "coverage": {"scope": "messages_loaded_in_window", "historyComplete": False,
                             "oldestDate": next((item["date"] for item in items if item.get("date")), None)},
                "clipboard": info["restore"], "warnings": warnings,
                "elapsedMs": round((time.monotonic() - started) * 1000, 1)}

    def read_many(self, window_titles, limit=50, open_if_needed=True, close_after=False):
        """Read up to 10 rooms one after another (the clipboard can serve one copy at a time)."""
        if not isinstance(window_titles, list) or not 1 <= len(window_titles) <= 10:
            raise ValueError("window_titles must contain 1..10 exact room names")
        for name in window_titles:
            _title(name)
        if len(set(window_titles)) != len(window_titles):
            raise ValueError("Duplicate window_titles are not allowed")
        _integer(limit, "limit", 1, 500)
        _boolean(open_if_needed, "open_if_needed")
        _boolean(close_after, "close_after")
        started = time.monotonic()
        items, opened = [], []
        for name in window_titles:
            was_open = bool(self._state().chats_titled(name))
            try:
                value = self.read_messages(name, limit=limit, open_if_needed=open_if_needed)
            except Exception as error:  # keep other rooms' results
                value = {"status": "failed", "errorType": type(error).__name__, "detail": str(error)[:200]}
            if not was_open and self._state().chats_titled(name):
                opened.append(name)
            items.append({"windowTitle": name, **value})
            time.sleep(0.3)
        cleanup = [self.close_room(name) for name in reversed(opened)] if close_after else []
        succeeded = sum(item.get("status") == "ok" for item in items)
        return {"status": "ok" if succeeded == len(items) else "partial", "source": SOURCE, "items": items,
                "total": len(items), "succeeded": succeeded, "windowsOpened": opened, "cleanup": cleanup,
                "atomicAcrossRooms": False, "elapsedMs": round((time.monotonic() - started) * 1000, 1)}

    def load_more(self, window_title, pages=1):
        """Ask the app to load older messages by scrolling the list up, then restore the view."""
        _title(window_title)
        _integer(pages, "pages", 1, 10)
        with safety.process_lock("ui", wait_seconds=10):
            state, chat, blocked = self._find_chat(window_title)
            if blocked:
                return blocked
            if not chat:
                return _blocked("room_window_not_open")
            _, before, _ = self._read_items(chat, state.pid)
            count = len(before)
            steps = []
            try:
                for _ in range(pages):
                    for _ in range(6):
                        w.post(chat.message_list, w.WM_VSCROLL, SB_PAGEUP, 0)
                    w.post(chat.message_list, w.WM_VSCROLL, SB_TOP, 0)
                    time.sleep(1.0)
                    _, after, _ = self._read_items(chat, state.pid)
                    method = "WM_VSCROLL"
                    if len(after) <= count:
                        # Fallback: wheel-up messages to the list (no focus or cursor change).
                        _post_wheel_up(chat.message_list, notches=10)
                        time.sleep(1.0)
                        _, after, _ = self._read_items(chat, state.pid)
                        method = "WM_MOUSEWHEEL"
                    steps.append({"recordsBefore": count, "recordsAfter": len(after), "method": method})
                    if len(after) <= count:
                        break
                    count = len(after)
            finally:
                w.post(chat.message_list, w.WM_VSCROLL, SB_BOTTOM, 0)
        return {"status": "ok", "source": SOURCE, "windowTitle": window_title, "steps": steps,
                "recordsBefore": len(before), "recordsAfter": count, "contentChanged": count != len(before),
                "historyComplete": False, "endOfHistory": None,
                "note": "No growth does not prove the start of the conversation was reached."}

    def search_messages(self, window_title, query, limit=100, load_pages=0, case_sensitive=False, context_rows=2):
        """Search the loaded messages of one room (optionally loading older ones first)."""
        _title(window_title)
        if not isinstance(query, str) or not query.strip() or len(query) > 512:
            raise ValueError("query must be a non-empty string of at most 512 characters")
        _integer(limit, "limit", 1, 500)
        _integer(load_pages, "load_pages", 0, 10)
        _boolean(case_sensitive, "case_sensitive")
        _integer(context_rows, "context_rows", 0, 10)
        history = self.load_more(window_title, load_pages) if load_pages else None
        if history and history.get("status") != "ok":
            return history
        with safety.process_lock("ui", wait_seconds=10):
            state, chat, blocked = self._find_chat(window_title)
            if blocked:
                return blocked
            if not chat:
                return _blocked("room_window_not_open")
            raw, items, _ = self._read_items(chat, state.pid)
        needle = query if case_sensitive else query.casefold()
        matches = []
        for index, item in enumerate(items):
            haystack = " ".join(str(item.get(key) or "") for key in ("sender", "text"))
            if needle in (haystack if case_sensitive else haystack.casefold()):
                matches.append({**item, "contextBefore": items[max(0, index - context_rows):index],
                                "contextAfter": items[index + 1:index + 1 + context_rows]})
        return {"status": "ok", "source": SOURCE, "windowTitle": window_title, "query": query,
                "items": matches[-limit:], "total": len(matches), "truncated": len(matches) > limit,
                "searchedRecords": len(items), "snapshotId": parser.snapshot_id(raw),
                "searchScope": "messages_loaded_in_window", "historyRequested": bool(load_pages),
                "coverage": {"historyComplete": False}}

    def close_search(self, window_title):
        """Compatibility no-op: searching is done locally and never opens the app's search UI."""
        _title(window_title)
        return {"status": "ok", "source": SOURCE, "windowTitle": window_title, "searchUiOpened": False}

    # ------------------------------------------------------------------ capture (attachments, emoticons)

    def capture(self, window_title, output_path=None, ocr=False):
        """PNG of one chat window as currently rendered (photos, emoticons, cards)."""
        _title(window_title)
        _boolean(ocr, "ocr")
        if output_path is not None and (not isinstance(output_path, str) or not os.path.isabs(output_path)):
            raise ValueError("output_path must be an absolute path")
        if output_path and os.path.exists(output_path):
            raise ValueError("output_path already exists")
        with safety.process_lock("ui", wait_seconds=10):
            _, chat, blocked = self._find_chat(window_title)
            if blocked:
                return blocked
            if not chat:
                return _blocked("room_window_not_open")
            shown = _show_without_activation(chat.hwnd)
            try:
                time.sleep(0.15 if shown else 0)
                image = cap.grab(chat.hwnd)
            finally:
                _restore_visibility(chat.hwnd, shown)
        path = cap.save_png(image, output_path)
        result = {"status": "ok", "source": SOURCE, "windowTitle": window_title, "imagePath": path,
                  "width": image[0], "height": image[1], "temporarilyRestored": shown is not None,
                  "note": "Screen-resolution capture of the window, not the original attachment file."}
        if ocr:
            result["ocr"] = cap.ocr(path)
        return result

    # ------------------------------------------------------------------ sending

    def send_message(self, window_title, text, operation_id, wait_seconds=8, require_self=False):
        from .sender import SendEngine
        return SendEngine(self).send(window_title, text, operation_id, wait_seconds, require_self)

    def send_result(self, operation_id, wait_seconds=0):
        from .sender import SendEngine
        return SendEngine(self).result(operation_id, wait_seconds)


# ---------------------------------------------------------------------- module helpers

def _signature(image):
    """Authenticode status of KakaoTalk.exe via Get-AuthenticodeSignature (local check only)."""
    script = ("$s = Get-AuthenticodeSignature -LiteralPath $env:KAKAOTALK_IMAGE; "
              "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
              "@{status = [string]$s.Status; subject = [string]$s.SignerCertificate.Subject} | ConvertTo-Json -Compress")
    try:
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                                capture_output=True, timeout=20, env={**os.environ, "KAKAOTALK_IMAGE": image},
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        value = json.loads(result.stdout.decode("utf-8", errors="replace").strip() or "{}")
    except Exception as error:
        return {"status": "unknown", "errorType": type(error).__name__}
    subject = value.get("subject") or ""
    status = value.get("status") or "unknown"
    if status == "Valid" and "kakao" not in subject.lower():
        status = "UnexpectedSigner"
    return {"status": status, "signer": re.search(r"O=([^,]+)", subject).group(1) if "O=" in subject else None}


def _show_without_activation(hwnd):
    """Make a hidden/minimized window renderable without focusing it. Returns prior state or None."""
    if w.user32.IsIconic(hwnd):
        w.user32.ShowWindow(hwnd, w.SW_SHOWNOACTIVATE)
        time.sleep(0.25)
        return "minimized"
    if not w.user32.IsWindowVisible(hwnd):
        w.user32.ShowWindow(hwnd, w.SW_SHOWNOACTIVATE)
        time.sleep(0.25)
        return "hidden"
    return None


def _restore_visibility(hwnd, previous):
    if previous == "minimized":
        w.user32.ShowWindow(hwnd, w.SW_SHOWMINNOACTIVE)
    elif previous == "hidden":
        w.user32.ShowWindow(hwnd, 0)  # SW_HIDE


def _post_wheel_up(hwnd, notches=1):
    left, top, right, bottom = w.rect(hwnd)
    x, y = (left + right) // 2, (top + bottom) // 2
    lparam = (x & 0xFFFF) | ((y & 0xFFFF) << 16)
    for _ in range(notches):
        w.post(hwnd, w.WM_MOUSEWHEEL, 120 << 16, lparam)
        time.sleep(0.03)


def _relative_box(window, child):
    wl, wt, _, _ = w.rect(window)
    cl, ct, cr, cbottom = w.rect(child)
    return cl - wl, ct - wt, cr - wl, cbottom - wt


_TIMEISH = re.compile(r"^(?:(?:오전|오후) ?\d{1,2}:\d{2}|\d{1,2}:\d{2} ?(?:AM|PM)|어제|Yesterday|"
                      r"\d{1,2}월 \d{1,2}일|\d{4}[.-] ?\d{1,2}[.-] ?\d{1,2}\.?|[월화수목금토일]요일)$")


# A single badge glyph OCR'd in front of a name (the self-chat icon reads as "0").
_BADGE = re.compile(r"^[0-9oO©@ⓞ●•·] (?=\S)")


def _rows_from_ocr(lines, width):
    """Group OCR lines of the room list into rows: name / preview on the left, time / unread on the right."""
    if not lines:
        return []
    lines = sorted(lines, key=lambda item: (item["top"], item["left"]))
    left_column = [line for line in lines if line["left"] < width * 0.62 and not _TIMEISH.match(line["text"])]
    right_column = [line for line in lines if line not in left_column]
    heights = sorted(line["bottom"] - line["top"] for line in left_column) or [16]
    line_height = max(8, heights[len(heights) // 2])
    rows = []
    for line in left_column:
        if rows and line["top"] - rows[-1]["_top"] < line_height * 2.2 and rows[-1]["preview"] is None:
            rows[-1]["preview"] = line["text"]
            rows[-1]["_bottom"] = line["bottom"]
            continue
        row = {"name": _BADGE.sub("", line["text"]), "preview": None, "timeText": None, "unread": None,
               "_top": line["top"], "_bottom": line["bottom"]}
        if row["name"] != line["text"]:
            row["rawName"] = line["text"]  # e.g. the self-chat badge read as "0"
        rows.append(row)
    for line in right_column:
        center = (line["top"] + line["bottom"]) / 2
        nearest = min(rows, key=lambda row: abs((row["_top"] + row["_bottom"]) / 2 - center), default=None)
        if nearest is None:
            continue
        text = line["text"].strip()
        if _TIMEISH.match(text) and nearest["timeText"] is None:
            nearest["timeText"] = text
        elif re.fullmatch(r"\d{1,3}\+?|999\+", text) and nearest["unread"] is None:
            nearest["unread"] = text
    for index, row in enumerate(rows):
        row["position"] = index
        row.pop("_top")
        row.pop("_bottom")
    return rows
