import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

if sys.platform != "win32":
    raise unittest.SkipTest("kakaotalk_win requires Windows")

from kakaotalk_win import safety, sender  # noqa: E402
from kakaotalk_win import win32 as w  # noqa: E402


def message(sender_name, text, minute=0):
    return {"kind": "text", "sender": sender_name, "text": text, "date": time.strftime("%Y-%m-%d"),
            "time": time.strftime("%H:%M", time.localtime(time.time() + minute * 60))}


class FakeChat:
    def __init__(self, world, title):
        self.world, self.title, self.composer, self.hwnd = world, title, title, id(title)

    def draft(self):
        return self.world.composer.get(self.title, "")


class World:
    """A fake KakaoTalk: rooms with history, open windows, composers and an Enter behaviour."""

    def __init__(self):
        self.rooms = {"나": [message("나", "메모")], "친구": [message("친구", "안녕"), message("나", "응")],
                      "빈방": [], "둘": [message("x", "y")]}
        self.open = {"친구"}
        self.composer = {}
        self.enter = "delivered"
        self.enters = 0
        self.typed = []

    def state(self):
        chats = [FakeChat(self, title) for title in sorted(self.open)]
        return SimpleNamespace(pid=1, main=SimpleNamespace(locked=False), chats=chats,
                               chats_titled=lambda title: [chat for chat in chats if chat.title == title])

    # win32 fakes
    def replace_all_text(self, hwnd, text):
        self.composer[hwnd] = text
        self.typed.append(text)

    def control_text(self, hwnd, limit=65536):
        return self.composer.get(hwnd, "").replace("\r\n", "\r")

    def set_control_text(self, hwnd, text):
        self.composer[hwnd] = text

    def post_plain_key(self, hwnd, vk):
        self.enters += 1
        if self.enter == "delivered":
            sent = self.composer[hwnd].replace("\r\n", "\n")
            if len(sent) > 500:  # the real app shows (and copies) only the first 500 characters
                sent = sent[:500] + "..."
            self.rooms[hwnd].append(message("나", sent))
            self.composer[hwnd] = ""
        elif self.enter == "slow":  # taken from the composer, not shown yet
            self.pending = self.composer[hwnd].replace("\r\n", "\n")
            self.composer[hwnd] = ""


class FakeOps:
    def __init__(self, world):
        self.world = world
        self.opens = []

    def _state(self):
        return self.world.state()

    def _find_chat(self, title, state=None):
        state = state or self._state()
        found = state.chats_titled(title)
        return state, (found[0] if found else None), None

    def open_room(self, title, require_existing=True):
        self.opens.append(title)
        if title not in self.world.rooms:
            return {"status": "blocked", "reason": "room_not_found"}
        self.world.open.add(title)
        return {"status": "ok", "windowTitle": title, "alreadyOpen": False}

    def _read_items(self, chat, pid):
        return "raw", [dict(item) for item in self.world.rooms[chat.title]], {}


class SenderTests(unittest.TestCase):
    def setUp(self):
        state_dir = Path(tempfile.mkdtemp(prefix="kakaotalk-win-test-"))
        self.addCleanup(shutil.rmtree, state_dir, ignore_errors=True)
        self.world = World()
        self.ops = FakeOps(self.world)
        self.answers = []
        patches = [
            patch.object(safety, "STATE_ROOT", state_dir),
            patch.dict(os.environ, {"KAKAOTALK_WIN_SELF_NAME": "나"}),
            patch.object(w, "replace_all_text", self.world.replace_all_text),
            patch.object(w, "control_text", self.world.control_text),
            patch.object(w, "set_control_text", self.world.set_control_text),
            patch.object(w, "post_plain_key", self.world.post_plain_key),
            patch.object(sender.confirm, "ask_send", self.ask),
            patch.object(sender.time, "sleep", lambda seconds: None),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        os.environ.pop("KAKAOTALK_WIN_CONFIRM", None)

    def ask(self, room, text, notes=()):
        self.answers.append((room, text, tuple(notes)))
        return True

    def assertStatus(self, result, expected):
        self.assertEqual(expected, result.get("status"), result)

    def assertReason(self, result, expected):
        self.assertEqual(expected, result.get("reason"), result)

    def send(self, room="친구", text="회의 자료 보냈어", op="op-1", **kwargs):
        return sender.SendEngine(self.ops).send(room, text, op, wait_seconds=kwargs.pop("wait", 0.5), **kwargs)

    def dialog_mode(self):
        mode = patch.dict(os.environ, {"KAKAOTALK_WIN_CONFIRM": "dialog"})
        mode.start()
        self.addCleanup(mode.stop)

    def allow_next_send(self):
        log = safety.load_json(safety.STATE_ROOT / "send-log.json", [])
        for entry in log:
            entry["at"] -= 3600 * 3
        safety.save_json(safety.STATE_ROOT / "send-log.json", log)

    def test_send_is_confirmed_once_and_deduplicated(self):
        result = self.send(text="첫 줄\n둘째 줄")
        self.assertStatus(result, "observed_in_window")
        self.assertTrue(result["authorVerified"])
        self.assertEqual(result["dispatchCalls"], 1)
        self.assertEqual(self.world.typed, ["첫 줄\r\n둘째 줄"])
        again = self.send(text="첫 줄\n둘째 줄")
        self.assertTrue(again["deduplicated"])
        self.assertEqual(again["dispatchCalls"], 0)
        self.assertEqual(self.world.enters, 1)
        self.assertEqual(self.answers, [])  # no extra dialog by default
        with self.assertRaises(ValueError):
            self.send(text="다른 본문")
        journal = safety.journal_path("op-1").read_text(encoding="utf-8")
        self.assertNotIn("둘째 줄", journal)

    def test_closed_existing_room_is_opened_through_search(self):
        result = self.send(room="둘", text="확인했습니다")
        self.assertStatus(result, "observed_in_window")
        self.assertEqual(self.ops.opens, ["둘"])
        self.assertTrue(result["openedWindow"])

    def test_unknown_room_and_room_without_history_are_never_typed_into(self):
        self.assertEqual(self.send(room="없는방")["reason"], "room_not_found")
        result = self.send(room="빈방", op="op-2")
        self.assertReason(result, "room_without_history")
        self.assertFalse(result["dispatched"])
        self.assertEqual(self.world.typed, [])
        self.assertEqual(self.answers, [])

    def test_dialog_mode_asks_once_before_typing(self):
        self.dialog_mode()
        self.assertStatus(self.send(), "observed_in_window")
        self.assertEqual(len(self.answers), 1)
        self.assertEqual(self.answers[0][:2], ("친구", "회의 자료 보냈어"))

    def test_declined_dialog_types_nothing_and_writes_no_journal(self):
        self.dialog_mode()
        with patch.object(sender.confirm, "ask_send", lambda *args, **kwargs: False):
            result = self.send()
        self.assertReason(result, "declined_by_user")
        self.assertEqual(self.world.typed, [])
        self.assertFalse(safety.journal_path("op-1").exists())

    def test_existing_draft_is_preserved(self):
        self.world.composer["친구"] = "쓰던 글"
        result = self.send()
        self.assertReason(result, "existing_draft_preserved")
        self.assertEqual(self.world.composer["친구"], "쓰던 글")

    def test_draft_typed_during_the_dialog_is_preserved(self):
        self.dialog_mode()

        def typing_human(room, text, notes=()):
            self.world.composer["친구"] = "사람이 입력 중"
            return True
        with patch.object(sender.confirm, "ask_send", typing_human):
            result = self.send()
        self.assertReason(result, "existing_draft_preserved")
        self.assertEqual(self.world.composer["친구"], "사람이 입력 중")

    def test_ignored_enter_removes_text_and_two_failures_stop_sending(self):
        self.world.enter = "ignored"
        first = self.send(op="a")
        self.assertStatus(first, "blocked")
        self.assertReason(first, "enter_not_accepted_text_removed")
        self.assertEqual(self.world.composer["친구"], "")
        self.allow_next_send()
        second = self.send(op="b", text="두 번째")
        self.assertTrue(second["safetyStop"]["tripped"])
        self.allow_next_send()
        third = self.send(op="c", text="세 번째")
        self.assertReason(third, "safety_stop_active")
        self.assertEqual(self.world.enters, 2)

    def test_pacing_gate_runs_before_the_dialog(self):
        self.dialog_mode()
        self.assertStatus(self.send(op="a"), "observed_in_window")
        blocked = self.send(op="b", text="곧바로 두 번째")
        self.assertReason(blocked, "send_interval")
        self.assertEqual(len(self.answers), 1)

    def test_require_self(self):
        self.assertEqual(self.send(room="친구", require_self=True)["reason"], "self_room_not_verified")
        result = self.send(room="나", op="self-1", require_self=True)
        self.assertStatus(result, "observed_in_window")
        with patch.dict(os.environ, {"KAKAOTALK_WIN_SELF_NAME": ""}):
            self.allow_next_send()
            self.assertEqual(self.send(room="나", op="self-2", text="또", require_self=True)["reason"],
                             "self_name_not_configured")

    def test_restriction_notice_trips_the_safety_stop(self):
        self.world.rooms["친구"].append({"kind": "unclassified", "text": "카카오톡 이용이 제한되었습니다."})
        result = self.send()
        self.assertReason(result, "restriction_notice_detected")
        self.assertTrue(safety.breaker_state()["tripped"])
        self.assertEqual(self.world.typed, [])

    def test_link_warning_is_reported(self):
        result = self.send(text="자료 https://example.com/a")
        self.assertTrue(result["warnings"])
        self.dialog_mode()
        self.allow_next_send()
        self.send(op="op-2", text="자료 https://example.com/b")
        self.assertTrue(self.answers[0][2])

    def test_unknown_outcome_is_rechecked_without_retyping(self):
        self.world.enter = "slow"
        result = self.send(op="slow-1")
        self.assertStatus(result, "outcome_unknown")
        self.assertEqual(self.world.enters, 1)
        # The message finally shows up.
        self.world.rooms["친구"].append(message("나", self.world.pending))
        checked = sender.SendEngine(self.ops).recheck("slow-1")
        self.assertStatus(checked, "observed_in_window")
        self.assertEqual(self.world.enters, 1)
        with self.assertRaises(ValueError):
            sender.SendEngine(self.ops).recheck("never-sent")

    def test_long_text_is_confirmed_from_the_shortened_view(self):
        long_text = "\n".join(f"{index}번 항목: 오늘 회의에서 정한 내용을 정리합니다." for index in range(40))
        self.assertGreater(len(long_text), 500)
        result = self.send(text=long_text)
        self.assertStatus(result, "observed_in_window")
        self.assertEqual(self.world.enters, 1)
        shown = self.world.rooms["친구"][-1]["text"]
        self.assertTrue(shown.endswith("...") and len(shown) == 503)

    def test_a_different_long_text_with_the_same_cut_is_not_accepted(self):
        prefix = "가" * 500
        self.world.rooms["친구"].append(message("나", prefix + "..."))  # an older, different long message
        self.world.enter = "slow"
        result = self.send(text=prefix + "다른 끝")
        self.assertStatus(result, "outcome_unknown")

    def test_same_text_already_in_history_is_not_mistaken_for_the_new_send(self):
        self.world.rooms["친구"].append(message("나", "회의 자료 보냈어"))
        self.world.enter = "slow"
        result = self.send()
        self.assertStatus(result, "outcome_unknown")

    def test_tail_alignment(self):
        items = [message("a", str(index)) for index in range(5)]
        tail = [sender.parser.fingerprint(item) for item in items[:3]]
        fresh, aligned = sender.new_after_tail(items, tail)
        self.assertTrue(aligned)
        self.assertEqual([item["text"] for item in fresh], ["3", "4"])
        self.assertEqual(sender.new_after_tail(items[3:], tail), (None, False))


if __name__ == "__main__":
    unittest.main()
