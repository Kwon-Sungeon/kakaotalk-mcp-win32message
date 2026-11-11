import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

if sys.platform != "win32":
    raise unittest.SkipTest("kakaotalk_win requires Windows")

from kakaotalk_win import people  # noqa: E402


def line(text, top, left=110):
    return {"text": text, "left": left, "top": top, "right": left + 60, "bottom": top + 20}


class Picker:
    """A fictional "대화상대 선택" dialog: a search box, friends, and the ticked count in its title."""

    FRIENDS = ["가상 친구", "가상친구2", "동명이인", "동명이인"]

    def __init__(self):
        self.query, self.ticked = "", []

    def lines(self, hwnd):
        shown = [name for name in self.FRIENDS if self.query in name] if self.query else self.FRIENDS
        title = "대화상대 선택" + (f" {len(self.ticked)}" if self.ticked else "")
        return [line(title, 60, 32)] + [line(name, 280 + 80 * at) for at, name in enumerate(shown)]

    def click(self, window, x, y):
        shown = [name for name in self.FRIENDS if self.query in name]
        self.ticked.append(shown[(y - 280) // 80])

    def type(self, hwnd, text):
        self.query = text


class PickTests(unittest.TestCase):
    def setUp(self):
        self.picker = Picker()
        fakes = [patch.object(people, "_lines", self.picker.lines), patch.object(people, "_click", self.picker.click),
                 patch.object(people, "_search_box", lambda window: 7),
                 patch.object(people.w, "set_control_text", self.picker.type),
                 patch.object(people.w, "window_dpi", lambda hwnd: unittest.mock.MagicMock()),
                 patch.object(people.w, "rect", lambda hwnd: (0, 150, 500, 200) if hwnd == 7 else (0, 0, 555, 900)),
                 patch.object(people.time, "sleep", lambda seconds: None)]
        for fake in fakes:
            fake.start()
            self.addCleanup(fake.stop)

    def test_exact_names_are_ticked(self):
        self.assertIsNone(people._pick(1, ["가상 친구", "가상친구2"]))
        self.assertEqual(self.picker.ticked, ["가상 친구", "가상친구2"])

    def test_unknown_or_shared_names_stop_before_anything_is_confirmed(self):
        self.assertEqual(people._pick(1, ["없는 사람"])["reason"], "friend_not_found")
        self.assertEqual(people._pick(1, ["동명이인"])["reason"], "several_friends_with_this_name")
        self.assertEqual(self.picker.ticked, [])

    def test_a_click_that_did_not_tick_is_noticed(self):
        with patch.object(people, "_click", lambda window, x, y: None):
            self.assertEqual(people._pick(1, ["가상 친구"])["reason"], "selection_not_confirmed")

    def test_names_argument(self):
        for bad in ([], "가상 친구", ["가상 친구", "가상 친구"], [""], ["x"] * 21):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                people.names(bad)

    def test_button_is_found_by_its_label(self):
        clicks = []
        with patch.object(people, "_lines", lambda hwnd: [line("취소", 840, 440), line("확 인", 840, 310)]), \
                patch.object(people, "_click", lambda window, x, y: clicks.append((x, y))):
            self.assertTrue(people._press(1, people.CONFIRM_LABELS))
            self.assertFalse(people._press(1, ("없는 단추",)))
        self.assertEqual(clicks, [(340, 850)])


class InviteTests(unittest.TestCase):
    def test_a_closed_room_is_not_invited_into(self):
        ops = SimpleNamespace(_find_chat=lambda title: (SimpleNamespace(chats=[]), None, None))
        with patch.object(people.safety, "process_lock"):
            self.assertEqual(people.invite(ops, "가상방", ["가상 친구"])["reason"], "room_window_not_open")


if __name__ == "__main__":
    unittest.main()
