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
            page_rows, statuses = [], []
            for path, listing in zip(paths, listings):
                recognized = cap.ocr(path)
                statuses.append(recognized.get("status"))
                if recognized.get("status") == "ok":
                    page_rows.append(_rows_from_ocr(recognized.get("lines", []), listing, scale,
                                                    last_page=path == paths[-1]))
            result["ocr"] = {"status": "ok" if statuses and all(s == "ok" for s in statuses) else
                             next((s for s in statuses if s != "ok"), "ocr_failed"), "engine": "Windows.Media.Ocr"}
            if page_rows:
                result["items"] = _merge_pages(page_rows)
                result["total"] = len(result["items"])
        result["note"] = ("Names come from OCR and can contain recognition errors (icons may be read as a "
                          "leading character). Check the images; open_room needs the exact room name. "
                          "hasUnread comes from the red badge colour; unread is the OCR'd number when legible.")
        return result

    def search_rooms(self, query, pages=1):
        """rooms() narrowed by the app's own room search."""
        return self.rooms(query=check.text(query, "query", 256, allow_empty=True), pages=pages)

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

    # ------------------------------------------------------------------ long messages ("전체보기")

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


_TIMEISH = re.compile(r"^(?:(?:오전|오후) ?\d{1,2}:?\d{2}|\d{1,2}:\d{2} ?(?:AM|PM)|어제|Yesterday|"
                      r"\d{1,2}월 \d{1,2}일|\d{4}[.-] ?\d{1,2}[.-] ?\d{1,2}\.?|[월화수목금토일]요일)$")
_KO_TIME = re.compile(r"(오전|오후) ?(\d{1,2}):?(\d{2})")
_UNREAD = re.compile(r"\d{1,3}\+?")
RIGHT_COLUMN = 0.81  # fraction of the list width where the time / badge column starts


def _clean_time(text):
    """OCR often drops the colon ("오후 235"); put it back."""
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
    """True when the right edge of a row holds the red unread badge (OCR often misreads its number)."""
    width, height, data = image
    if not data:
        return None
    red = 0
    for y in range(max(0, top), min(height, bottom), 2):
        row = data[(y * width + int(width * 0.8)) * 4:(y * width + width) * 4]
        for at in range(0, len(row) - 3, 8):
            blue, green, red_value = row[at], row[at + 1], row[at + 2]
            if red_value >= 200 and green <= 150 and blue <= 150 and red_value - green >= 80:
                red += 1
    return red >= 12


def _rows_from_ocr(lines, image, scale=1.0, last_page=True):
    """Group OCR lines of one room-list capture into rooms.

    A room is a profile picture, the name (with member count) and up to two preview
    lines; the time sits right of the name and the unread badge right of the preview.
    Lines are grouped by the vertical gap between them. Rows cut by the top edge are
    dropped (the previous page held them); rows cut by the bottom edge are kept only
    on the last page, marked partial.
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
    rows = []
    for cluster in clusters:
        head = cluster[0]
        beside = sorted((line for line in cluster[1:] if abs(line["top"] - head["top"]) < line_height * 0.6),
                        key=lambda line: line["left"])
        below = [line for line in cluster[1:] if line not in beside]
        top, bottom = min(line["top"] for line in cluster), max(line["bottom"] for line in cluster)
        if top < EDGE_MARGIN * scale:
            continue
        row = _clean_name(" ".join(line["text"].strip() for line in [head] + beside))
        row.update(preview=" ".join(line["text"].strip() for line in below) or None, timeText=None, unread=None,
                   hasUnread=_has_red_badge(image, top, bottom + line_height), _top=top, _bottom=bottom)
        if bottom > height - EDGE_MARGIN * scale or (not below and top > height - ROW_PITCH * 0.6 * scale):
            if not last_page:
                continue
            row["partial"] = True
        rows.append(row)
    for mark in marks:
        center = (mark["top"] + mark["bottom"]) / 2
        near = [row for row in rows if row["_top"] - line_height <= center <= row["_bottom"] + line_height * 1.5]
        if not near:
            continue
        row = min(near, key=lambda item: abs((item["_top"] + item["_bottom"]) / 2 - center))
        text = mark["text"].strip()
        if _TIMEISH.match(text):
            row["timeText"] = row["timeText"] or _clean_time(text)
        elif text != "0" and row["unread"] is None:  # the badge circle alone is often read as "0"
            row["unread"] = text
    return [{key: value for key, value in row.items() if not key.startswith("_")} for row in rows]


def _completeness(row):
    filled = sum(row.get(key) is not None for key in ("preview", "timeText", "unread", "memberCount"))
    return filled - (2 if row.get("partial") else 0)


def _same_room(seen, row):
    if seen["name"] != row["name"]:
        return False
    times = (seen.get("timeText"), row.get("timeText"))
    return None in times or times[0] == times[1]


def _merge_pages(page_rows):
    """Join the rows of consecutive captures; the overlap between pages appears twice."""
    merged = []
    for rows in page_rows:
        previous = merged[-6:]
        for row in rows:
            twin = next((seen for seen in previous if _same_room(seen, row)), None)
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
