"""One-shot, journaled sending through the official app's own composer.

Order of a send (every step must pass, or nothing is typed):

1. the operation journal: an operation_id is bound to one room and one text
   forever; a repeated call never types again, it only re-checks;
2. account-safety gates (``safety.check_send_allowed``): pacing, hourly/daily
   caps, active safety stop; and no second send of a text whose first send to
   the same room is still unconfirmed (that is how mistakes send twice);
3. the target: an existing room window (opened through the app's room search),
   no draft in the composer, optionally verified as the user's own chat;
4. optionally the human: a native Yes/No dialog owned by this process
   (``confirmSend: dialog``; off by default, the MCP client's tool approval remains);
5. the intent is written to disk *before* anything is typed;
6. EM_REPLACESEL into the composer (KakaoTalk ignores WM_SETTEXT text when
   sending), read back and compared, then one posted Enter; a composer that
   holds anything else is left alone, never cleared;
7. confirmation by re-reading: a new message with the same text after the
   previous tail, and an empty composer.

Anything that cannot be confirmed is ``outcome_unknown`` and is never retried
automatically. Message bodies are stored only as hashes.
"""

import re
import time
from datetime import datetime

from . import checks, confirm, parser, safety
from . import win32 as w

SOURCE = "kakaotalk_pc_win32"
SETTLED = frozenset({"observed_in_window", "blocked"})
MESSAGE_KINDS = ("text", "media", "deleted")
URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
TAIL = 32
POLL_SECONDS = 0.25
UNRESOLVED_WINDOW = 600  # seconds an unconfirmed send blocks the same text to the same room


def plain(text):
    """Line endings as KakaoTalk copies them (LF inside a message)."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def text_hash(text):
    return safety.sha256_hex(safety.normalize_text(text))


def shows_sent_text(item, record):
    """Does this read-back message show the sent text, in full or as the app's shortened view?"""
    shown = item.get("text") or ""
    if text_hash(shown) == record["textHash"]:
        return True
    if shown.endswith(parser.CUT_MARK):
        return text_hash(shown[:-len(parser.CUT_MARK)]) in (record.get("cutHashes") or ())
    return False


def binding(window_title, text, require_self):
    """What an operation_id is tied to: this room, this exact text, this self-check setting."""
    return safety.sha256_hex("\x1f".join((window_title, text, "self-only" if require_self else "any-room")))


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
    def _report(record, **extra):
        phase = record["phase"]
        entered = bool((record.get("dispatch") or {}).get("entered"))
        report = dict(status=phase, operationId=record["operationId"], windowTitle=record["windowTitle"],
                      source=SOURCE, dispatched=phase != "blocked" or entered, retrySafe=False,
                      reason=record.get("reason"), authorVerified=record.get("authorVerified"),
                      dispatch=record.get("dispatch"))
        report.update(extra)
        return report

    @staticmethod
    def _not_sent(operation_id, window_title, reason=None, **extra):
        value = {"status": "blocked", "reason": reason, **extra}
        value.update(operationId=operation_id, windowTitle=window_title, source=SOURCE, dispatched=False,
                     note="Nothing was typed or sent.")
        return value

    # ------------------------------------------------------------------ send

    def send(self, window_title, text, operation_id, wait_seconds=8, require_self=False):
        checks.room(window_title)
        checks.flag(require_self, "require_self")
        wait = checks.seconds(wait_seconds)
        checks.text(text, "text", 100_000)
        path = safety.journal_path(operation_id)
        bound_to = binding(window_title, text, require_self)
        with safety.process_lock("send", wait_seconds=2), safety.process_lock("ui", wait_seconds=10):
            earlier = safety.load_json(path)
            if earlier:
                return self._repeat(path, earlier, bound_to, wait)

            gate = safety.check_send_allowed(window_title, text) or self._unresolved_twin(window_title, text)
            if gate:
                return self._not_sent(operation_id, window_title, **gate)

            state, chat, blocked = self.ops._find_chat(window_title)
            if blocked:
                return self._not_sent(operation_id, window_title, **blocked)
            opened_now = chat is None
            if opened_now:
                opening = self.ops.open_room(window_title)
                if opening["status"] != "ok":
                    return self._not_sent(operation_id, window_title, **opening)
                state, chat, blocked = self.ops._find_chat(window_title)
                if blocked:
                    return self._not_sent(operation_id, window_title, **blocked)
                if chat is None:
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

            me = safety.self_name()
            record = dict(phase="dispatching", operationId=operation_id, windowTitle=window_title,
                          fingerprint=bound_to, textHash=text_hash(text), requireSelf=require_self,
                          cutHashes=[text_hash(part) for part in parser.shown_prefixes(plain(text))],
                          selfName=safety.sha256_hex(me) if me else None, createdAt=time.time(),
                          baselineCount=len(messages),
                          baselineTail=[parser.fingerprint(item) for item in messages[-TAIL:]])
            safety.save_json(path, record)
            safety.record_send(window_title, text, operation_id=operation_id)
            try:
                dispatch = self._dispatch(chat, text)
            except Exception as error:  # the composer may or may not hold the text now
                dispatch = {"entered": None, "errorType": type(error).__name__}
            record["dispatch"] = dispatch
            if dispatch.get("entered") is False:
                record.update(phase="blocked", reason=dispatch.get("reason"))
                safety.save_json(path, record)
                return self._report(record, dispatchCalls=1, warnings=notes)
            try:
                report = self._confirm(path, record, wait, first=True)
            except Exception as error:  # Enter was posted: whatever failed, the outcome is not known
                record.update(phase="outcome_unknown", reason="check_failed")
                report = self._report(record, errorType=type(error).__name__,
                                      note="Do not send again. Check with send_result using the same operation_id.")
            report.update(dispatchCalls=1, warnings=notes, openedWindow=opened_now)
            return report

    @staticmethod
    def _unresolved_twin(window_title, text):
        """An earlier send of this text to this room whose outcome is still unknown: sending again
        could deliver it twice, so the earlier one has to be checked first."""
        for earlier_id in safety.recent_operations(window_title, text, UNRESOLVED_WINDOW):
            earlier = safety.load_json(safety.journal_path(earlier_id)) or {}
            if earlier.get("phase") not in SETTLED:
                return {"reason": "previous_send_unresolved", "previousOperationId": earlier_id,
                        "detail": "The same text was just sent to this room and its outcome is unknown. "
                                  "Check it with send_result and that operation_id before sending again."}
        return None

    def _repeat(self, path, record, bound_to, wait):
        """Same operation_id again: never type, only report or re-check what the journal holds."""
        if record.get("fingerprint") != bound_to:
            raise ValueError("operation_id: already used for a different room or text; make a new ID")
        if record["phase"] in SETTLED:
            report = self._report(record)
        else:
            report = self._confirm(path, record, wait, first=False)
        report.update(deduplicated=True, dispatchCalls=0)
        return report

    def _check_target(self, chat, pid, window_title, require_self):
        try:
            if chat.draft():
                return {"blocked": "existing_draft_preserved"}
        except TimeoutError:
            return {"blocked": "composer_unresponsive"}
        _, items, _ = self.ops._read_items(chat, pid)
        if not items:  # a window opened a moment ago may not have drawn its messages yet
            time.sleep(0.7)
            _, items, _ = self.ops._read_items(chat, pid)
        messages = messages_of(items)
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
        if chat.draft():  # typed by the user since the first check
            return {"entered": False, "reason": "existing_draft_preserved"}
        w.replace_all_text(chat.composer, wanted.replace("\n", "\r\n"))
        time.sleep(0.1)
        if plain(w.control_text(chat.composer)) != wanted:
            # Something else is in there (the user typing, or the app changed the text): never delete it.
            return {"entered": False, "reason": "composer_text_mismatch", "textLeftInComposer": True,
                    "detail": "The composer does not hold exactly the requested text; nothing was sent "
                              "and the composer was left as it is."}
        w.post_plain_key(chat.composer, w.VK_RETURN)
        return {"entered": True, "method": "EM_REPLACESEL + posted Enter"}

    # ------------------------------------------------------------------ confirmation

    def _observe(self, record):
        try:
            return self._look(record)
        except Exception as error:  # the app closed, a window vanished, the clipboard stayed busy ...
            return {"observed": False, "reason": "read_failed", "errorType": type(error).__name__}

    def _look(self, record):
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
        _, items, _ = self.ops._read_items(chat, state.pid)
        messages = messages_of(items)
        fresh, aligned = new_after_tail(messages, record.get("baselineTail") or [])
        if not aligned:
            # The loaded range changed. Accept only the newest message, and only if it is
            # shown in the minute of the dispatch or later.
            fresh = messages[-1:]
        for item in reversed(fresh or []):
            if not shows_sent_text(item, record):  # any kind: a text "사진" parses as a photo
                continue
            if not aligned:
                shown = _local_minutes(item)
                if shown is None or shown < record["createdAt"] // 60 * 60:
                    continue
            author = None
            if record.get("selfName"):
                author = safety.sha256_hex(item.get("sender") or "") == record["selfName"]
                if not author:
                    continue
            if draft:
                return {"observed": False, "reason": "composer_not_empty_after_send"}
            return {"observed": True, "authorVerified": author, "baselineAligned": aligned}
        return {"observed": False, "reason": "new_message_not_observed", "baselineAligned": aligned}

    def _settle_observed(self, path, record, observed):
        record.update(phase="observed_in_window", reason="new_message_and_empty_composer",
                      authorVerified=observed.get("authorVerified"))
        safety.save_json(path, record)
        safety.note_confirmed_send()
        return self._report(record)

    def _confirm(self, path, record, wait, first):
        deadline = time.monotonic() + wait
        pause = POLL_SECONDS
        while True:
            observed = self._observe(record)
            if observed.get("observed"):
                return self._settle_observed(path, record, observed)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(pause, remaining))
            pause = min(pause * 1.5, 1.5)  # every look copies the whole chat through the clipboard
        if first and observed.get("textInComposer"):
            # Enter was not taken: remove our text so it cannot be sent later by accident.
            try:
                w.set_control_text(observed["chat"].composer, "")
                time.sleep(0.4)
                again = self._observe(record)
            except Exception:  # unreadable now: fall through to outcome_unknown below
                again = {}
            if again.get("observed"):
                return self._settle_observed(path, record, again)
            if again.get("reason") == "new_message_not_observed":  # read worked: nothing new, composer empty
                record.update(phase="blocked", reason="enter_not_accepted_text_removed")
                safety.save_json(path, record)
                return self._report(record, safetyStop=safety.note_unconfirmed_send())
        record.update(phase="outcome_unknown", reason=observed.get("reason", "not_observed"))
        safety.save_json(path, record)
        return self._report(record, safetyStop=safety.note_unconfirmed_send() if first else None,
                            note="Do not send again. Check with send_result using the same operation_id.")

    # ------------------------------------------------------------------ later checks

    def recheck(self, operation_id, wait_seconds=0):
        """Report a journaled send; an unsettled one is re-checked by reading only."""
        wait = checks.seconds(wait_seconds)
        path = safety.journal_path(operation_id)
        with safety.process_lock("send", wait_seconds=2), safety.process_lock("ui", wait_seconds=10):
            record = safety.load_json(path)
            if record is None:
                raise ValueError("operation_id: no send with this ID is recorded on this PC")
            if record["phase"] in SETTLED:
                return self._report(record)
            return self._confirm(path, record, wait, first=False)
