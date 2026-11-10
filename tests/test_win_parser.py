import unittest

from kakaotalk_win import parser

KOREAN = ("2026년 11월 9일 일요일\r\n"
          "[김철수] [오후 3:05] 첫 줄\n둘째 줄\r\n"
          "[이영희] [오전 12:01] 사진\r\n"
          "이영희님이 들어왔습니다.\r\n"
          "2026년 11월 10일 월요일\r\n"
          "[김철수] [오후 12:30] 삭제된 메시지입니다.\r\n"
          "[나] [오전 9:07] 사진 3장\r\n"
          "[나] [오전 9:08] 파일: 보고서.pdf\r\n"
          "무언가 알 수 없는 줄\r\n")


class ParserTests(unittest.TestCase):
    def test_korean_records_dates_and_multiline_bodies(self):
        items = parser.parse(KOREAN, self_name="나")
        self.assertEqual([item["kind"] for item in items],
                         ["text", "media", "system", "deleted", "media", "media", "unclassified"])
        first = items[0]
        self.assertEqual((first["sender"], first["time"], first["date"]), ("김철수", "15:05", "2026-11-09"))
        self.assertEqual(first["text"], "첫 줄\n둘째 줄")
        self.assertFalse(first["fromSelf"])
        self.assertEqual(items[1]["time"], "00:01")
        self.assertEqual(items[1]["mediaType"], "photo")
        self.assertEqual(items[3]["date"], "2026-11-10")
        self.assertEqual(items[3]["time"], "12:30")
        self.assertEqual(items[4]["mediaType"], "photo")
        self.assertEqual(items[5]["mediaType"], "file")
        self.assertTrue(items[5]["fromSelf"])
        self.assertEqual(items[6]["text"], "무언가 알 수 없는 줄")
        self.assertEqual([item["index"] for item in items], list(range(len(items))))

    def test_english_ui(self):
        raw = "Monday, November 10, 2026\r\n[Kim] [3:05 PM] hello\r\n[Lee] [12:10 AM] Photo\r\n"
        items = parser.parse(raw)
        self.assertEqual(items[0]["date"], "2026-11-10")
        self.assertEqual(items[0]["time"], "15:05")
        self.assertEqual(items[1]["time"], "00:10")
        self.assertEqual(items[1]["kind"], "media")
        self.assertNotIn("fromSelf", items[0])

    def test_text_that_only_looks_like_media_stays_text(self):
        items = parser.parse("[A] [오후 1:00] 사진 보내줘\r\n")
        self.assertEqual(items[0]["kind"], "text")

    def test_brackets_inside_body_and_sender(self):
        items = parser.parse("[[공지] 운영팀] [오후 1:00] [중요] 내일 회의\r\n")
        self.assertEqual(items[0]["sender"], "[공지] 운영팀")
        self.assertEqual(items[0]["text"], "[중요] 내일 회의")

    def test_empty_and_snapshot(self):
        self.assertEqual(parser.parse(""), [])
        self.assertEqual(parser.snapshot_id("a"), parser.snapshot_id("a"))
        self.assertNotEqual(parser.snapshot_id("a"), parser.snapshot_id("b"))
        self.assertEqual(len(parser.snapshot_id("a")), 24)

    def test_to_24h(self):
        self.assertEqual(parser.to_24h("오전 12:00"), "00:00")
        self.assertEqual(parser.to_24h("오후 12:00"), "12:00")
        self.assertEqual(parser.to_24h("오후 11:59"), "23:59")
        self.assertEqual(parser.to_24h("9:15 AM"), "09:15")
        self.assertIsNone(parser.to_24h("25:00"))
        self.assertIsNone(parser.to_24h("어제"))


if __name__ == "__main__":
    unittest.main()
