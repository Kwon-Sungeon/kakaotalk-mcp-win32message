import io
import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

if sys.platform != "win32":
    raise unittest.SkipTest("kakaotalk_win requires Windows")

from kakaotalk_win import capture, cli, mcp_server  # noqa: E402
from kakaotalk_win.operations import _has_red_badge, _match_shortened, _merge_pages, _rows_from_ocr  # noqa: E402

EXPECTED = {"kakaotalk_win_status", "kakaotalk_win_windows", "kakaotalk_win_rooms", "kakaotalk_win_search_rooms",
            "kakaotalk_win_open", "kakaotalk_win_close", "kakaotalk_win_read", "kakaotalk_win_read_attachment",
            "kakaotalk_win_read_many", "kakaotalk_win_load_more", "kakaotalk_win_search", "kakaotalk_win_send",
            "kakaotalk_win_send_result", "kakaotalk_win_read_full_text", "kakaotalk_win_recent",
            "kakaotalk_win_create_room", "kakaotalk_win_invite"}


class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_set_and_annotations(self):
        tools = {tool.name: tool for tool in await mcp_server.mcp.list_tools()}
        self.assertEqual(set(tools), EXPECTED)
        read_only = {name for name, tool in tools.items() if tool.annotations.readOnlyHint}
        self.assertEqual(read_only, {"kakaotalk_win_status", "kakaotalk_win_windows", "kakaotalk_win_read",
                                     "kakaotalk_win_send_result"})
        send = tools["kakaotalk_win_send"]
        self.assertIn("operation_id", send.inputSchema["required"])
        self.assertFalse(send.annotations.idempotentHint)
        self.assertTrue(send.annotations.openWorldHint)
        for name, tool in tools.items():
            self.assertNotIn("reset", name)
            self.assertNotIn("config", name)
            self.assertIsNone(tool.outputSchema, name)  # replies are sent once, as text
        self.assertFalse(tools["kakaotalk_win_read_many"].inputSchema["properties"]["open_if_needed"]["default"])

    async def test_a_missing_app_is_a_blocked_reply_not_an_error(self):
        from kakaotalk_win import ui
        with patch("kakaotalk_win.operations.KakaoTalkWin") as ops:
            ops.return_value.windows.side_effect = ui.SeveralKakaoProcesses("two")
            self.assertEqual(json.loads(mcp_server.kakaotalk_win_windows())["reason"], "multiple_kakaotalk_processes")
            ops.return_value.windows.side_effect = ui.KakaoNotRunning("none")
            self.assertEqual(json.loads(mcp_server.kakaotalk_win_windows())["reason"], "kakaotalk_not_running")

    async def test_a_capture_is_deleted_even_when_attaching_it_fails(self):
        shot = capture.save_png((4, 4, b"\xff" * 64))
        with patch("kakaotalk_win.operations.KakaoTalkWin") as ops, patch.object(mcp_server, "Image") as image:
            image.return_value.to_image_content.side_effect = RuntimeError("cannot encode")
            ops.return_value.capture.return_value = {"status": "ok", "imagePath": shot}
            with self.assertRaises(RuntimeError):
                mcp_server.kakaotalk_win_read_attachment("방")
        self.assertFalse(os.path.exists(shot))

    async def test_tools_delegate_to_operations(self):
        with patch("kakaotalk_win.operations.KakaoTalkWin") as ops:
            ops.return_value.read_messages.return_value = {"status": "ok", "items": []}
            self.assertEqual(json.loads(mcp_server.kakaotalk_win_read("방", 3))["status"], "ok")
            ops.return_value.read_messages.assert_called_once_with("방", 3, 0, "", since="")
            ops.return_value.send_message.return_value = {"status": "blocked"}
            mcp_server.kakaotalk_win_send("방", "안녕", "op-1")
            ops.return_value.send_message.assert_called_once_with("방", "안녕", "op-1", 8, False)

    async def test_rooms_image_only_when_needed(self):
        with patch("kakaotalk_win.operations.KakaoTalkWin") as ops, patch.object(mcp_server, "Image") as image:
            image.return_value.to_image_content.return_value = mcp_server.ImageContent(
                type="image", data="AA==", mimeType="image/png")
            ops.return_value.rooms.return_value = {"status": "ok", "imagePath": "x.png", "ocr": {"status": "ok"}}
            self.assertEqual([part.type for part in mcp_server.kakaotalk_win_rooms()], ["text"])
            ops.return_value.rooms.return_value = {"status": "ok", "imagePath": "x.png",
                                                   "ocr": {"status": "ocr_failed"}}
            self.assertEqual([part.type for part in mcp_server.kakaotalk_win_rooms()], ["text", "image"])

    async def test_temporary_captures_are_deleted_after_the_reply(self):
        image = (4, 4, b"\xff" * 64)
        attached, hidden = capture.save_png(image), capture.save_png(image)
        with tempfile.TemporaryDirectory() as folder:
            chosen = os.path.join(folder, "mine.png")
            capture.save_png(image, chosen)
            with patch("kakaotalk_win.operations.KakaoTalkWin") as ops:
                ops.return_value.capture.return_value = {"status": "ok", "imagePath": attached}
                parts = mcp_server.kakaotalk_win_read_attachment("방")
                self.assertEqual([part.type for part in parts], ["text", "image"])  # read before deletion
                self.assertTrue(json.loads(parts[0].text)["temporaryImagesDeleted"])
                ops.return_value.rooms.return_value = {"status": "ok", "imagePath": hidden, "imagePaths": [hidden],
                                                       "ocr": {"status": "ok"}}
                reply = json.loads(mcp_server.kakaotalk_win_rooms()[0].text)
                self.assertEqual((reply["imagePath"], reply["imagePaths"]), (None, []))
                ops.return_value.capture.return_value = {"status": "ok", "imagePath": chosen}
                mcp_server.kakaotalk_win_read_attachment("방", chosen)
            self.assertFalse(os.path.exists(attached) or os.path.exists(hidden))
            self.assertTrue(os.path.exists(chosen))  # a file the caller asked for is kept


class CaptureCleanupTests(unittest.TestCase):
    def test_only_own_temporary_captures_count(self):
        path = capture.save_png((2, 2, b"\x00" * 16))
        try:
            self.assertTrue(capture.is_temporary(path))
            self.assertFalse(capture.is_temporary(os.path.join(os.path.dirname(path), "other.png")))
            self.assertFalse(capture.is_temporary(r"C:\somewhere\kakaotalk-win-x.png"))
        finally:
            capture.discard([path])
        self.assertFalse(os.path.exists(path))

    def test_old_captures_are_swept(self):
        old, new = capture.save_png((2, 2, b"\x00" * 16)), capture.save_png((2, 2, b"\x00" * 16))
        os.utime(old, (time.time() - 7200, time.time() - 7200))
        capture.sweep_temporary()
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(new))
        capture.discard([new])


class CliTests(unittest.TestCase):
    def run_cli(self, *argv):
        stream = io.StringIO()
        stream.reconfigure = lambda **kwargs: None
        with patch("sys.stdout", stream):
            code = cli.main(list(argv))
        return code, json.loads(stream.getvalue())

    def test_safety_loosening_needs_an_interactive_human(self):
        from kakaotalk_win import safety
        with patch.object(safety, "breaker_state", return_value={"tripped": True, "reason": "x"}), \
                patch.object(safety, "reset_breaker") as reset:
            code, value = self.run_cli("reset-stop")
        self.assertEqual(code, 1)
        self.assertEqual(value["errorType"], "PermissionError")
        reset.assert_not_called()
        with patch.object(safety, "save_config") as save:
            code, value = self.run_cli("config", "--confirm", "off")
        self.assertEqual(value["errorType"], "PermissionError")
        save.assert_not_called()
        with patch.object(safety, "save_config", return_value={"confirmSend": "dialog"}) as save, \
                patch.object(safety, "load_config", return_value={}):
            code, value = self.run_cli("config", "--confirm", "dialog")  # tightening needs no terminal
        self.assertEqual(code, 0)
        save.assert_called_once_with({"confirmSend": "dialog"})

    def test_unknown_limit_is_rejected(self):
        code, value = self.run_cli("config", "--limit", "per_week=3")
        self.assertEqual(code, 1)
        self.assertIn("Unknown limit", value["detail"])


def line(text, left, top, width=200):
    return {"text": text, "left": left, "top": top, "right": left + width, "bottom": top + 17}


def blank(width=651, height=815):
    return width, height, b""


class OcrRowTests(unittest.TestCase):
    """Fictional rooms laid out like a 144 DPI capture of KakaoTalk PC 26.9.2 (rows about 105 px apart)."""

    def test_rows_group_name_members_preview_time_and_unread(self):
        lines = [line("9", 30, 21, 40),                                   # profile picture: ignored
                 line("주말 등산 모임 3", 121, 32), line("오후 2:12", 550, 34, 67),
                 line("좋아요!!", 122, 59),
                 line("OAB 프로젝트방", 120, 240), line("4", 322, 245, 10), line("오후 1:56", 550, 244, 67),
                 line("네 확인했습니다", 122, 269), line("2", 593, 270, 25),
                 line("김철수", 121, 442), line("오후 1:03", 550, 454, 67),
                 line("자료 정리해서 내일 오전에 공유", 122, 469), line("드릴게요.", 122, 491),
                 line("이영희 %", 121, 556), line("어제", 580, 559, 30), line("그렇구나......", 122, 583)]
        rows = _rows_from_ocr(lines, blank())
        self.assertEqual([(row["name"], row["memberCount"], row["preview"], row["timeText"], row["unread"])
                          for row in rows],
                         [("주말 등산 모임", 3, "좋아요!!", "오후 2:12", None),
                          ("OAB 프로젝트방", 4, "네 확인했습니다", "오후 1:56", "2"),
                          ("김철수", None, "자료 정리해서 내일 오전에 공유 드릴게요.", "오후 1:03", None),
                          ("이영희", None, "그렇구나......", "어제", None)])
        self.assertEqual(rows[1]["nameVariants"], ["AB 프로젝트방", "OAB 프로젝트방 4"])

    def test_self_chat_badge_is_removed_but_kept_as_raw_name(self):
        rows = _rows_from_ocr([line("0 홍길동", 120, 29), line("메모", 122, 56)], blank())
        self.assertEqual((rows[0]["name"], rows[0]["rawName"]), ("홍길동", "0 홍길동"))

    def test_name_and_time_read_as_one_line_are_split(self):
        def word(text, left, width):
            return {"text": text, "left": left, "top": 32, "right": left + width, "bottom": 49}
        joined = {"text": "사진 동아리 공지방 385 오후 226", "left": 121, "top": 32, "right": 617, "bottom": 49,
                  "words": [word("사진", 121, 35), word("동아리", 161, 50), word("공지방", 216, 45),
                            word("385", 267, 30), word("오후", 550, 30), word("226", 585, 32)]}
        rows = _rows_from_ocr([joined, line("*모집이 마감되었습니다!", 122, 59)], blank())
        self.assertEqual((rows[0]["name"], rows[0]["memberCount"], rows[0]["timeText"]),
                         ("사진 동아리 공지방", 385, "오후 2:26"))

    def test_rows_cut_by_the_edges(self):
        lines = [line("미리보기만 보이는 줄", 122, 2), line("가운데 방", 121, 120), line("내용", 122, 147),
                 line("아래 잘린 방", 121, 790)]
        middle = _rows_from_ocr(lines, blank(), last_page=False)
        self.assertEqual([row["name"] for row in middle], ["가운데 방"])
        last = _rows_from_ocr(lines, blank(), last_page=True)
        self.assertEqual([(row["name"], row.get("partial")) for row in last],
                         [("가운데 방", None), ("아래 잘린 방", True)])

    def test_red_badge_is_detected_from_pixels(self):
        width, height = 100, 40
        pixels = bytearray(b"\xff\xff\xff\x00" * width * height)
        for y in range(10, 30):
            for x in range(88, 98):
                pixels[(y * width + x) * 4:(y * width + x) * 4 + 4] = bytes((97, 111, 255, 0))  # BGRA salmon red
        self.assertTrue(_has_red_badge((width, height, bytes(pixels)), 0, 40))
        self.assertFalse(_has_red_badge((width, height, bytes(b"\xff" * width * height * 4)), 0, 40))

    def test_overlapping_pages_are_merged(self):
        first = [{"name": "가", "timeText": "오후 1:00", "preview": "a", "unread": None, "memberCount": None},
                 {"name": "나", "timeText": None, "preview": None, "unread": None, "memberCount": None,
                  "partial": True}]
        second = [{"name": "나", "timeText": "오후 12:00", "preview": "b", "unread": None, "memberCount": None},
                  {"name": "다", "timeText": "어제", "preview": "c", "unread": None, "memberCount": None}]
        merged = _merge_pages([first, second])
        self.assertEqual([(row["name"], row["preview"], row["position"]) for row in merged],
                         [("가", "a", 0), ("나", "b", 1), ("다", "c", 2)])


class LongMessageMatchTests(unittest.TestCase):
    def test_full_text_is_matched_by_shown_prefix_and_sender(self):
        shown = "가나다 " * 125
        targets = [{"index": 3, "sender": "나", "text": shown[:500] + "..."},
                   {"index": 9, "sender": "친구", "text": "다른 " * 200 + "..."}]
        full = shown + "그리고 끝"
        self.assertEqual(_match_shortened(targets, {}, full, "나")["index"], 3)
        self.assertIsNone(_match_shortened(targets, {}, full, "친구"))
        self.assertIsNone(_match_shortened(targets, {3: full}, full, "나"))
        self.assertIsNone(_match_shortened(targets, {}, None, "나"))


if __name__ == "__main__":
    unittest.main()
