import io
import json
import sys
import unittest
from unittest.mock import patch

if sys.platform != "win32":
    raise unittest.SkipTest("kakaotalk_win requires Windows")

from kakaotalk_win import cli, mcp_server  # noqa: E402
from kakaotalk_win.operations import _rows_from_ocr  # noqa: E402

EXPECTED = {"kakaotalk_win_status", "kakaotalk_win_windows", "kakaotalk_win_rooms", "kakaotalk_win_search_rooms",
            "kakaotalk_win_open", "kakaotalk_win_close", "kakaotalk_win_read", "kakaotalk_win_read_attachment",
            "kakaotalk_win_read_many", "kakaotalk_win_load_more", "kakaotalk_win_search", "kakaotalk_win_send",
            "kakaotalk_win_send_result"}


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
        for name in tools:
            self.assertNotIn("reset", name)
            self.assertNotIn("config", name)

    async def test_tools_delegate_to_operations(self):
        with patch("kakaotalk_win.operations.KakaoTalkWin") as ops:
            ops.return_value.read_messages.return_value = {"status": "ok", "items": []}
            self.assertEqual(json.loads(mcp_server.kakaotalk_win_read("방", 3))["status"], "ok")
            ops.return_value.read_messages.assert_called_once_with("방", 3, 0, "")
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


class OcrRowTests(unittest.TestCase):
    def test_rows_group_name_preview_time_and_unread(self):
        def line(text, left, top):
            return {"text": text, "left": left, "top": top, "right": left + 80, "bottom": top + 14}
        lines = [line("김철수", 70, 10), line("내일 봐요", 70, 32), line("오후 3:05", 300, 10), line("2", 330, 34),
                 line("프로젝트방", 70, 80), line("자료 올렸습니다", 70, 102), line("어제", 300, 80)]
        rows = _rows_from_ocr(lines, 380)
        self.assertEqual([(row["name"], row["preview"], row["timeText"], row["unread"]) for row in rows],
                         [("김철수", "내일 봐요", "오후 3:05", "2"), ("프로젝트방", "자료 올렸습니다", "어제", None)])

    def test_self_chat_badge_is_removed_but_kept_as_raw_name(self):
        rows = _rows_from_ocr([{"text": "0 홍길동", "left": 120, "top": 29, "right": 200, "bottom": 51}], 600)
        self.assertEqual((rows[0]["name"], rows[0]["rawName"]), ("홍길동", "0 홍길동"))


if __name__ == "__main__":
    unittest.main()
