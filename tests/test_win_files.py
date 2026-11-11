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

from kakaotalk_win import files, safety, sender  # noqa: E402
from test_win_sender import FakeChat, FakeOps, World  # noqa: E402


def media(sender_name, text, kind="file"):
    return {"kind": "media", "mediaType": kind, "sender": sender_name, "text": text,
            "date": time.strftime("%Y-%m-%d"), "time": time.strftime("%H:%M")}


def line(text, top, left=325, words=None):
    return {"text": text, "left": left, "top": top, "right": left + 200, "bottom": top + 18,
            "words": [{"text": word, "left": left + 60 * at, "top": top, "right": left + 60 * at + 40,
                       "bottom": top + 18} for at, word in enumerate(words or text.split())]}


def bubble(name, top, link="저장 • 다른 이름으로 저장", size="용량: 24bytes"):
    return [line(name, top), line("유효기간: ~2026.11.25.", top + 33), line(size, top + 60), line(link, top + 92)]


class Scratch(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="kakaotalk-win-test-"))
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)

    def file(self, name, content=b"x" * 24, folder=None):
        path = (folder or self.folder) / name
        path.write_bytes(content)
        return str(path)


class ArgumentTests(Scratch):
    def test_paths(self):
        good = self.file("a.txt")
        self.assertEqual(files.paths([good]), [os.path.normpath(good)])
        (self.folder / "more").mkdir()
        twin = self.file("A.TXT", folder=self.folder / "more")
        for bad in ([], "a.txt", ["a.txt"], [str(self.folder / "missing.txt")], [self.file("empty.txt", b"")],
                    [good, twin], [str(self.folder)], [good] * 11):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                files.paths(bad)

    def test_names_and_sizes(self):
        self.assertEqual(files.name_of("파일: 시험 문서 (가상).txt"), "시험 문서 (가상).txt")
        self.assertIsNone(files.name_of("사진"))
        self.assertTrue(files._saved_as("a (1).txt", "a.txt"))
        self.assertFalse(files._saved_as("ab.txt", "a.txt"))
        self.assertTrue(files._size_fits(60_000_000, "용량: 57.2MB"))
        self.assertTrue(files._size_fits(105_960, "용량: 104KB"))
        self.assertFalse(files._size_fits(48, "용량: 24bytes"))
        self.assertFalse(files._size_fits(2_200_000, "용량: 1.2MB"))  # another file of this name
        self.assertFalse(files._size_fits(110_000, "용량: 104KB"))
        self.assertTrue(files._size_fits(48, "읽지 못한 글자"))

    def test_bubble_titles(self):
        self.assertTrue(files._shows("가상 자료-1.csv", "가상_자료-1.csv"))  # OCR drops the underscore
        self.assertFalse(files._shows("보고서 1.pdf", "보고서 2.pdf"))
        self.assertFalse(files._shows("data.txt", "a.txt"))  # too short to take a line above for granted
        self.assertTrue(files._shows("가상 친구견적서.pdf", "견적서.pdf"))   # the sender's name read above the title
        with self.assertRaises(ValueError):
            files.paths([self.file("바로가기.lnk")])


class BubbleTests(unittest.TestCase):
    def test_bubbles_are_read_newest_first_with_their_links(self):
        lines = [line("안녕하세요", 100, left=40)] + bubble("가상 보고서.pdf", 200) \
            + bubble("긴 이름의", 400, link="열기 • 폴더 열기")[:1] + [line("문서.txt", 427)] \
            + bubble("x", 427, link="열기 • 폴더 열기")[1:]
        found = files._bubbles(lines)
        self.assertEqual([item["name"] for item in found], ["긴 이름의문서.txt", "가상 보고서.pdf"])
        self.assertTrue(files._labelled(found[0]["link"], files.SAVED_LABELS))
        self.assertTrue(files._labelled(found[1]["link"], files.SAVE_LABELS))
        self.assertEqual(found[1]["size"], "용량: 24bytes")

    def test_a_bubble_without_a_link_has_none(self):
        self.assertIsNone(files._bubbles(bubble("만료된 파일.zip", 200)[:3])[0]["link"])


class DispatchTests(unittest.TestCase):
    """The file dialog and the app's send box, as fake windows."""

    def run_dispatch(self, box_lines, found=("C:\\가상\\a.txt",)):
        self.clicks, self.closed, self.typed = [], [], {}
        chat = SimpleNamespace(hwnd=1, composer=2)
        fakes = [patch.object(files, "_owned_window", lambda pid, before, classes, owner, timeout=3.0:
                              10 if files.DIALOG_CLASS in classes else 20),
                 patch.object(files, "_search_box", lambda window: 11),
                 patch.object(files, "_lines", lambda window: box_lines),
                 patch.object(files, "_click", lambda window, x, y: self.clicks.append(window)),
                 patch.object(files, "_close", self.closed.append),
                 patch.object(files, "_gone", lambda window, timeout=3.0: True),
                 patch.object(files.w, "top_windows", lambda: []),
                 patch.object(files.w, "post_chord", lambda *args, **kwargs: None),
                 patch.object(files.w, "post", lambda *args: None),
                 patch.object(files.w, "set_control_text", self.typed.__setitem__),
                 patch.object(files.w, "control_text", lambda hwnd: self.typed.get(hwnd, "")),
                 patch.object(files.w.user32, "GetDlgItem", lambda window, item: 12)]
        for fake in fakes:
            fake.start()
            self.addCleanup(fake.stop)
        return files.dispatch(chat, 1, list(found))

    def test_the_button_is_pressed_when_it_shows_the_requested_count(self):
        two = ("C:\\가상\\a.txt", "C:\\가상\\b c.png")
        done = self.run_dispatch([line("파일 전송", 40), line("a.txt", 106), line("2개 전송", 297)], two)
        self.assertEqual((done["entered"], self.clicks, self.closed), (True, [20], []))
        self.assertEqual(self.typed[11], '"C:\\가상\\a.txt" "C:\\가상\\b c.png"')

    def test_another_count_closes_the_box_without_sending(self):
        done = self.run_dispatch([line("파일 전송", 40), line("2개 전송", 297)])
        self.assertEqual((done["entered"], done["reason"], self.clicks, self.closed),
                         (False, "send_box_not_recognized", [], [20]))

    def test_a_refused_file_closes_the_box_without_sending(self):
        done = self.run_dispatch([line("파일 전송", 40), line("전송 불가 1", 99), line("확인", 240)])
        self.assertEqual((done["entered"], done["reason"], self.clicks), (False, "files_refused_by_kakaotalk", []))


class SendFilesTests(Scratch):
    def setUp(self):
        super().setUp()
        self.world = World()
        self.ops = FakeOps(self.world)
        self.engine = sender.SendEngine(self.ops)
        self.arrives, self.calls = [], 0
        fakes = [patch.object(safety, "STATE_ROOT", self.folder / "state"),
                 patch.dict(os.environ, {"KAKAOTALK_WIN_SELF_NAME": "나"}),
                 patch.object(sender.files, "dispatch", self.dispatch),
                 patch.object(sender.time, "sleep", lambda seconds: None)]
        for fake in fakes:
            fake.start()
            self.addCleanup(fake.stop)
        os.environ.pop("KAKAOTALK_WIN_CONFIRM", None)

    def dispatch(self, chat, pid, found):
        self.calls += 1
        self.world.rooms[chat.title].extend(self.arrives)
        return {"entered": True}

    def test_files_and_pictures_are_confirmed_by_their_new_messages(self):
        self.world.composer["친구"] = "쓰다 만 글"  # a draft is no obstacle and stays
        self.arrives = [media("나", "파일: a.txt"), media("나", "사진", "photo")]
        sent = [self.file("a.txt"), self.file("b.png")]
        report = self.engine.send_files("친구", sent, "op-1", wait_seconds=0)
        self.assertEqual((report["status"], report["reason"], report["files"]),
                         ("observed_in_window", "new_file_messages", ["a.txt", "b.png"]))
        self.assertEqual(self.world.composer["친구"], "쓰다 만 글")
        self.assertEqual(len(safety.load_json(safety._send_log_path())), 2)  # one send per file
        again = self.engine.send_files("친구", sent, "op-1", wait_seconds=0)
        self.assertEqual((again["deduplicated"], self.calls), (True, 1))

    def test_a_picture_message_does_not_confirm_a_document(self):
        self.arrives = [media("나", "사진", "photo")]  # sent by hand meanwhile
        report = self.engine.send_files("친구", [self.file("견적서.pdf")], "op-6", wait_seconds=0)
        self.assertEqual((report["status"], report["reason"]), ("outcome_unknown", "new_message_not_observed"))

    def test_someone_elses_picture_does_not_confirm(self):
        self.arrives = [media("친구", "사진", "photo")]
        report = self.engine.send_files("친구", [self.file("b.png")], "op-2", wait_seconds=0)
        self.assertEqual((report["status"], report["reason"]), ("outcome_unknown", "new_message_not_observed"))

    def test_an_upload_in_progress_is_not_a_failed_send(self):
        self.arrives = [{"kind": "unclassified", "text": "[나]  파일: big.zip", "date": None}]
        report = self.engine.send_files("친구", [self.file("big.zip")], "op-3", wait_seconds=0)
        self.assertEqual((report["status"], report["reason"]), ("outcome_unknown", "upload_in_progress"))
        self.assertIsNone(safety.load_json(safety.STATE_ROOT / "unconfirmed.json"))
        self.world.rooms["친구"][-1] = media("나", "파일: big.zip")
        self.assertEqual(self.engine.recheck("op-3")["status"], "observed_in_window")

    def test_nothing_handed_over_is_blocked_and_counts_once(self):
        with patch.object(sender.files, "dispatch", lambda chat, pid, found: {"entered": False, "reason": "x"}):
            report = self.engine.send_files("친구", [self.file("a.txt"), self.file("b.txt")], "op-4", wait_seconds=0)
        self.assertEqual((report["status"], report["dispatched"], report["reason"]), ("blocked", False, "x"))
        self.assertEqual(len(safety.load_json(safety._send_log_path())), 1)

    def test_each_file_counts_toward_the_limits(self):
        safety.record_send("친구", "앞서 보낸 글", now=time.time() - 60)
        many = [self.file(f"{number}.txt") for number in range(10)]
        report = self.engine.send_files("친구", many, "op-5", wait_seconds=0)
        self.assertEqual((report["status"], report["reason"], self.calls), ("blocked", "room_hourly_send_limit", 0))


class SaveTests(Scratch):
    def setUp(self):
        super().setUp()
        self.world = World()
        self.world.rooms["친구"] += [media("친구", "파일: a.txt"), media("친구", "사진", "photo")]
        self.ops = FakeOps(self.world)
        self.link = "저장 • 다른 이름으로 저장"
        fakes = [patch.object(safety, "STATE_ROOT", self.folder / "state"),
                 patch.object(files, "download_folder", lambda: str(self.folder)),
                 patch.object(FakeChat, "message_list", 3, create=True),
                 patch.object(files, "_lines", lambda window: bubble("a.txt", 200, link=self.link)),
                 patch.object(files, "_click", self.click),
                 patch.object(files, "_show", lambda window: None), patch.object(files, "_hide", lambda *args: None),
                 patch.object(files.w, "post", lambda *args: None),
                 patch.object(files.time, "sleep", lambda seconds: None)]
        for fake in fakes:
            fake.start()
            self.addCleanup(fake.stop)

    def click(self, window, x, y):
        """The app saves the file (under a free name) and the bubble then offers to open it."""
        self.clicked = (x, y)
        self.file("a (1).txt" if (self.folder / "a.txt").exists() else "a.txt")
        self.link = "열기 • 폴더 열기"

    def test_the_new_file_in_the_download_folder_is_returned(self):
        target = self.folder / "copies"
        target.mkdir()
        result = files.save(self.ops, "친구", "a.txt", str(target))
        self.assertEqual((result["status"], result["path"], result["bytes"], result["alreadySaved"]),
                         ("ok", str(self.folder / "a.txt"), 24, False))
        self.assertEqual(self.clicked, (345, 301))  # the first word of the link line: "저장"
        self.assertTrue((target / "a.txt").exists())

    def test_a_copy_is_never_written_over_another_file(self):
        target = self.folder / "copies"
        target.mkdir()
        self.file("a.txt", b"kept", folder=target)
        result = files.save(self.ops, "친구", "a.txt", str(target))
        self.assertEqual((result["status"], "copiedTo" in result, (target / "a.txt").read_bytes()), ("ok", False, b"kept"))
        self.assertIn("already exists", result["notCopied"])

    def test_the_bubble_that_reads_exactly_so_is_preferred(self):
        lines = bubble("견적서_가다상사.xlsx", 500, link="열기 • 폴더 열기") + bubble("견적서_가나상사.xlsx", 200)
        with patch.object(files, "_lines", lambda window: lines):
            found = files._bubble(SimpleNamespace(hwnd=1), "견적서_가나상사.xlsx")
        self.assertEqual(found["name"], "견적서_가나상사.xlsx")

    def test_a_taken_name_is_followed_to_the_numbered_copy(self):
        self.file("a.txt", b"some other file with this name")
        self.assertEqual(files.save(self.ops, "친구", "a.txt")["path"], str(self.folder / "a (1).txt"))

    def test_a_saved_file_is_found_by_name_and_size_without_clicking(self):
        self.link = "열기 • 폴더 열기"
        self.file("a.txt", b"some other file with this name")
        self.assertEqual(files.save(self.ops, "친구", "a.txt")["reason"], "saved_copy_not_identified")
        self.file("a (1).txt")
        result = files.save(self.ops, "친구", "a.txt")
        self.assertEqual((result["path"], result["alreadySaved"]), (str(self.folder / "a (1).txt"), True))
        self.assertFalse(hasattr(self, "clicked"))

    def test_only_files_named_in_the_loaded_messages_are_saved(self):
        result = files.save(self.ops, "친구", "b.txt")
        self.assertEqual((result["reason"], result["filesInLoadedMessages"]), ("file_message_not_found", ["a.txt"]))
        self.assertEqual(files.save(self.ops, "닫힌 방", "a.txt")["reason"], "room_window_not_open")

    def test_a_save_that_never_finishes_is_reported(self):
        with patch.object(files, "_click", lambda window, x, y: None), \
                patch.object(files.time, "monotonic", iter(range(0, 10_000, 30)).__next__):
            self.assertEqual(files.save(self.ops, "친구", "a.txt")["reason"], "save_not_confirmed")


if __name__ == "__main__":
    unittest.main()
