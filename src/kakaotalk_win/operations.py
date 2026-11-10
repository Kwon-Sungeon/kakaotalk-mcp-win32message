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
from . import checks as check
from . import clipboard as cb
from . import parser
from . import safety
from . import ui
from . import win32 as w

SOURCE = "kakaotalk_pc_win32"
SB_PAGEUP = 2
SB_BOTTOM = 7
SB_TOP = 6
MESSAGE_KINDS = ("text", "media", "deleted")


def _elapsed_ms(since):
    return round((time.monotonic() - since) * 1000, 1)


def _newest_page(items, limit, offset):
    """The `limit` items that end `offset` items before the newest one, plus the next offset (or None)."""
    stop = len(items) - min(offset, len(items))
    begin = stop - min(limit, stop)
    return items[begin:stop], (offset + stop - begin if begin else None)


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

        problem = "copy failed"
        for backoff in (0.3, 0.6, 0.9):
            try:
                copied, info = cb.capture_text(trigger, pid)
            except (cb.ClipboardConflict, cb.ClipboardBusy) as error:
                problem = str(error)
                time.sleep(backoff)
            else:
                if copied is None:
                    problem = "The app did not copy its message list"
                else:
                    return copied, info
        raise RuntimeError(problem)

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
        check.text(query, "query", 256, allow_empty=True)
        check.flag(ocr, "ocr")
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
        """rooms() narrowed by the app's own room search."""
        return self.rooms(query=check.text(query, "query", 256, allow_empty=True))

    # ------------------------------------------------------------------ open / close

    def open_room(self, window_title, require_existing=True):
        """Open an existing room by exact name via the app's room search; never creates a room."""
        check.room(window_title)
        check.flag(require_existing, "require_existing")
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
        """WM_CLOSE on the chat window. The room itself is not left; a window holding a draft stays open."""
        check.room(window_title)
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
        """Messages loaded in an open chat window, paged from the newest end (oldest first within a page)."""
        check.room(window_title)
        check.number(limit, "limit", 1, 500)
        check.number(offset, "offset", 0, 100000)
        check.flag(open_if_needed, "open_if_needed")
        check.snapshot(snapshot_id, offset)
        with safety.process_lock("ui", wait_seconds=10):
            state, chat, blocked = self._find_chat(window_title)
            if not blocked and chat is None and open_if_needed:
                opening = self.open_room(window_title)
                if opening["status"] != "ok":
                    return opening
                state, chat, blocked = self._find_chat(window_title)
            if blocked:
                return blocked
            if chat is None:
                return _blocked("room_window_not_open", detail="Open it first with open_room (or open_if_needed=true)")
            began = time.monotonic()
            raw, items, info = self._read_items(chat, state.pid)
        current = parser.snapshot_id(raw)
        if snapshot_id not in ("", current):
            return {"status": "snapshot_changed", "source": SOURCE, "snapshotId": current, "items": [],
                    "reason": "The loaded messages changed; read again from offset 0"}
        page, next_offset = _newest_page(items, limit, offset)
        notices = [item for item in items[-20:] if item["kind"] in ("system", "unclassified")]
        warnings = ["A recent notice mentions a restriction/protection measure; check the app."] \
            if any(safety.mentions_restriction(item.get("text")) for item in notices) else []
        dates = [item["date"] for item in items if item.get("date")]
        return {"status": "ok", "source": SOURCE, "windowTitle": window_title, "items": page,
                "total": len(items), "limit": limit, "offset": offset,
                "hasMore": next_offset is not None, "nextOffset": next_offset, "snapshotId": current,
                "messageCount": len([item for item in items if item["kind"] in MESSAGE_KINDS]),
                "coverage": {"scope": "messages_loaded_in_window", "historyComplete": False,
                             "oldestDate": dates[0] if dates else None},
                "clipboard": info["restore"], "warnings": warnings, "elapsedMs": _elapsed_ms(began)}

    def read_many(self, window_titles, limit=50, open_if_needed=True, close_after=False):
        """Read up to 10 rooms in turn. Never in parallel: every read passes through the one clipboard."""
        check.rooms(window_titles, most=10)
        check.number(limit, "limit", 1, 500)
        check.flag(open_if_needed, "open_if_needed")
        check.flag(close_after, "close_after")
        began = time.monotonic()
        results, newly_opened = [], []
        for title in window_titles:
            open_before = bool(self._state().chats_titled(title))
            try:
                outcome = self.read_messages(title, limit=limit, open_if_needed=open_if_needed)
            except Exception as error:  # one failing room must not hide the others
                outcome = {"status": "failed", "errorType": type(error).__name__, "detail": str(error)[:200]}
            if not open_before and self._state().chats_titled(title):
                newly_opened.append(title)
            results.append(dict(outcome, windowTitle=title))
            time.sleep(0.3)
        closed = [self.close_room(title) for title in newly_opened[::-1]] if close_after else []
        ok_count = len([entry for entry in results if entry.get("status") == "ok"])
        return {"status": "partial" if ok_count < len(results) else "ok", "source": SOURCE,
                "items": results, "total": len(results), "succeeded": ok_count,
                "windowsOpened": newly_opened, "cleanup": closed, "elapsedMs": _elapsed_ms(began)}

    def load_more(self, window_title, pages=1):
        """Scroll the message list up so the app loads older messages, then go back to the bottom."""
        check.room(window_title)
        check.number(pages, "pages", 1, 10)
        with safety.process_lock("ui", wait_seconds=10):
            state, chat, blocked = self._find_chat(window_title)
            if blocked:
                return blocked
            if chat is None:
                return _blocked("room_window_not_open")

            def loaded():
                return len(self._read_items(chat, state.pid)[1])

            first = current = loaded()
            steps = []
            try:
                while len(steps) < pages:
                    for _ in range(6):
                        w.post(chat.message_list, w.WM_VSCROLL, SB_PAGEUP, 0)
                    w.post(chat.message_list, w.WM_VSCROLL, SB_TOP, 0)
                    time.sleep(1.0)
                    grown, method = loaded(), "WM_VSCROLL"
                    if grown <= current:
                        # Some builds ignore scroll-bar messages; wheel messages need no focus or cursor either.
                        _post_wheel_up(chat.message_list, notches=10)
                        time.sleep(1.0)
                        grown, method = loaded(), "WM_MOUSEWHEEL"
                    steps.append({"recordsBefore": current, "recordsAfter": grown, "method": method})
                    if grown <= current:
                        break
                    current = grown
            finally:
                w.post(chat.message_list, w.WM_VSCROLL, SB_BOTTOM, 0)
        return {"status": "ok", "source": SOURCE, "windowTitle": window_title, "steps": steps,
                "recordsBefore": first, "recordsAfter": current, "contentChanged": current != first,
                "historyComplete": False, "endOfHistory": None,
                "note": "No growth does not prove the start of the conversation was reached."}

    def search_messages(self, window_title, query, limit=100, load_pages=0, case_sensitive=False, context_rows=2):
        """Substring search over sender and text of the loaded messages, with neighbours for context."""
        check.room(window_title)
        check.text(query, "query", 512)
        check.number(limit, "limit", 1, 500)
        check.number(load_pages, "load_pages", 0, 10)
        check.flag(case_sensitive, "case_sensitive")
        check.number(context_rows, "context_rows", 0, 10)
        if load_pages:
            loading = self.load_more(window_title, load_pages)
            if loading["status"] != "ok":
                return loading
        with safety.process_lock("ui", wait_seconds=10):
            state, chat, blocked = self._find_chat(window_title)
            if blocked:
                return blocked
            if chat is None:
                return _blocked("room_window_not_open")
            raw, items, _ = self._read_items(chat, state.pid)
        fold = str if case_sensitive else str.casefold
        wanted = fold(query)
        hits = [{**item, "contextBefore": items[max(0, at - context_rows):at],
                 "contextAfter": items[at + 1:at + 1 + context_rows]}
                for at, item in enumerate(items)
                if wanted in fold(f"{item.get('sender') or ''} {item.get('text') or ''}")]
        return {"status": "ok", "source": SOURCE, "windowTitle": window_title, "query": query,
                "items": hits[-limit:], "total": len(hits), "truncated": len(hits) > limit,
                "searchedRecords": len(items), "snapshotId": parser.snapshot_id(raw),
                "searchScope": "messages_loaded_in_window", "historyRequested": load_pages > 0,
                "coverage": {"historyComplete": False}}

    # ------------------------------------------------------------------ capture (attachments, emoticons)

    def capture(self, window_title, output_path=None, ocr=False):
        """PNG of one chat window as currently rendered (photos, emoticons, cards)."""
        check.room(window_title)
        check.flag(ocr, "ocr")
        check.new_file(output_path)
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

    def _sender(self):
        from .sender import SendEngine  # imported late: sender imports this module's helpers
        return SendEngine(self)

    def send_message(self, window_title, text, operation_id, wait_seconds=8, require_self=False):
        """One journaled send into an existing room (see sender.py for the full procedure)."""
        return self._sender().send(window_title, text, operation_id, wait_seconds, require_self)

    def send_result(self, operation_id, wait_seconds=0):
        """Journal lookup for an earlier send; re-checks by reading only and never types again."""
        return self._sender().recheck(operation_id, wait_seconds)


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
    return [{**{key: value for key, value in row.items() if not key.startswith("_")}, "position": place}
            for place, row in enumerate(rows)]
