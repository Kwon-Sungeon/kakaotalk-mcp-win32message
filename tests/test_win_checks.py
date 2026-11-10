import os
import sys
import tempfile
import unittest

if sys.platform != "win32":
    raise unittest.SkipTest("kakaotalk_win requires Windows")

from kakaotalk_win import checks  # noqa: E402
from kakaotalk_win.operations import _newest_page  # noqa: E402


class ArgumentCheckTests(unittest.TestCase):
    def test_numbers_reject_booleans_floats_and_out_of_range(self):
        self.assertEqual(checks.number(5, "limit", 1, 10), 5)
        for bad in (True, 2.0, "3", 0, 11):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "^limit:"):
                checks.number(bad, "limit", 1, 10)

    def test_flags_and_seconds(self):
        self.assertIs(checks.flag(False, "ocr"), False)
        with self.assertRaises(ValueError):
            checks.flag(0, "ocr")
        self.assertEqual(checks.seconds(3), 3.0)
        for bad in (True, -1, 21, "1"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                checks.seconds(bad)

    def test_room_names(self):
        self.assertEqual(checks.room("팀 공지"), "팀 공지")
        for bad in ("", "   ", None, "x" * 513):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                checks.room(bad)
        self.assertEqual(checks.rooms(["a", "b"], most=10), ["a", "b"])
        for bad in ([], ["a", "a"], "a", ["a"] * 11):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                checks.rooms(bad, most=10)

    def test_snapshot_is_needed_only_after_the_first_page(self):
        self.assertEqual(checks.snapshot("", 0), "")
        self.assertEqual(checks.snapshot("a" * 24, 50), "a" * 24)
        for value, offset in (("", 50), ("A" * 24, 0), ("abc", 0), (None, 0)):
            with self.subTest(value=value, offset=offset), self.assertRaises(ValueError):
                checks.snapshot(value, offset)

    def test_operation_ids(self):
        self.assertEqual(checks.operation_id("reply-2026.11.10:1_a"), "reply-2026.11.10:1_a")
        for bad in ("", "x" * 129, "공지", "a b", "../x", 7):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                checks.operation_id(bad)

    def test_new_file_must_be_absolute_and_unused(self):
        self.assertIsNone(checks.new_file(None))
        with self.assertRaises(ValueError):
            checks.new_file("relative.png")
        with tempfile.TemporaryDirectory() as folder:
            fresh = os.path.join(folder, "shot.png")
            self.assertEqual(checks.new_file(fresh), fresh)
            open(fresh, "wb").close()
            with self.assertRaises(ValueError):
                checks.new_file(fresh)


class PagingTests(unittest.TestCase):
    def test_pages_walk_back_from_the_newest_message(self):
        items = list(range(10))
        self.assertEqual(_newest_page(items, 4, 0), ([6, 7, 8, 9], 4))
        self.assertEqual(_newest_page(items, 4, 4), ([2, 3, 4, 5], 8))
        self.assertEqual(_newest_page(items, 4, 8), ([0, 1], None))
        self.assertEqual(_newest_page(items, 50, 0), (items, None))
        self.assertEqual(_newest_page(items, 4, 99), ([], None))


if __name__ == "__main__":
    unittest.main()
