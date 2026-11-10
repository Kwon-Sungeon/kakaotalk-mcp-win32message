import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

if sys.platform != "win32":
    raise unittest.SkipTest("kakaotalk_win requires Windows")

from kakaotalk_win import safety  # noqa: E402


class StateTestCase(unittest.TestCase):
    def setUp(self):
        self.state_dir = Path(tempfile.mkdtemp(prefix="kakaotalk-win-test-"))
        self.addCleanup(shutil.rmtree, self.state_dir, ignore_errors=True)
        patcher = patch.object(safety, "STATE_ROOT", self.state_dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        environment = patch.dict("os.environ", {}, clear=False)
        environment.start()
        self.addCleanup(environment.stop)
        for name in ("KAKAOTALK_WIN_CONFIRM", "KAKAOTALK_WIN_SELF_NAME"):
            __import__("os").environ.pop(name, None)


class LimitTests(StateTestCase):
    def test_configuration_can_only_tighten(self):
        safety.save_config({"limits": {"per_hour": 500, "per_day": 5, "min_interval_seconds": 1,
                                       "duplicate_window_hours": 48, "opens_per_minute": True}})
        limits = safety.limits()
        self.assertEqual(limits["per_hour"], 20)
        self.assertEqual(limits["per_day"], 5)
        self.assertEqual(limits["min_interval_seconds"], 10.0)
        self.assertEqual(limits["duplicate_window_hours"], 48.0)
        self.assertEqual(limits["opens_per_minute"], safety.CEILINGS["opens_per_minute"])

    def test_confirm_defaults_to_off_and_unknown_values_fail_closed(self):
        self.assertEqual(safety.confirm_mode(), "off")
        safety.save_config({"confirmSend": "dialog"})
        self.assertEqual(safety.confirm_mode(), "dialog")
        safety.save_config({"confirmSend": "nonsense"})
        self.assertEqual(safety.confirm_mode(), "dialog")


class GateTests(StateTestCase):
    def test_interval_and_caps(self):
        now = 1_000_000.0
        self.assertIsNone(safety.check_send_allowed("방", "안녕", now))
        safety.record_send("방", "안녕", now)
        self.assertEqual(safety.check_send_allowed("방", "또", now + 5)["reason"], "send_interval")
        self.assertIsNone(safety.check_send_allowed("방", "또", now + 11))
        for index in range(1, 20):
            safety.record_send(f"방{index % 9}", f"m{index}", now + 11 * index)
        self.assertEqual(safety.check_send_allowed("방", "x", now + 11 * 21)["reason"], "hourly_send_limit")

    def test_room_and_distinct_room_caps(self):
        now = 2_000_000.0
        for index in range(10):
            safety.record_send("한방", f"m{index}", now + 11 * index)
        self.assertEqual(safety.check_send_allowed("한방", "x", now + 200)["reason"], "room_hourly_send_limit")
        safety.reset_breaker()
        later = now + 7200
        for index in range(10):
            safety.record_send(f"room{index}", f"n{index}", later + 11 * index)
        self.assertEqual(safety.check_send_allowed("새방", "x", later + 200)["reason"], "too_many_rooms_per_hour")
        self.assertIsNone(safety.check_send_allowed("room3", "x", later + 200))

    def test_daily_cap(self):
        now = 3_000_000.0
        for index in range(100):
            safety.record_send(f"r{index % 5}", f"d{index}", now - 80000 + 700 * index)
        self.assertEqual(safety.check_send_allowed("r1", "x", now + 100)["reason"], "daily_send_limit")

    def test_no_broadcast_of_long_or_linked_text(self):
        now = 4_000_000.0
        long_text = "이번 주 금요일 오후 세 시에 전체 회의가 있습니다. 자료를 미리 확인해 주세요."
        safety.record_send("A", long_text, now)
        blocked = safety.check_send_allowed("B", "  " + long_text.replace(" ", "  "), now + 60)
        self.assertEqual(blocked["reason"], "same_text_sent_to_another_room")
        self.assertIsNone(safety.check_send_allowed("A", long_text, now + 60))
        self.assertIsNone(safety.check_send_allowed("B", long_text, now + 86400 + 60))
        safety.record_send("A", "네 감사합니다", now + 100)
        self.assertIsNone(safety.check_send_allowed("B", "네 감사합니다", now + 200))
        safety.record_send("A", "https://ex.am/p", now + 300)
        self.assertEqual(safety.check_send_allowed("B", "https://ex.am/p", now + 400)["reason"],
                         "same_text_sent_to_another_room")

    def test_breaker_and_text_length(self):
        self.assertEqual(safety.check_send_allowed("A", "x" * 5001, 1.0)["reason"], "text_too_long")
        self.assertIsNone(safety.note_unconfirmed_send())
        self.assertTrue(safety.note_unconfirmed_send()["tripped"])
        self.assertEqual(safety.check_send_allowed("A", "x", 1.0)["reason"], "safety_stop_active")
        safety.reset_breaker()
        safety.note_confirmed_send()
        self.assertIsNone(safety.check_send_allowed("A", "x", 1.0))
        self.assertIsNone(safety.note_unconfirmed_send())

    def test_open_rate(self):
        for index in range(6):
            safety.record_open(100.0 + index)
        self.assertEqual(safety.check_open_allowed(110.0)["reason"], "open_rate_limit")
        self.assertIsNone(safety.check_open_allowed(170.0))


class MiscTests(StateTestCase):
    def test_restriction_keywords_ignore_open_chat_policy_banner(self):
        self.assertFalse(safety.mentions_restriction(
            "운영정책을 위반한 메시지로 신고 접수 시 카카오톡 이용에 제한이 있을 수 있습니다."))
        self.assertTrue(safety.mentions_restriction("고객님의 계정은 보호조치 되었습니다."))
        self.assertTrue(safety.mentions_restriction("카카오톡 이용이 제한되었습니다."))
        self.assertFalse(safety.mentions_restriction(None))

    def test_journal_ids(self):
        self.assertTrue(str(safety.journal_path("req-1:a.b_c")).endswith(".json"))
        for bad in ("", "a/b", "공백 있음", "x" * 129, None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                safety.journal_path(bad)

    def test_lock_is_reentrant_and_exclusive_across_handles(self):
        with safety.process_lock("t"):
            with safety.process_lock("t"):
                pass
        with safety.process_lock("t"):
            pass

    def test_pacing_log_stores_hashes_only(self):
        safety.record_send("비밀방", "비밀 본문", 10.0)
        stored = (safety.STATE_ROOT / "send-log.json").read_text(encoding="utf-8")
        self.assertNotIn("비밀", stored)


if __name__ == "__main__":
    unittest.main()
