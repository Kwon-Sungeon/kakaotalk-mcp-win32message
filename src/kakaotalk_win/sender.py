"""One-shot, journaled sending through the official app's own composer.

Order of a send (every step must pass, or nothing is typed):

1. the operation journal: an operation_id is bound to one room and one text
   forever; a repeated call never types again, it only re-checks;
2. account-safety gates (``safety.check_send_allowed``): pacing, hourly/daily
   caps, no broadcasting, active safety stop;
3. the target: an existing room window (opened through the app's room search,
   never a new room), no draft in the composer, conversation history present,
   optionally verified as the user's own chat;
4. optionally the human: a native Yes/No dialog owned by this process
   (``confirmSend: dialog``; off by default, the MCP client's tool approval remains);
5. the intent is written to disk *before* anything is typed;
6. EM_REPLACESEL into the composer (KakaoTalk ignores WM_SETTEXT text when
   sending), read back and compared, then one posted Enter;
7. confirmation by re-reading: a new message with the same text after the
   previous tail, and an empty composer.

Anything that cannot be confirmed is ``outcome_unknown`` and is never retried
automatically. Message bodies are stored only as hashes.
"""

import json
import re
import time
from datetime import datetime

from . import confirm, parser, safety
from . import win32 as w

SOURCE = "kakaotalk_pc_win32"
FINAL_PHASES = ("observed_in_window", "blocked")
MESSAGE_KINDS = ("text", "media", "deleted")
URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
TAIL = 32


def wait_value(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 20:
        raise ValueError("wait_seconds must be between 0 and 20")
    return float(value)


def plain(text):
    """Line endings as KakaoTalk copies them (LF inside a message)."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def text_hash(text):
    return safety.digest(safety.normalize_text(text))


def messages_of(items):
    return [item for item in items if item.get("kind") in MESSAGE_KINDS]


def new_after_tail(messages, tail):
    """Messages after the last occurrence of the baseline tail, or None if the tail is gone."""
    prints = [parser.fingerprint(item) for item in messages]
    if not tail:
        return messages, True
    size = len(tail)
    for end in range(len(prints), size - 1, -1):
        if prints[end - size:end] == tail:
            return messages[end:], True
    return None, False


def _local_minutes(item):
    if not item.get("date") or not item.get("time"):
        return None
    try:
        return datetime.strptime(f"{item['date']} {item['time']}", "%Y-%m-%d %H:%M").timestamp()
    except ValueError:
        return None


class SendEngine:
    def __init__(self, ops):
        self.ops = ops

    # ------------------------------------------------------------------ results

    @staticmethod
    def _output(record, **extra):
        return {"status": record["phase"], "operationId": record["operationId"],
                "windowTitle": record["windowTitle"], "source": SOURCE, "dispatched": record["phase"] != "blocked"
                or bool(record.get("dispatch", {}).get("entered")), "retrySafe": False,
                "reason": record.get("reason"), "authorVerified": record.get("authorVerified"),
                "dispatch": record.get("dispatch"), **extra}

    @staticmethod
    def _not_sent(operation_id, window_title, reason=None, **extra):
        value = {"status": "blocked", "reason": reason, **extra}
        value.update(operationId=operation_id, windowTitle=window_title, source=SOURCE, dispatched=False,
                     note="Nothing was typed or sent.")
        return value

    # ------------------------------------------------------------------ send

    def send(self, window_title, text, operation_id, wait_seconds=8, require_self=False):
        from .operations import _boolean, _title
        _title(window_title)
        _boolean(require_self, "require_self")
        wait = wait_value(wait_seconds)
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text must be a non-empty string")
        path = safety.journal_path(operation_id)
        fingerprint = safety.digest(json.dumps([window_title, text, require_self], ensure_ascii=False))
        with safety.process_lock("send", wait_seconds=2), safety.process_lock("ui", wait_seconds=10):
            record = safety.load_json(path)
            if record:
                if record.get("fingerprint") != fingerprint:
                    raise ValueError("operation_id is already bound to another room or message")
                if record["phase"] in FINAL_PHASES:
                    return self._output(record, deduplicated=True, dispatchCalls=0)
                return {**self._confirm(path, record, wait, first=False), "deduplicated": True, "dispatchCalls": 0}

            gate = safety.check_send_allowed(window_title, text)
            if gate:
                return self._not_sent(operation_id, window_title, **gate)

            state, chat, blocked = self.ops._find_chat(window_title)
            if blocked:
                return self._not_sent(operation_id, window_title, **blocked)
            opened_now = False
            if not chat:
                opened = self.ops.open_room(window_title, require_existing=False)
                if opened.get("status") != "ok":
                    return self._not_sent(operation_id, window_title, **opened)
                opened_now = True
                state, chat, blocked = self.ops._find_chat(window_title)
                if blocked:
                    return self._not_sent(operation_id, window_title, **blocked)
                if not chat:
                    return self._not_sent(operation_id, window_title, "room_window_not_open")
            if state.main and state.main.locked:
                return self._not_sent(operation_id, window_title, "kakaotalk_locked")
            checked = self._check_target(chat, state.pid, window_title, require_self)
            if checked.get("blocked"):
                return self._not_sent(operation_id, window_title, checked["blocked"], openedWindow=opened_now,
                                      **checked.get("extra", {}))
            messages = checked["messages"]

            notes = []
            if URL.search(text):
                notes.append("링크가 포함되어 있습니다. 광고·홍보성 링크 반복 전송은 이용제한 사유입니다.")
            if safety.confirm_mode() == "dialog":
                if not confirm.ask_send(window_title, text, notes=notes):
                    return self._not_sent(operation_id, window_title, "declined_by_user")
                # The human may have typed into the room while the dialog was open.
                state, chat, blocked = self.ops._find_chat(window_title)
                if blocked or not chat:
                    return self._not_sent(operation_id, window_title, "room_window_changed")
                try:
                    if chat.draft():
                        return self._not_sent(operation_id, window_title, "existing_draft_preserved")
                except TimeoutError:
                    return self._not_sent(operation_id, window_title, "composer_unresponsive")
                _, items, _ = self.ops._read_items(chat, state.pid)
                messages = messages_of(items)

            record = {"operationId": operation_id, "windowTitle": window_title, "fingerprint": fingerprint,
                      "textHash": text_hash(text), "requireSelf": require_self,
                      "selfName": safety.digest(safety.self_name()) if safety.self_name() else None,
                      "baselineCount": len(messages),
                      "baselineTail": [parser.fingerprint(item) for item in messages][-TAIL:],
                      "createdAt": time.time(), "phase": "dispatching"}
            safety.save_json(path, record)
            safety.record_send(window_title, text)
            try:
                dispatch = self._dispatch(chat, text)
            except Exception as error:  # the composer may or may not hold the text now
                dispatch = {"entered": None, "errorType": type(error).__name__}
            record["dispatch"] = dispatch
            if dispatch.get("entered") is False:
                record.update(phase="blocked", reason=dispatch.get("reason"))
                safety.save_json(path, record)
                return self._output(record, dispatchCalls=1, warnings=notes)
            result = self._confirm(path, record, wait, first=True)
            return {**result, "dispatchCalls": 1, "warnings": notes, "openedWindow": opened_now}

    def _check_target(self, chat, pid, window_title, require_self):
        try:
            if chat.draft():
                return {"blocked": "existing_draft_preserved"}
        except TimeoutError:
            return {"blocked": "composer_unresponsive"}
        _, items, _ = self.ops._read_items(chat, pid)
        messages = messages_of(items)
        if not messages:
            return {"blocked": "room_without_history",
                    "extra": {"detail": "Only rooms with an existing conversation can receive messages; "
                                        "this tool never starts a new chat."}}
        if any(item["kind"] in ("system", "unclassified") and safety.mentions_restriction(item.get("text"))
               for item in items[-30:]):
            stop = safety.trip("restriction_notice_in_room", room=safety.short_hash(window_title))
            return {"blocked": "restriction_notice_detected", "extra": {"safetyStop": stop}}
        if require_self:
            me = safety.self_name()
            senders = {item.get("sender") for item in messages}
            if not me:
                return {"blocked": "self_name_not_configured",
                        "extra": {"detail": "Run `kakaotalk-win config --self-name \"내 프로필 이름\"` first."}}
            if window_title != me or senders - {me}:
                return {"blocked": "self_room_not_verified",
                        "extra": {"detail": "The room title and every loaded sender must equal the configured "
                                            "self name."}}
        return {"messages": messages, "items": items}

    @staticmethod
    def _dispatch(chat, text):
        """Exactly one method, never a fallback chain (a second attempt could send twice)."""
        wanted = plain(text)
        w.replace_all_text(chat.composer, wanted.replace("\n", "\r\n"))
        time.sleep(0.1)
        current = plain(w.control_text(chat.composer))
        if current != wanted:
            w.set_control_text(chat.composer, "")
            return {"entered": False, "reason": "composer_text_mismatch",
                    "detail": "The composer did not hold exactly the requested text; it was cleared."}
        w.post_plain_key(chat.composer, w.VK_RETURN)
        return {"entered": True, "method": "EM_REPLACESEL + posted Enter"}

    # ------------------------------------------------------------------ confirmation

    def _observe(self, record):
        state = self.ops._state()
        chats = state.chats_titled(record["windowTitle"])
        if len(chats) != 1:
            return {"observed": False, "reason": "room_window_not_open" if not chats else "duplicate_room_windows"}
        chat = chats[0]
        try:
            draft = chat.draft()
        except TimeoutError:
            return {"observed": False, "reason": "composer_unresponsive"}
        if draft and text_hash(draft) == record["textHash"]:
            return {"observed": False, "reason": "text_still_in_composer", "textInComposer": True, "chat": chat}
        try:
            _, items, _ = self.ops._read_items(chat, state.pid)
        except RuntimeError:
            return {"observed": False, "reason": "read_failed"}
        messages = messages_of(items)
        fresh, aligned = new_after_tail(messages, record.get("baselineTail") or [])
        if not aligned:
            # The loaded range changed. Accept only the newest message, and only if its
            # displayed time is no earlier than the dispatch.
            fresh = messages[-1:]
        for item in reversed(fresh or []):
            if item.get("kind") != "text" or text_hash(item.get("text") or "") != record["textHash"]:
                continue
            if not aligned:
                shown = _local_minutes(item)
                if shown is None or shown < record["createdAt"] - 120:
                    continue
            author = None
            if record.get("selfName"):
                author = safety.digest(item.get("sender") or "") == record["selfName"]
                if not author:
                    continue
            if draft:
                return {"observed": False, "reason": "composer_not_empty_after_send"}
            return {"observed": True, "authorVerified": author, "baselineAligned": aligned}
        return {"observed": False, "reason": "new_message_not_observed", "baselineAligned": aligned}

    def _confirm(self, path, record, wait, first):
        until = time.monotonic() + wait
        observed = {}
        while True:
            observed = self._observe(record)
            if observed.get("observed"):
                record.update(phase="observed_in_window", reason="new_message_and_empty_composer",
                              authorVerified=observed.get("authorVerified"))
                safety.save_json(path, record)
                safety.note_confirmed_send()
                return self._output(record)
            if time.monotonic() >= until:
                break
            time.sleep(min(0.25, max(0.0, until - time.monotonic())))
        if first and observed.get("textInComposer"):
            # Enter was not taken: remove our text so it cannot be sent later by accident.
            chat = observed["chat"]
            try:
                w.set_control_text(chat.composer, "")
                time.sleep(0.4)
                again = self._observe(record)
            except Exception:
                again = {}
            if again.get("observed"):
                record.update(phase="observed_in_window", reason="new_message_and_empty_composer",
                              authorVerified=again.get("authorVerified"))
                safety.save_json(path, record)
                safety.note_confirmed_send()
                return self._output(record)
            if not again.get("textInComposer") and again.get("reason") != "composer_unresponsive":
                record.update(phase="blocked", reason="enter_not_accepted_text_removed")
                safety.save_json(path, record)
                stop = safety.note_unconfirmed_send()
                return self._output(record, safetyStop=stop)
        record.update(phase="outcome_unknown", reason=observed.get("reason", "not_observed"))
        safety.save_json(path, record)
        stop = safety.note_unconfirmed_send() if first else None
        return self._output(record, safetyStop=stop,
                            note="Do not send again. Check with send_result using the same operation_id.")

    # ------------------------------------------------------------------ result

    def result(self, operation_id, wait_seconds=0):
        wait = wait_value(wait_seconds)
        path = safety.journal_path(operation_id)
        with safety.process_lock("send", wait_seconds=2), safety.process_lock("ui", wait_seconds=10):
            record = safety.load_json(path)
            if not record:
                raise ValueError("Unknown operation_id")
            if record["phase"] in FINAL_PHASES:
                return self._output(record)
            return self._confirm(path, record, wait, first=False)
