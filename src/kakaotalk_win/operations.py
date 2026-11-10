"""High-level KakaoTalk PC operations, one per feature:

    windows         -> list open chat windows (EnumWindows)
    rooms / search_rooms -> room list capture + Windows OCR (the list has no text API)
    open_room       -> main-window room search Edit: WM_SETTEXT + Enter, title verified
    close_room      -> WM_CLOSE on the chat window (drafts preserved)
    read_messages   -> Ctrl+A/Ctrl+C on the message list, parsed (sender, time, date)
    read_attachment -> capture: PrintWindow PNG of the chat window (+ optional OCR)
    read_many       -> sequential reads (the clipboard is a single shared resource)
    read_full_text  -> "전체보기" clicked with window messages, viewer copied and closed
    recent_messages -> room list times + reads, filtered to the last N minutes
    load_more       -> WM_VSCROLL SB_PAGEUP (WM_MOUSEWHEEL fallback) on the list, view restored
    search_messages -> local search over the loaded messages
    send_message    -> composer EM_REPLACESEL + Enter, confirmed by re-reading (sender.py)
    send_result     -> journal lookup + read-only re-check

Every operation goes through the official app's own UI. Nothing talks to Kakao
servers, reads Kakao's files or memory, or modifies the app.
"""

import difflib
import functools
import json
import os
import re
import subprocess
import time
from datetime import datetime, timedelta

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


def _clears_selection(method):
    """Reading selects every message (Ctrl+A) and the app keeps showing that highlight.

    Public operations that copied a message list clear it once, when the outermost
    call finishes, so the user does not find a fully selected chat afterwards.
    """
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        self._depth += 1
        try:
            return method(self, *args, **kwargs)
        finally:
            self._depth -= 1
            if not self._depth and self._selected:
                pending, self._selected = self._selected, {}
                for chat in pending.values():
                    try:
                        _clear_selection(chat)
                    except Exception:  # cosmetic only; never fail the operation for it
                        pass
    return wrapper


class KakaoTalkWin:
    def __init__(self):
        w.enable_dpi_awareness()
        self._selected = {}  # hwnd -> chat window whose message list was select-all copied
        self._depth = 0

    # ------------------------------------------------------------------ helpers

    def _state(self):
        return ui.snapshot()

    def _find_chat(self, title, state=None):
        state = state or self._state()
        matches = state.chats_titled(title)
        if len(matches) > 1:
            return state, None, _blocked("duplicate_room_windows", detail="Several open windows share this title")
        return state, (matches[0] if matches else None), None

    @staticmethod
    def _copy_text_of(control, pid):
        """Select-all and copy inside one of the app's text views, preserving the user's clipboard."""
        def trigger(changed):
            w.post_chord(control, [w.VK_CONTROL], [w.VK_A, w.VK_C], until=changed, timeout=1.5)

        problem = "copy failed"
        for backoff in (0.3, 0.6, 0.9):
            try:
                copied, info = cb.capture_text(trigger, pid)
            except (cb.ClipboardConflict, cb.ClipboardBusy) as error:
                problem = str(error)
                time.sleep(backoff)
            else:
                if copied is None:
                    problem = "The app did not copy the text"
                else:
                    return copied, info
        raise RuntimeError(problem)

    def _copy(self, chat, pid):
        self._selected[chat.hwnd] = chat
        return self._copy_text_of(chat.message_list, pid)

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

    def rooms(self, query="", ocr=True, output_path=None, pages=1):
        """Capture the room list (optionally filtered by the app's search) page by page and OCR it.

        pages > 1 scrolls the list down between captures (overlapping by about one row) and
        scrolls it back to the top afterwards. Only the list is scrolled; nothing is opened.
        """
        check.text(query, "query", 256, allow_empty=True)
        check.flag(ocr, "ocr")
        check.number(pages, "pages", 1, 10)
        check.new_file(output_path)
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
                scale = w.window_scale(main.hwnd)
                listings, end_of_list, restored = _capture_list_pages(main.hwnd, target, pages, scale)
            finally:
                if main.search_edit and query != previous_query:
                    w.set_control_text(main.search_edit, previous_query)
                _restore_visibility(main.hwnd, shown)
        paths = [cap.save_png(listing, output_path if number == 0 else None)
                 for number, listing in enumerate(listings)]
        result = {"status": "ok", "source": SOURCE, "query": query, "imagePath": paths[0], "imagePaths": paths,
                  "pagesCaptured": len(paths), "endOfList": end_of_list, "scrolledBackToTop": restored,
                  "scope": "captured_pages_only", "list": "search_results" if target == main.search_list else "chats",
                  "temporarilyShownMainWindow": shown is not None}
        if ocr:
            page_rows, statuses, badges = [], [], []
            for path, listing in zip(paths, listings):
                recognized = _ocr_list_page(path, listing, scale)
                statuses.append(recognized.get("status"))
                if recognized.get("status") == "ok":
                    last = path == paths[-1]
                    rows = _rows_from_ocr(recognized.get("lines", []), listing, scale, last, positions=True)
                    if any(row.get("nameUnreadable") for row in rows):
                        # A second look at the untouched capture, enlarged (cubic): a short name missed at
                        # 1x is read at 2x. Small scales already used 2x (darkened) for the first pass.
                        again = cap.ocr(path, zoom=2 if scale >= SMALL_SCALE else 3)
                        if again.get("status") == "ok":
                            _fill_unread_names(rows, _rows_from_ocr(again.get("lines", []), listing, scale, last,
                                                                    positions=True), scale)
                    for row in rows:
                        row.pop("_top", None)
                    badges.append((listing, rows))
                    page_rows.append(rows)
            _read_badge_numbers(badges, zoom=max(2, round(2 / scale)))  # smaller digits at lower system scales
            result["ocr"] = {"status": "ok" if statuses and all(s == "ok" for s in statuses) else
                             next((s for s in statuses if s != "ok"), "ocr_failed"), "engine": "Windows.Media.Ocr"}
            if page_rows:
                result["items"] = _merge_pages(page_rows)
                result["total"] = len(result["items"])
        result["note"] = ("Names come from OCR and can contain recognition errors (icons may be read as a "
                          "leading character). Check the images; open_room needs the exact room name. "
                          "hasUnread comes from the red badge colour; unread is the badge number when legible. "
                          "previewUnreadable=true: the row shows a preview OCR could not read (often a one-word "
                          "reply); read the room for its content. nameUnreadable=true: a room is there (its "
                          "time was read) but not its name; check the image.")
        return result

    def search_rooms(self, query, pages=1):
        """rooms() narrowed by the app's own room search."""
        return self.rooms(query=check.text(query, "query", 256, allow_empty=True), pages=pages)

    # ------------------------------------------------------------------ open / close

    @_clears_selection
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

    @_clears_selection
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

    @_clears_selection
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

    @_clears_selection
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

    @_clears_selection
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

    # ------------------------------------------------------------------ long messages ("전체보기")

    @_clears_selection
    def read_full_text(self, window_title, max_messages=3, scroll_pages=10):
        """Full text of loaded messages that the app shortens to their first 500 characters.

        For each "전체보기" button on screen (found by OCR of the chat window), a click is
        delivered as window messages, the viewer window it opens is copied with Ctrl+A,
        Ctrl+C and closed again, and the text is matched to a shortened message by its
        first 500 characters and sender. When the buttons are off screen the list is
        scrolled up a page at a time (at most scroll_pages) and returned to the newest
        messages afterwards.
        """
        check.room(window_title)
        check.number(max_messages, "max_messages", 1, 10)
        check.number(scroll_pages, "scroll_pages", 0, 30)
        with safety.process_lock("ui", wait_seconds=10):
            state, chat, blocked = self._find_chat(window_title)
            if blocked:
                return blocked
            if chat is None:
                return _blocked("room_window_not_open", detail="Open it first with open_room")
            began = time.monotonic()
            _, items, _ = self._read_items(chat, state.pid)
            targets = [item for item in items if item.get("textTruncated")][-max_messages:]
            if not targets:
                return {"status": "ok", "source": SOURCE, "windowTitle": window_title, "items": [], "total": 0,
                        "note": "No shortened (over 500 characters) message among the loaded messages."}
            found, opened_viewers, scrolled = {}, 0, False
            shown = _show_without_activation(chat.hwnd)
            try:
                for page in range(scroll_pages + 1):
                    if page:
                        w.post(chat.message_list, w.WM_VSCROLL, SB_PAGEUP, 0)
                        scrolled = True
                        time.sleep(0.9)
                    for point in _view_all_buttons(chat):
                        full, sender = self._open_view_all(chat, state.pid, point)
                        opened_viewers += full is not None
                        match = _match_shortened(targets, found, full, sender)
                        if match is not None:
                            found[match["index"]] = full
                        if len(found) == len(targets):
                            break
                    if len(found) == len(targets):
                        break
            finally:
                if scrolled:
                    w.post(chat.message_list, w.WM_VSCROLL, SB_BOTTOM, 0)
                _restore_visibility(chat.hwnd, shown)
        expanded = [dict(item, text=found[item["index"]], textTruncated=False, fullText=True)
                    for item in targets if item["index"] in found]
        missing = [{key: item.get(key) for key in ("index", "sender", "date", "time")}
                   for item in targets if item["index"] not in found]
        return {"status": "partial" if missing else "ok", "source": SOURCE, "windowTitle": window_title,
                "items": expanded, "total": len(expanded), "notFound": missing,
                "viewerWindowsOpened": opened_viewers, "scrolledBackToNewest": scrolled or None,
                "elapsedMs": _elapsed_ms(began)}

    def _open_view_all(self, chat, pid, point):
        """Click one "전체보기" button, copy the viewer's text and close it. Returns (text, sender)."""
        before = set(w.top_windows())
        with w.window_dpi(chat.hwnd):
            w.post_click(chat.message_list, *point)
        viewer = _wait_new_window(pid, before, timeout=3.0)
        if viewer is None:
            return None, None
        children = w.child_windows(viewer)
        if any(w.class_name(child) == ui.COMPOSER_CLASS for child in children):
            return None, None  # a chat window appeared (opened by someone else): leave it alone
        try:
            bodies = [child for child in children if w.class_name(child) == ui.VIEWER_BODY_CLASS]
            if len(bodies) != 1:
                return None, None
            time.sleep(0.2)
            try:
                text, _ = self._copy_text_of(bodies[0], pid)
            except RuntimeError:
                return None, None
            title = w.window_text(viewer)
            sender = next((title[:-len(end)] for end in ui.VIEWER_TITLE_SUFFIXES if title.endswith(end)), None)
            return _normalize(text), sender
        finally:
            w.post(viewer, w.WM_CLOSE)
            deadline = time.monotonic() + 2
            while w.user32.IsWindow(viewer) and time.monotonic() < deadline:
                time.sleep(0.05)

    # ------------------------------------------------------------------ recent messages across rooms

    @_clears_selection
    def recent_messages(self, minutes=60, open_rooms=False, list_pages=3, max_rooms=10):
        """Messages of the last `minutes` across rooms.

        The room list (newest first, read by OCR without opening anything) decides which
        rooms had activity in the window. Rooms whose chat window is already open are read
        and filtered by message time. Closed rooms are only listed under notRead unless
        open_rooms=True: then they are opened through the app's room search (which marks
        their messages as read), read, and closed again.
        """
        check.number(minutes, "minutes", 1, 1440)
        check.flag(open_rooms, "open_rooms")
        check.number(list_pages, "list_pages", 1, 10)
        check.number(max_rooms, "max_rooms", 1, 20)
        began, now = time.monotonic(), datetime.now()
        since = now - timedelta(minutes=minutes)
        listing = self.rooms(pages=list_pages)
        cap.discard(listing.get("imagePaths"))  # only the OCR'd rows are needed; never keep the screenshots
        if listing.get("status") != "ok" or "items" not in listing:
            return listing if listing.get("status") != "ok" else _blocked("room_list_unreadable", ocr=listing.get("ocr"))
        rows = listing["items"]
        stamps = [_row_time(row, now) for row in rows]
        last = max((at for at, stamp in enumerate(stamps) if stamp and stamp >= since - timedelta(minutes=1)),
                   default=-1)
        candidates = rows[:last + 1]  # the list is newest first, so rows above an in-window row are in it too
        older_seen = any(stamp and stamp < since for stamp in stamps[last + 1:])
        rooms, not_read, opened_here = [], [], []
        open_titles = [chat.title for chat in self._state().chats]
        claimed = {name for row in rows for name in _row_names(row) if name in open_titles}
        read_titles = set()
        for row in candidates[:max_rooms]:
            summary = {key: row.get(key) for key in ("name", "timeText", "preview", "hasUnread", "unread")}
            if not row.get("name"):  # OCR could not read the name: nothing safe to match or open
                not_read.append(dict(summary, reason="name_unreadable"))
                continue
            title = _match_title(row, open_titles, claimed)
            if title in read_titles:  # two list rows resolved to one window: do not read or report it twice
                not_read.append(dict(summary, reason="ambiguous_room_name"))
                continue
            if title is None and open_rooms:
                title, problem = self._open_listed_room(row)
                if title is None:
                    not_read.append(dict(summary, reason=problem))
                    continue
                opened_here.append(title)
                open_titles.append(title)
            elif title is None:
                not_read.append(dict(summary, reason="room_window_not_open"))
                continue
            read_titles.add(title)
            rooms.append(self._recent_in_room(title, row, since))
        cleanup = [self.close_room(title) for title in opened_here]
        return {"status": "ok", "source": SOURCE, "since": since.strftime("%Y-%m-%d %H:%M"), "minutes": minutes,
                "rooms": rooms, "notRead": not_read, "roomsSkippedOverLimit": max(0, len(candidates) - max_rooms),
                "markedAsRead": opened_here, "cleanup": cleanup,
                "listCoverage": {"pagesCaptured": listing.get("pagesCaptured"), "endOfList": listing.get("endOfList"),
                                 "reachedOlderRooms": older_seen,
                                 "complete": bool(older_seen or listing.get("endOfList"))},
                "elapsedMs": _elapsed_ms(began)}

    def _open_listed_room(self, row):
        """Open a room named by OCR: exact name, then variants, then the search's top hit if it is close."""
        names = [row["name"]] + list(row.get("nameVariants") or [])
        problem = "room_not_found"
        for name in names:
            opened = self.open_room(name, require_existing=False)
            if opened.get("status") == "ok":
                return name, None
            problem = opened.get("reason") or problem
            other = opened.get("openedTitle")
            if opened.get("reason") == "search_top_result_mismatch" and other and _similar(name, other):
                again = self.open_room(other, require_existing=False)
                if again.get("status") == "ok":
                    return other, None
                problem = again.get("reason") or problem
            if problem in ("open_rate_limit", "kakaotalk_locked", "main_window_not_found"):
                break
        return None, problem

    def _recent_in_room(self, title, row, since):
        read = self.read_messages(title, limit=500)
        if read.get("status") != "ok":
            return {"windowTitle": title, "status": read.get("status"), "reason": read.get("reason")}
        loads = 0
        while loads < 2 and _oldest_time(read["items"]) and _oldest_time(read["items"]) > since:
            more = self.load_more(title, 1)
            loads += 1
            if more.get("status") != "ok" or not more.get("contentChanged"):
                break
            read = self.read_messages(title, limit=500)
        fresh = [item for item in read["items"]
                 if item["kind"] in MESSAGE_KINDS and _item_time(item) and _item_time(item) >= since]
        oldest = _oldest_time(read["items"])
        return {"windowTitle": title, "status": "ok", "listName": row.get("name"), "messages": fresh,
                "total": len(fresh), "olderPagesLoaded": loads,
                "mayBeIncomplete": bool(oldest and oldest > since),
                "truncatedMessages": sum(bool(item.get("textTruncated")) for item in fresh)}

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

    @_clears_selection
    def send_message(self, window_title, text, operation_id, wait_seconds=8, require_self=False):
        """One journaled send into an existing room (see sender.py for the full procedure)."""
        return self._sender().send(window_title, text, operation_id, wait_seconds, require_self)

    @_clears_selection
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
    """Child rect relative to its top-level window, in the app's own DPI coordinates."""
    with w.window_dpi(window):
        wl, wt, _, _ = w.rect(window)
        cl, ct, cr, cbottom = w.rect(child)
    return cl - wl, ct - wt, cr - wl, cbottom - wt


# Room-list layout measured at 144 DPI (150%); multiplied by window_scale() for other scales.
SB_LINEDOWN = 1
ROW_PITCH = 105     # height of one room row
LINE_STEP = 20      # distance of one SB_LINEDOWN
AVATAR_EDGE = 100   # text left of this x is inside the profile picture
EDGE_MARGIN = 6     # a row this close to the top/bottom edge is cut off


SMALL_SCALE = 0.9  # below this (system scale under 150%) list text is small enough to need help


def _ocr_list_page(path, listing, scale):
    """OCR one room-list capture. At small system scales the grey time labels are about 12px high
    and half of them are misread; darkened and enlarged 2x (cubic) they read reliably. The saved
    capture the caller returns stays untouched."""
    if scale >= SMALL_SCALE:
        return cap.ocr(path)
    helper = cap.save_png(cap.darken(listing))
    try:
        return cap.ocr(helper, zoom=2)
    finally:
        try:
            os.remove(helper)
        except OSError:
            pass


def _capture_list_pages(window, listing, pages, scale):
    """Capture the list `pages` times, scrolling down by a page minus about a row in between.

    Returns (captures, endOfList, scrolledBackToTop). The list is always sent back to the top.
    """
    box = _relative_box(window, listing)
    steps = max(1, int(((box[3] - box[1]) - 1.5 * ROW_PITCH * scale) / (LINE_STEP * scale)))
    captures, end_of_list, scrolled = [], (None if pages == 1 else False), False
    try:
        for number in range(pages):
            if number:
                for _ in range(steps):
                    w.post(listing, w.WM_VSCROLL, SB_LINEDOWN, 0)
                scrolled = True
                time.sleep(0.6)
            shot = cap.crop(cap.grab(window), box)
            if captures and cap.difference(shot, captures[-1]) < 0.002:
                end_of_list = True
                break
            captures.append(shot)
    finally:
        if scrolled:
            w.post(listing, w.WM_VSCROLL, SB_TOP, 0)
            time.sleep(0.4)
    return captures, end_of_list, (True if scrolled else None)


_TIMEISH = re.compile(r"^(?:(?:오전|오후) ?\d{1,2}[:•·.;]?\d{2}|\d{1,2}:\d{2} ?(?:AM|PM)|어제|Yesterday|"
                      r"\d{1,2}월 \d{1,2}일|\d{4}[.-] ?\d{1,2}[.-] ?\d{1,2}\.?|[월화수목금토일]요일)$")
_KO_TIME = re.compile(r"(오전|오후) ?(\d{1,2})[:•·.;]?(\d{2})")  # OCR reads the colon as • · . ; or drops it
_UNREAD = re.compile(r"\d{1,3}\+?")
RIGHT_COLUMN = 0.81  # fraction of the list width where the time / badge column starts


def _clean_time(text):
    """OCR often drops or misreads the colon ("오후 235", "오후 2•35"); put it back."""
    match = _KO_TIME.fullmatch(text)
    return f"{match[1]} {int(match[2])}:{match[3]}" if match else text


def _split_columns(lines, width):
    """Windows OCR joins words on one baseline into one line, so a room name and its time (or a
    preview and its unread badge) can arrive as one line. Split such lines at the right column."""
    edge = width * RIGHT_COLUMN
    result = []
    for line in lines:
        words = line.get("words") or []
        left = [word for word in words if word["left"] < edge]
        right = [word for word in words if word["left"] >= edge]
        if not (left and right):
            result.append(line)
            continue
        for part in (left, right):
            result.append({"text": " ".join(word["text"] for word in part),
                           "left": min(word["left"] for word in part), "top": min(word["top"] for word in part),
                           "right": max(word["right"] for word in part),
                           "bottom": max(word["bottom"] for word in part), "words": part})
    return result
# An icon in front of a name read as one character and a space (the self-chat badge reads as "0").
_ICON_PREFIX = re.compile(r"^[0-9oO©@ⓞ●•·나] (?=\S)")
# An icon glued to the name ("OAB 프로젝트방" for a flag icon before "AB 프로젝트방").
_GLUED_ICON = re.compile(r"^[0Oo©@ⓞ](?=[A-Z가-힣])")
_MUTE_SUFFIX = re.compile(r"\s+[%Øø@]$")
_MEMBER_SUFFIX = re.compile(r"^(?P<name>.*\S)\s+(?P<count>\d{1,4})$")


def _clean_name(raw):
    name = _MUTE_SUFFIX.sub("", _ICON_PREFIX.sub("", raw))
    members = None
    match = _MEMBER_SUFFIX.match(name)
    if match:
        name, members = match["name"], int(match["count"])
    row = {"name": name, "memberCount": members}
    if name != raw:
        row["rawName"] = raw
    if _GLUED_ICON.match(name):
        row["nameVariants"] = [name[1:]]
    return row


def _has_red_badge(image, top, bottom):
    """True when the right edge of a row holds the red unread badge."""
    if not image[2]:
        return None
    return _red_badge_box(image, top, bottom) is not None


def _has_ink(image, left, top, right, bottom, minimum=12):
    """True when a band holds text: enough pixels far from the band's most common (background) colour."""
    width, height, data = image
    if not data or right <= left or bottom <= top:
        return False
    samples = []
    for y in range(max(0, top), min(height, bottom), 2):
        for x in range(max(0, left), min(width, right), 2):
            at = (y * width + x) * 4
            samples.append(data[at:at + 3])
    if not samples:
        return False
    counts = {}
    for pixel in samples:
        counts[pixel] = counts.get(pixel, 0) + 1
    background = max(counts, key=counts.get)
    ink = sum(1 for pixel in samples if max(abs(a - b) for a, b in zip(pixel, background)) > 80)
    return ink >= minimum


def _is_badge_red(blue, green, red):
    return red >= 200 and green <= 150 and blue <= 150 and red - green >= 80


def _red_badge_box(image, top, bottom):
    """Bounding box (left, top, right, bottom) of the red unread badge at a row's right edge, or None."""
    width, height, data = image
    if not data:
        return None
    start = int(width * 0.8)
    xs, ys = [], []
    for y in range(max(0, top), min(height, bottom)):
        row = data[(y * width + start) * 4:(y * width + width) * 4]
        for at in range(0, len(row) - 3, 4):
            if _is_badge_red(row[at], row[at + 1], row[at + 2]):
                xs.append(start + at // 4)
                ys.append(y)
    if len(xs) < 40:
        return None
    return min(xs), min(ys), max(xs) + 1, max(ys) + 1


BADGE_STRIP = 0.75  # the badge column (with the time labels above each badge) starts here


def _ink_badges(image, boxes):
    """Copy of a page where each red badge is redrawn as dark digits on white.

    Windows OCR reads the white-on-red circle as "0", and it finds nothing at all in
    an image holding only bare digits. Redrawn in place, the digits sit under the
    time labels like ordinary text and are read reliably.
    """
    width, height, data = image
    pixels = bytearray(data)
    for left, top, right, bottom in boxes:
        xs = range(max(0, left - 2), min(width, right + 2))
        for y in range(max(0, top - 2), min(height, bottom + 2)):
            row = {x: data[(y * width + x) * 4:(y * width + x) * 4 + 3] for x in xs}
            red = [x for x in xs if _is_badge_red(*row[x])]
            for x in xs:
                blue, green, red_value = row[x]
                at = (y * width + x) * 4
                if not red or x < red[0] or x > red[-1] or _is_badge_red(blue, green, red_value):
                    pixels[at:at + 3] = b"\xff\xff\xff"  # outside the circle, or its red fill
                else:  # the white digit, anti-aliased edges included, becomes ink
                    ink = max(0, min(255, (min(green, blue) - 120) * 255 // 135))
                    pixels[at:at + 3] = bytes((255 - ink,) * 3)
    return width, height, bytes(pixels)


def _badge_number(words, box, offset):
    """Digits of the OCR words that lie on a badge box (word x is relative to the strip at `offset`)."""
    left, top, right, bottom = box
    found = sorted((word for word in words
                    if word["left"] + offset < right and word["right"] + offset > left
                    and word["top"] < bottom and word["bottom"] > top), key=lambda word: word["left"])
    digits = re.sub(r"[^0-9+]", "", "".join(word["text"] for word in found).replace("O", "0").replace("o", "0"))
    return digits if _UNREAD.fullmatch(digits) and not digits.startswith("0") else None


def _enlarge(image, zoom):
    """Nearest-neighbour enlargement. Short numbers such as "1" are read far more reliably at 2x."""
    width, height, data = image
    rows = []
    for y in range(height):
        line = data[y * width * 4:(y + 1) * width * 4]
        wide = b"".join(line[x * 4:x * 4 + 4] * zoom for x in range(width))
        rows.append(wide * zoom)
    return width * zoom, height * zoom, b"".join(rows)


def _ocr_words(image, language, zoom):
    """OCR words of an image, with coordinates divided back to the original size."""
    path = cap.save_png(image)
    try:
        lines = cap.ocr(path, language).get("lines", [])
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    words = []
    for line in lines:
        line_words = line.get("words") or [line]
        for word in [line_words] if isinstance(line_words, dict) else line_words:
            words.append(dict(word, **{edge: word[edge] / zoom for edge in ("left", "top", "right", "bottom")}))
    return words


def _read_badge_numbers(pages, zoom=2):
    """Fill `unread` from the red badges: the badge column of each page with badges, OCR'd at 2x.

    Badges the Korean pass leaves unread get one English pass; the two engines miss different digits.
    """
    for listing, rows in pages:
        slots = [(row, row.pop("badgeBox")) for row in rows if row.get("badgeBox")]
        if not slots:
            continue
        width, height, _ = listing
        offset = int(width * BADGE_STRIP)
        strip = _enlarge(cap.crop(_ink_badges(listing, [box for _, box in slots]), (offset, 0, width, height)), zoom)
        for language in ("ko", "en"):
            words = _ocr_words(strip, language, zoom)
            for row, box in slots:
                if not row.get("unread"):
                    row["unread"] = _badge_number(words, box, offset)
            if all(row.get("unread") for row, _ in slots):
                break


def _rows_from_ocr(lines, image, scale=1.0, last_page=True, positions=False):
    """Group OCR lines of one room-list capture into rooms.

    A room is a profile picture, the name (with member count) and up to two preview
    lines; the time sits right of the name and the unread badge right of the preview.
    Lines are grouped by the vertical gap between them. Rows cut by the top edge are
    dropped (the previous page held them); rows cut by the bottom edge are kept only
    on the last page, marked partial. A time label with no name next to it is still a
    room: OCR sometimes misses a short name entirely. Such rows come back with
    name=None and nameUnreadable=true rather than disappearing. positions=True keeps the
    row's vertical position (_top) for a second OCR pass to fill the name in.
    """
    width, height = image[0], image[1]
    usable = [line for line in _split_columns(lines, width)
              if line["text"].strip() and line["left"] >= AVATAR_EDGE * scale]

    def right_side(line):
        text = line["text"].strip()
        return bool(_TIMEISH.match(text)) or (bool(_UNREAD.fullmatch(text)) and line["left"] > width * 0.75)

    marks = [line for line in usable if right_side(line)]
    texts = sorted((line for line in usable if not right_side(line)), key=lambda line: (line["top"], line["left"]))
    if not texts:
        return []
    sizes = sorted(line["bottom"] - line["top"] for line in texts)
    line_height = max(8, sizes[len(sizes) // 2])
    clusters = []
    for line in texts:
        if clusters and line["top"] - max(item["bottom"] for item in clusters[-1]) <= line_height * 1.2:
            clusters[-1].append(line)
        else:
            clusters.append([line])
    rows, dropped = [], []
    for number, cluster in enumerate(clusters):
        head = cluster[0]
        beside = sorted((line for line in cluster[1:] if abs(line["top"] - head["top"]) < line_height * 0.6),
                        key=lambda line: line["left"])
        below = [line for line in cluster[1:] if line not in beside]
        top, bottom = min(line["top"] for line in cluster), max(line["bottom"] for line in cluster)
        if top < EDGE_MARGIN * scale:
            dropped.append((top, bottom))
            continue
        # The row's own extent, independent of what OCR found: a missed preview line must not
        # shorten the area searched for the badge, which sits beside the preview.
        next_top = min(line["top"] for line in clusters[number + 1]) if number + 1 < len(clusters) else height
        row_end = int(min(top + ROW_PITCH * 0.8 * scale, next_top - 1, height))
        row = _clean_name(" ".join(line["text"].strip() for line in [head] + beside))
        badge = _red_badge_box(image, int(top), row_end)
        row.update(preview=" ".join(line["text"].strip() for line in below) or None, timeText=None, unread=None,
                   hasUnread=(badge is not None) if image[2] else None, _top=top, _bottom=bottom)
        if badge:
            row["badgeBox"] = badge  # read by _read_badge_numbers, then removed
        if bottom > height - EDGE_MARGIN * scale or (not below and top > height - ROW_PITCH * 0.6 * scale):
            if not last_page:
                dropped.append((top, bottom))
                continue
            row["partial"] = True
        elif not below and _has_ink(image, int(AVATAR_EDGE * scale), int(bottom + 2),
                                    int(width * RIGHT_COLUMN), row_end):
            row["previewUnreadable"] = True  # a short preview ("넵") that OCR did not pick up
        rows.append(row)
    for mark in marks:
        center = (mark["top"] + mark["bottom"]) / 2
        near = [row for row in rows if row["_top"] - line_height <= center <= row["_bottom"] + line_height * 1.5]
        text = mark["text"].strip()
        if not near:
            if _TIMEISH.match(text) and not any(top - line_height <= center <= bottom + line_height * 1.5
                                                for top, bottom in dropped):
                orphan = _nameless_row(mark, image, scale, rows, last_page)
                if orphan:
                    rows.append(orphan)
            continue
        row = min(near, key=lambda item: abs((item["_top"] + item["_bottom"]) / 2 - center))
        if _TIMEISH.match(text):
            row["timeText"] = row["timeText"] or _clean_time(text)
        elif text != "0" and row["unread"] is None:  # the badge circle alone is often read as "0"
            row["unread"] = text
    rows.sort(key=lambda row: row["_top"])
    keep = ("_top",) if positions else ()
    return [{key: value for key, value in row.items() if key in keep or not key.startswith("_")} for row in rows]


def _nameless_row(mark, image, scale, rows, last_page):
    """A room known only from its time label (name and preview both missed by OCR), or None."""
    width, height = image[0], image[1]
    top, bottom = mark["top"], mark["bottom"]
    if top < EDGE_MARGIN * scale:
        return None
    following = [row["_top"] for row in rows if row["_top"] > bottom]
    row_end = int(min(top + ROW_PITCH * 0.8 * scale, min(following, default=height) - 1, height))
    badge = _red_badge_box(image, int(top), row_end)
    row = {"name": None, "memberCount": None, "nameUnreadable": True, "preview": None,
           "timeText": _clean_time(mark["text"].strip()), "unread": None,
           "hasUnread": (badge is not None) if image[2] else None, "_top": top, "_bottom": bottom}
    if badge:
        row["badgeBox"] = badge
    if top > height - ROW_PITCH * 0.6 * scale:
        if not last_page:
            return None
        row["partial"] = True
    elif _has_ink(image, int(AVATAR_EDGE * scale), int(bottom + 2), int(width * RIGHT_COLUMN), row_end):
        row["previewUnreadable"] = True
    return row


def _fill_unread_names(rows, second, scale):
    """Copy name and preview from a second OCR pass into rows that came back nameless."""
    for row in rows:
        if not row.get("nameUnreadable"):
            continue
        match = next((other for other in second if other.get("name") and not other.get("nameUnreadable")
                      and abs(other["_top"] - row["_top"]) <= ROW_PITCH * 0.3 * scale
                      and other.get("timeText") in (None, row.get("timeText"))), None)
        if match is None:
            continue
        for key in ("name", "memberCount", "rawName", "nameVariants"):
            if match.get(key) is not None:
                row[key] = match[key]
        if match.get("preview") and not row.get("preview"):
            row["preview"] = match["preview"]
            row.pop("previewUnreadable", None)
        row.pop("nameUnreadable", None)


def _completeness(row):
    filled = sum(row.get(key) is not None for key in ("name", "preview", "timeText", "unread", "memberCount"))
    return filled + (2 if row.get("name") is not None else 0) - (2 if row.get("partial") else 0)


def _same_room(seen, row, in_overlap=False):
    """Whether a row of a new page repeats a row of the previous one.

    A nameless row (nameUnreadable) has only its time; different rooms often share a
    time label, so it is matched only inside the overlap between the two pages.
    """
    times = (seen.get("timeText"), row.get("timeText"))
    if seen["name"] is None or row["name"] is None:
        return in_overlap and times[0] is not None and times[0] == times[1]
    if seen["name"] != row["name"]:
        return False
    return None in times or times[0] == times[1]


def _merge_pages(page_rows):
    """Join the rows of consecutive captures; the overlap between pages appears twice."""
    merged = []
    for rows in page_rows:
        previous = merged[-6:]
        overlap = merged[-2:]  # pages overlap by about one row
        for place, row in enumerate(rows):
            twin = next((seen for seen in previous
                         if _same_room(seen, row, in_overlap=place < 2 and any(seen is item for item in overlap))),
                        None)
            if twin is None:
                merged.append(row)
            elif _completeness(row) > _completeness(twin):
                twin.clear()
                twin.update(row)
    return [dict(row, position=place) for place, row in enumerate(merged)]


def _view_all_buttons(chat):
    """List-client points of the "전체보기" buttons on screen, lowest (newest) first."""
    with w.window_dpi(chat.hwnd):
        window = w.rect(chat.hwnd)
        listing = w.rect(chat.message_list)
    path = cap.save_png(cap.grab(chat.hwnd))
    try:
        recognized = cap.ocr(path)
    finally:
        try:
            os.remove(path)  # the capture shows the conversation; keep it no longer than needed
        except OSError:
            pass
    labels = {label.replace(" ", "").casefold() for label in ui.VIEW_ALL_LABELS}
    points = []
    for line in recognized.get("lines", []):
        if line["text"].replace(" ", "").casefold() not in labels:
            continue
        x = (line["left"] + line["right"]) // 2 - (listing[0] - window[0])
        y = (line["top"] + line["bottom"]) // 2 - (listing[1] - window[1])
        if 0 <= x < listing[2] - listing[0] and 0 <= y < listing[3] - listing[1]:
            points.append((x, y))
    return sorted(points, key=lambda point: -point[1])


def _wait_new_window(pid, before, timeout):
    """A top-level window of the app that appeared after `before` was taken, or None."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for hwnd in w.top_windows():
            if hwnd in before or w.process_id(hwnd)[0] != pid:
                continue
            if w.class_name(hwnd) in ui.TOP_CLASSES and w.user32.IsWindowVisible(hwnd):
                time.sleep(0.3)  # let it lay out its children
                return hwnd
        time.sleep(0.1)
    return None


def _match_shortened(targets, found, full, sender):
    """The shortened message whose shown 500 characters start `full` (same sender), newest first."""
    if not full:
        return None
    whole = safety.normalize_text(full)
    for item in reversed(targets):
        if item["index"] in found:
            continue
        if sender and item.get("sender") and item["sender"] != sender:
            continue
        shown = safety.normalize_text(item["text"][:-len(parser.CUT_MARK)])
        if shown and whole.startswith(shown):
            return item
    return None


# ---------------------------------------------------------------------- clearing the select-all highlight

def _plain_strip(image, left, top, width, height):
    """True when the strip just above the bottom edge of the message list is empty background.

    Background means: every sampled pixel across the middle of the strip has the
    same colour as the list's left edge on that row. A message bubble, photo,
    link card or "new message" bar there fails the test, and nothing is clicked.
    """
    image_width, image_height, data = image

    def pixel(x, y):
        at = (y * image_width + x) * 4
        return data[at:at + 3]

    for y in range(top + height - 6, top + height - 1):
        if not 0 <= y < image_height:
            return False
        background = pixel(left + 3, y)
        for x in range(left + int(width * 0.1), left + int(width * 0.9), 3):
            if any(abs(a - b) > 6 for a, b in zip(pixel(x, y), background)):
                return False
    return True


def _clear_selection(chat):
    """Click the empty strip under the newest message to drop the Ctrl+A highlight."""
    if not w.user32.IsWindow(chat.message_list) or w.user32.IsIconic(chat.hwnd):
        return False
    image = cap.grab(chat.hwnd)
    with w.window_dpi(chat.hwnd):
        window_left, window_top, _, _ = w.rect(chat.hwnd)
        left, top, right, bottom = w.rect(chat.message_list)
    width, height = right - left, bottom - top
    if not _plain_strip(image, left - window_left, top - window_top, width, height):
        return False
    with w.window_dpi(chat.hwnd):
        w.post_click(chat.message_list, width // 2, height - 4)
    return True


# ---------------------------------------------------------------------- recent messages helpers

_EN_TIME = re.compile(r"(\d{1,2}):(\d{2}) ?(AM|PM)", re.IGNORECASE)


def _row_time(row, now):
    """Latest moment a room-list time label can mean (today's time, or the end of an earlier day)."""
    text = (row.get("timeText") or "").strip()
    if not text:
        return None
    ko, en = _KO_TIME.fullmatch(text), _EN_TIME.fullmatch(text)
    if ko or en:
        hour, minute = (int(ko[2]), int(ko[3])) if ko else (int(en[1]), int(en[2]))
        afternoon = (ko and ko[1] == "오후") or (en and en[3].upper() == "PM")
        hour = hour % 12 + (12 if afternoon else 0)
        if hour > 23 or minute > 59:
            return None
        return now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    end_of_yesterday = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(seconds=1)
    if text in ("어제", "Yesterday"):
        return end_of_yesterday
    return end_of_yesterday - timedelta(days=1)  # a weekday or a date: older than yesterday


def _item_time(item):
    try:
        return datetime.strptime(f"{item.get('date')} {item.get('time')}", "%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return None


def _oldest_time(items):
    return next((_item_time(item) for item in items if _item_time(item)), None)


def _comparable(name):
    return re.sub(r"[\s.…]+$", "", (name or "").strip()).casefold()


def _similar(ocr_name, title):
    """An OCR'd room name and a real title that differ only by recognition errors or truncation."""
    shown, real = _comparable(ocr_name), _comparable(title)
    if not shown or not real:
        return False
    if len(shown) >= 6 and real.startswith(shown):  # the list shortens long names with "…"
        return True
    # Rooms often differ only by a number ("24기 모임" / "25기 모임", "1팀" / "2팀"): never treat
    # names with different numbers as one room, however close the rest of the text is.
    if re.findall(r"\d+", shown) != re.findall(r"\d+", real):
        return False
    return difflib.SequenceMatcher(None, shown, real).ratio() >= 0.8


def _row_names(row):
    return [row.get("name")] + list(row.get("nameVariants") or [])


def _match_title(row, titles, claimed=()):
    """The exact title of an open chat window that a room-list row refers to, if exactly one fits.

    `claimed` holds titles another listed row names exactly; those are never matched loosely.
    """
    names = _row_names(row)
    for name in names:
        if name in titles:
            return name
    close = {title for title in titles if title not in claimed for name in names if _similar(name, title)}
    return close.pop() if len(close) == 1 else None
