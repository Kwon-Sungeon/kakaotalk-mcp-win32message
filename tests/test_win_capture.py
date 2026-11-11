import json
import os
import queue
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

if sys.platform != "win32":
    raise unittest.SkipTest("kakaotalk_win requires Windows")

from kakaotalk_win import capture  # noqa: E402

IMAGE = (2, 2, b"\x00" * 16)


class TemporaryCaptureTests(unittest.TestCase):
    def test_callers_file_named_like_a_capture_is_kept(self):
        chosen = os.path.join(tempfile.gettempdir(), f"{capture.TEMP_PREFIX}{uuid.uuid4().hex}.png")
        capture.save_png(IMAGE, chosen)
        self.addCleanup(os.remove, chosen)
        self.assertFalse(capture.is_temporary(chosen))
        self.assertEqual(capture.discard([chosen]), 0)
        self.assertTrue(os.path.exists(chosen))

    def test_own_temporary_capture_is_discarded_once(self):
        path = capture.save_png(IMAGE)
        self.assertTrue(capture.is_temporary(path))
        self.assertEqual(capture.discard([path, None]), 1)
        self.assertFalse(os.path.exists(path))
        self.assertFalse(capture.is_temporary(path))
        self.assertEqual(capture.discard([path]), 0)


class _Stdout:
    def __init__(self):
        self.lines = queue.Queue()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def __iter__(self):
        while (line := self.lines.get()) is not None:
            yield line


class _Stdin:
    def __init__(self, process):
        self.process = process

    def write(self, data):
        if self.process.dead:
            raise BrokenPipeError
        self.process.requests.append(json.loads(data))
        if self.process.replies:
            self.process.stdout.lines.put(self.process.replies.pop(0))

    def flush(self):
        pass

    def close(self):
        pass


class _FakeProcess:
    """Stands in for the PowerShell OCR loop; `replies` are the stdout lines it gives, one per request."""

    def __init__(self, replies=(), dead=False):
        self.replies, self.dead, self.killed, self.requests = list(replies), dead, False, []
        self.stdout, self.stdin = _Stdout(), _Stdin(self)

    def kill(self):
        self.killed = True
        self.stdout.lines.put(None)

    def wait(self):
        return 1


def _reply(text="가상방", left=40):
    line = {"text": text, "left": left, "top": 10, "right": left + 40, "bottom": 30,
            "words": {"text": text, "left": left, "top": 10, "right": left + 40, "bottom": 30}}
    return ("﻿" + json.dumps({"status": "ok", "language": "ko", "lines": line}) + "\r\n").encode("utf-8")


class OcrSessionTests(unittest.TestCase):
    def setUp(self):
        once = patch.object(capture, "_ocr_once", return_value={"status": "ok", "language": "ko", "lines": []})
        self.once = once.start()
        self.addCleanup(once.stop)

    def popen(self, *processes):
        patcher = patch.object(capture.subprocess, "Popen", side_effect=list(processes))
        self.addCleanup(patcher.stop)
        return patcher.start()

    def test_requests_share_one_process_until_the_outermost_block_ends(self):
        process = _FakeProcess([_reply(), _reply("Room", left=80)])
        popen = self.popen(process)
        with capture.ocr_session():
            first = capture.ocr("가상방.png")
            with capture.ocr_session():
                second = capture.ocr("b.png", "en", zoom=2)
            self.assertFalse(process.killed)
        self.assertTrue(process.killed)
        self.assertEqual(popen.call_count, 1)
        self.once.assert_not_called()
        self.assertEqual(first["lines"][0]["text"], "가상방")
        self.assertEqual((second["lines"][0]["left"], second["lines"][0]["words"][0]["left"]), (40, 40))
        self.assertEqual([(r["lang"], r["zoom"]) for r in process.requests], [("ko", 1), ("en", 2)])
        self.assertTrue(process.requests[0]["image"].endswith("가상방.png"))

    def test_timeout_falls_back_and_the_next_call_starts_a_fresh_process(self):
        silent, fresh = _FakeProcess(), _FakeProcess([_reply()])
        popen = self.popen(silent, fresh)
        with patch.object(capture, "OCR_TIMEOUT", 0.05), capture.ocr_session():
            self.assertEqual(capture.ocr("a.png"), {"status": "ok", "language": "ko", "lines": []})
            self.assertTrue(silent.killed)
            self.assertEqual(capture.ocr("b.png")["lines"][0]["text"], "가상방")
        self.assertEqual(popen.call_count, 2)
        self.assertEqual(self.once.call_count, 1)

    def test_dead_process_or_failed_start_falls_back(self):
        self.popen(_FakeProcess(dead=True), OSError("no powershell"))
        with capture.ocr_session():
            capture.ocr("a.png")
            capture.ocr("b.png")
        self.assertEqual(self.once.call_count, 2)

    def test_outside_a_session_each_call_is_one_shot(self):
        popen = self.popen()
        capture.ocr("a.png")
        popen.assert_not_called()
        self.once.assert_called_once()


if __name__ == "__main__":
    unittest.main()
