import sys
import unittest
from unittest.mock import MagicMock, patch

if sys.platform != "win32":
    raise unittest.SkipTest("kakaotalk_win requires Windows")

from kakaotalk_win import clipboard  # noqa: E402

KAKAO_PID = 4321


class _Open:
    def __init__(self, timeout=1.0):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class CaptureTextTests(unittest.TestCase):
    def setUp(self):
        self.saved = clipboard.Snapshot([(clipboard.CF_UNICODETEXT, "global", "사용자 원본\0".encode("utf-16-le"))],
                                        [], 5)
        self.counter = [5]
        for name, value in (("snapshot", MagicMock(return_value=self.saved)),
                            ("sequence", lambda: self.counter[0]), ("_Open", _Open),
                            ("owner_process", MagicMock(return_value=KAKAO_PID)),
                            ("read_unicode", MagicMock(return_value="가상방 대화")),
                            ("restore", MagicMock(return_value={"restored": True})),
                            ("_restore_if_ours", MagicMock())):
            patcher = patch.object(clipboard, name, value)
            setattr(self, name.strip("_"), patcher.start())
            self.addCleanup(patcher.stop)

    def copy(self, _changed):
        self.counter[0] += 1  # the app wrote to the clipboard

    def test_read_failure_after_the_apps_write_restores(self):
        self.read_unicode.side_effect = OSError("GlobalLock failed")
        with self.assertRaises(OSError):
            clipboard.capture_text(self.copy, KAKAO_PID)
        self.restore_if_ours.assert_called_once_with(self.saved, KAKAO_PID)

    def test_clipboard_busy_after_the_apps_write_restores(self):
        with patch.object(clipboard, "_Open", MagicMock(side_effect=clipboard.ClipboardBusy("busy"))):
            with self.assertRaises(clipboard.ClipboardBusy):
                clipboard.capture_text(self.copy, KAKAO_PID)
        self.restore_if_ours.assert_called_once_with(self.saved, KAKAO_PID)

    def test_foreign_owner_wins_and_is_not_restored(self):
        self.owner_process.return_value = 999
        with self.assertRaises(clipboard.ClipboardConflict):
            clipboard.capture_text(self.copy, KAKAO_PID)
        self.restore_if_ours.assert_not_called()
        self.restore.assert_not_called()
        self.read_unicode.assert_not_called()

    def test_successful_capture_restores_once(self):
        text, info = clipboard.capture_text(self.copy, KAKAO_PID)
        self.assertEqual((text, info["copied"], info["restore"]), ("가상방 대화", True, {"restored": True}))
        self.restore.assert_called_once_with(self.saved, 6)
        self.restore_if_ours.assert_not_called()


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.user32 = MagicMock()
        self.user32.GetClipboardSequenceNumber.return_value = 7
        self.user32.EmptyClipboard.return_value = 1
        self.user32.GetClipboardFormatNameW.return_value = 0
        for target, name, value in ((clipboard.w, "user32", self.user32), (clipboard.w, "kernel32", MagicMock()),
                                    (clipboard, "_owner_window", lambda: 1),
                                    (clipboard, "_global_from_bytes", lambda data: 99)):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.saved = clipboard.Snapshot([(clipboard.CF_UNICODETEXT, "global", b"a\0\0\0"),
                                         (clipboard.CF_DIB, "global", b"\0" * 8)], ["format_49999"], 7)

    def test_formats_that_could_not_be_set_are_reported(self):
        self.user32.SetClipboardData.side_effect = lambda fmt, handle: 0 if fmt == clipboard.CF_DIB else handle
        result = clipboard.restore(self.saved, 7)
        self.assertEqual(result, {"restored": True, "formats": 2, "unpreservedFormats": ["format_49999"],
                                  "failedFormats": ["CF_DIB"]})

    def test_nothing_written_is_not_restored(self):
        self.user32.SetClipboardData.return_value = 0
        result = clipboard.restore(self.saved, 7)
        self.assertFalse(result["restored"])
        self.assertEqual(result["failedFormats"], ["CF_UNICODETEXT", "CF_DIB"])

    def test_empty_clipboard_failure_is_not_restored(self):
        self.user32.EmptyClipboard.return_value = 0
        self.assertFalse(clipboard.restore(self.saved, 7)["restored"])
        self.user32.SetClipboardData.assert_not_called()

    def test_full_success_has_no_failed_formats(self):
        self.user32.SetClipboardData.side_effect = lambda fmt, handle: handle
        self.assertEqual(clipboard.restore(self.saved, 7),
                         {"restored": True, "formats": 2, "unpreservedFormats": ["format_49999"]})


if __name__ == "__main__":
    unittest.main()
