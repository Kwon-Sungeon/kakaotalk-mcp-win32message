import sys
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

if sys.platform != "win32":
    raise unittest.SkipTest("kakaotalk_win requires Windows")

from kakaotalk_win import operations  # noqa: E402
from kakaotalk_win.operations import (KakaoTalkWin, _badge_number, _ink_badges, _match_title,  # noqa: E402
                                      _plain_strip, _red_badge_box, _row_time, _similar)

NOW = datetime(2026, 11, 10, 14, 20)


def solid(width, height, bgr):
    return width, height, bytes(bytes(bgr) + b"\x00") * (width * height)


def paint(image, box, bgr):
    width, height, data = image
    pixels = bytearray(data)
    for y in range(box[1], box[3]):
        for x in range(box[0], box[2]):
            pixels[(y * width + x) * 4:(y * width + x) * 4 + 3] = bytes(bgr)
    return width, height, bytes(pixels)


class RowTimeTests(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(_row_time({"timeText": "오후 2:12"}, NOW), NOW.replace(hour=14, minute=12))
        self.assertEqual(_row_time({"timeText": "오전 12:05"}, NOW), NOW.replace(hour=0, minute=5))
        self.assertEqual(_row_time({"timeText": "오후 12:30"}, NOW), NOW.replace(hour=12, minute=30))
        self.assertEqual(_row_time({"timeText": "2:12 PM"}, NOW), NOW.replace(hour=14, minute=12))
        self.assertEqual(_row_time({"timeText": "어제"}, NOW), datetime(2026, 11, 9, 23, 59, 59))
        self.assertLess(_row_time({"timeText": "11월 7일"}, NOW), datetime(2026, 11, 9))
        self.assertIsNone(_row_time({"timeText": None}, NOW))


class TitleMatchTests(unittest.TestCase):
    TITLES = ["주말 등산 모임", "AB 프로젝트방", "사진 동아리 공지방 2026년 가을 정기 출사 안내"]

    def test_exact_variant_truncated_and_misread(self):
        self.assertEqual(_match_title({"name": "주말 등산 모임"}, self.TITLES), "주말 등산 모임")
        self.assertEqual(_match_title({"name": "OAB 프로젝트방", "nameVariants": ["AB 프로젝트방"]}, self.TITLES),
                         "AB 프로젝트방")
        self.assertEqual(_match_title({"name": "사진 동아리 공지방 2026년 가을…"}, self.TITLES),
                         "사진 동아리 공지방 2026년 가을 정기 출사 안내")
        self.assertEqual(_match_title({"name": "주말 등산 모입"}, self.TITLES), "주말 등산 모임")
        self.assertIsNone(_match_title({"name": "전혀 다른 방"}, self.TITLES))

    def test_short_or_ambiguous_names_do_not_match(self):
        self.assertFalse(_similar("가", "가나다라마"))
        self.assertIsNone(_match_title({"name": "모임"}, ["모임 A", "모임 B"]))

    def test_rooms_that_differ_by_a_number_are_different_rooms(self):
        self.assertFalse(_similar("사진 동아리 24기 수다방", "사진 동아리 25기 수다방"))
        self.assertFalse(_similar("등산 1팀", "등산 2팀"))
        self.assertTrue(_similar("사진 동아리 24기 수다밤", "사진 동아리 24기 수다방"))
        self.assertIsNone(_match_title({"name": "사진 동아리 25기 수다방"}, ["사진 동아리 24기 수다방"]))

    def test_a_title_named_exactly_by_another_row_is_not_matched_loosely(self):
        titles = ["주말 등산 모임"]
        self.assertIsNone(_match_title({"name": "주말 등산 모입"}, titles, claimed={"주말 등산 모임"}))
        self.assertEqual(_match_title({"name": "주말 등산 모임"}, titles, claimed={"주말 등산 모임"}), "주말 등산 모임")


class SelectionStripTests(unittest.TestCase):
    def test_click_only_on_plain_background(self):
        background = solid(200, 100, (240, 220, 190))
        self.assertTrue(_plain_strip(background, 0, 0, 200, 100))
        bubble = paint(background, (40, 90, 180, 100), (0, 230, 255))
        self.assertFalse(_plain_strip(bubble, 0, 0, 200, 100))
        self.assertFalse(_plain_strip(background, 0, 50, 200, 100))  # strip outside the capture


class BadgeTests(unittest.TestCase):
    def test_badge_box_and_ink(self):
        image = paint(solid(100, 40, (255, 255, 255)), (85, 10, 97, 30), (97, 111, 255))
        image = paint(image, (90, 15, 92, 25), (255, 255, 255))  # a white "1" inside the badge
        box = _red_badge_box(image, 0, 40)
        self.assertEqual(box, (85, 10, 97, 30))
        width, height, data = _ink_badges(image, [box])
        black = sum(1 for at in range(0, len(data), 4) if data[at] == 0)
        self.assertEqual(black, 2 * 10)  # only the digit became ink; the red fill turned white
        red = sum(1 for at in range(0, len(data), 4) if operations._is_badge_red(*data[at:at + 3]))
        self.assertEqual(red, 0)
        self.assertIsNone(_red_badge_box(solid(100, 40, (255, 255, 255)), 0, 40))

    def test_badge_number_from_words_on_the_box(self):
        words = [{"text": "오후", "left": 0, "top": 0, "right": 20, "bottom": 10},
                 {"text": "1", "left": 30, "top": 20, "right": 36, "bottom": 32},
                 {"text": "0", "left": 37, "top": 20, "right": 43, "bottom": 32},
                 {"text": "7", "left": 30, "top": 80, "right": 36, "bottom": 92}]
        self.assertEqual(_badge_number(words, (125, 18, 150, 34), 100), "10")
        self.assertIsNone(_badge_number(words, (125, 40, 150, 60), 100))
        self.assertIsNone(_badge_number([{"text": "0", "left": 30, "top": 20, "right": 36, "bottom": 32}],
                                        (125, 18, 150, 34), 100))


class RoomRowTests(unittest.TestCase):
    """One room row of a 150% capture, where OCR found the name and time but missed the preview."""

    WIDTH, HEIGHT = 651, 220

    def lines(self):
        return [{"text": "주말 등산 모임", "left": 120, "top": 33, "right": 260, "bottom": 48,
                 "words": [{"text": "주말", "left": 120, "top": 33, "right": 150, "bottom": 48}]},
                {"text": "오후 2•26", "left": 550, "top": 33, "right": 618, "bottom": 46,
                 "words": [{"text": "오후", "left": 550, "top": 33, "right": 580, "bottom": 46},
                           {"text": "2•26", "left": 588, "top": 34, "right": 618, "bottom": 46}]}]

    def test_badge_beside_a_missed_preview_is_found_whole(self):
        image = paint(solid(self.WIDTH, self.HEIGHT, (255, 255, 255)), (593, 59, 618, 84), (107, 122, 255))
        row = operations._rows_from_ocr(self.lines(), image, 1.0)[0]
        self.assertTrue(row["hasUnread"])
        self.assertEqual(row["badgeBox"], (593, 59, 618, 84))  # not cut at the name line
        self.assertEqual(row["timeText"], "오후 2:26")
        self.assertNotIn("previewUnreadable", row)

    def test_unread_preview_text_is_flagged(self):
        image = paint(solid(self.WIDTH, self.HEIGHT, (255, 255, 255)), (122, 62, 150, 78), (90, 90, 90))  # "넵"
        row = operations._rows_from_ocr(self.lines(), image, 1.0)[0]
        self.assertIsNone(row["preview"])
        self.assertTrue(row["previewUnreadable"])

    def test_ink_check(self):
        blank = solid(200, 40, (255, 255, 255))
        self.assertFalse(operations._has_ink(blank, 0, 0, 200, 40))
        self.assertTrue(operations._has_ink(paint(blank, (20, 10, 60, 30), (60, 60, 60)), 0, 0, 200, 40))


class NamelessRowTests(unittest.TestCase):
    """OCR read a room's time but neither its short name nor its one-word preview."""

    WIDTH, HEIGHT = 651, 400

    @staticmethod
    def line(text, left, top, right, bottom):
        return {"text": text, "left": left, "top": top, "right": right, "bottom": bottom,
                "words": [{"text": text, "left": left, "top": top, "right": right, "bottom": bottom}]}

    def lines(self):
        return [self.line("주말 등산 모임", 120, 33, 260, 48), self.line("내일 봐요", 120, 62, 200, 76),
                self.line("오후 3:10", 550, 33, 618, 46),
                self.line("오후 2:58", 550, 138, 618, 151),            # the room below: name and preview missed
                self.line("AB 프로젝트방", 120, 243, 250, 258), self.line("확인했습니다", 120, 272, 230, 286),
                self.line("오후 2:26", 550, 243, 618, 256)]

    def image(self):
        image = solid(self.WIDTH, self.HEIGHT, (255, 255, 255))
        image = paint(image, (122, 140, 160, 154), (30, 30, 30))       # the unread name
        image = paint(image, (122, 168, 140, 182), (90, 90, 90))       # the unread preview
        return paint(image, (593, 164, 618, 189), (107, 122, 255))     # and an unread badge

    def test_a_time_without_a_name_is_kept_as_a_room(self):
        rows = operations._rows_from_ocr(self.lines(), self.image(), 1.0)
        self.assertEqual([row["timeText"] for row in rows], ["오후 3:10", "오후 2:58", "오후 2:26"])
        nameless = rows[1]
        self.assertIsNone(nameless["name"])
        self.assertTrue(nameless["nameUnreadable"] and nameless["hasUnread"] and nameless["previewUnreadable"])
        self.assertEqual(nameless["badgeBox"], (593, 164, 618, 189))

    def test_a_time_of_a_row_cut_by_the_top_edge_is_not_a_room(self):
        lines = [self.line("이전 페이지 방", 120, 2, 250, 16), self.line("오후 4:00", 550, 3, 618, 15)] + self.lines()
        rows = operations._rows_from_ocr(lines, self.image(), 1.0)
        self.assertNotIn("오후 4:00", [row["timeText"] for row in rows])

    def test_a_second_pass_fills_the_name_in(self):
        rows = operations._rows_from_ocr(self.lines(), self.image(), 1.0, positions=True)
        again = operations._rows_from_ocr(self.lines() + [self.line("홍길동", 120, 140, 160, 154),
                                                          self.line("넵", 120, 168, 140, 182)],
                                          self.image(), 1.0, positions=True)
        operations._fill_unread_names(rows, again, 1.0)
        self.assertEqual((rows[1]["name"], rows[1]["preview"]), ("홍길동", "넵"))
        self.assertNotIn("nameUnreadable", rows[1])
        self.assertNotIn("previewUnreadable", rows[1])

    def test_pages_merge_a_nameless_row_only_inside_their_overlap(self):
        named = {"name": "홍길동", "timeText": "오후 2:26", "preview": "넵"}
        other = {"name": "다른 방", "timeText": "오후 2:20"}
        nameless = {"name": None, "nameUnreadable": True, "timeText": "오후 2:26", "preview": None}
        merged = operations._merge_pages([[dict(named), dict(other)], [dict(nameless), {"name": "새 방",
                                                                                       "timeText": "오후 2:10"}]])
        self.assertEqual([row["name"] for row in merged], ["홍길동", "다른 방", "새 방"])  # repeat in the overlap
        merged = operations._merge_pages([[dict(named), dict(other), {"name": "셋째", "timeText": "오후 2:21"}],
                                          [dict(nameless)]])
        self.assertEqual([row["name"] for row in merged], ["홍길동", "다른 방", "셋째", None])  # not in it


class SmallScaleOcrTests(unittest.TestCase):
    def test_darken_turns_grey_text_black_and_keeps_white(self):
        _, _, data = operations.cap.darken((2, 1, bytes((150, 150, 150, 255, 255, 255, 255, 255))))
        self.assertEqual(data, bytes((0, 0, 0, 255, 255, 255, 255, 255)))

    def test_zoomed_coordinates_are_divided_back(self):
        line = {"text": "오후 2:26", "left": 244, "top": 66, "right": 300, "bottom": 92,
                "words": {"text": "오후", "left": 244, "top": 66, "right": 270, "bottom": 92}}
        scaled = operations.cap._unzoom(line, 2)
        self.assertEqual((scaled["left"], scaled["top"], scaled["right"], scaled["bottom"]), (122, 33, 150, 46))
        self.assertEqual(scaled["words"][0]["left"], 122)

    def test_time_with_a_misread_colon(self):
        for raw in ("오후 2•26", "오후 2·26", "오후 2.26", "오후 226"):
            self.assertEqual(operations._clean_time(raw), "오후 2:26")
            self.assertTrue(operations._TIMEISH.match(raw))
        self.assertEqual(_row_time({"timeText": operations._clean_time("오후 2•12")}, NOW),
                         NOW.replace(hour=14, minute=12))


class FakeApp(KakaoTalkWin):
    """recent_messages over a fictional app: three listed rooms, two of them open."""

    def __init__(self, rooms, open_titles, messages):
        self._selected, self._depth = {}, 0
        self.listed, self.open_titles, self.messages = rooms, list(open_titles), messages
        self.opened, self.closed = [], []

    def rooms(self, query="", ocr=True, output_path=None, pages=1):
        return {"status": "ok", "items": self.listed, "pagesCaptured": pages, "endOfList": False}

    def _state(self):
        return SimpleNamespace(chats=[SimpleNamespace(title=title) for title in self.open_titles])

    def open_room(self, window_title, require_existing=True):
        self.opened.append(window_title)
        if window_title == "AB 프로젝트":
            return {"status": "blocked", "reason": "search_top_result_mismatch", "openedTitle": "AB 프로젝트방"}
        if window_title in self.messages:
            self.open_titles.append(window_title)
            return {"status": "ok"}
        return {"status": "blocked", "reason": "room_not_found"}

    def close_room(self, window_title):
        self.closed.append(window_title)
        return {"status": "ok", "windowTitle": window_title}

    def read_messages(self, window_title, limit=50, offset=0, snapshot_id="", open_if_needed=False):
        return {"status": "ok", "items": self.messages[window_title]}

    def load_more(self, window_title, pages=1):
        return {"status": "ok", "contentChanged": False}


def message(text, minutes_ago, sender="김철수"):
    moment = datetime.now() - timedelta(minutes=minutes_ago)
    return {"kind": "text", "sender": sender, "text": text, "date": moment.strftime("%Y-%m-%d"),
            "time": moment.strftime("%H:%M")}


def label(minutes_ago):
    moment = datetime.now() - timedelta(minutes=minutes_ago)
    return f"{'오후' if moment.hour >= 12 else '오전'} {moment.hour % 12 or 12}:{moment.minute:02d}"


class RecentMessagesTests(unittest.TestCase):
    def setUp(self):
        self.listed = [{"name": "주말 등산 모입", "timeText": label(5), "preview": "좋아요", "hasUnread": True},
                       {"name": "AB 프로젝트", "timeText": label(20), "preview": "네", "hasUnread": False},
                       {"name": "오래된 방", "timeText": "어제", "preview": "옛날", "hasUnread": False}]
        self.messages = {"주말 등산 모임": [message("지난주 사진", 200), message("토요일 7시 출발", 5)],
                         "AB 프로젝트방": [message("자료 올렸어요", 20, "이영희")]}

    def test_only_open_rooms_by_default(self):
        app = FakeApp(self.listed, ["주말 등산 모임"], self.messages)
        with patch.object(operations, "_clear_selection"):
            result = app.recent_messages(minutes=60)
        self.assertEqual([room["windowTitle"] for room in result["rooms"]], ["주말 등산 모임"])
        self.assertEqual([m["text"] for m in result["rooms"][0]["messages"]], ["토요일 7시 출발"])
        self.assertEqual([(row["name"], row["reason"]) for row in result["notRead"]],
                         [("AB 프로젝트", "room_window_not_open")])
        self.assertEqual(app.opened, [])
        self.assertTrue(result["listCoverage"]["complete"])

    def test_open_rooms_follows_a_close_search_hit_and_closes_it_again(self):
        app = FakeApp(self.listed, ["주말 등산 모임"], self.messages)
        with patch.object(operations, "_clear_selection"):
            result = app.recent_messages(minutes=60, open_rooms=True)
        self.assertEqual([room["windowTitle"] for room in result["rooms"]], ["주말 등산 모임", "AB 프로젝트방"])
        self.assertEqual(result["markedAsRead"], ["AB 프로젝트방"])
        self.assertEqual(app.closed, ["AB 프로젝트방"])
        self.assertEqual(result["notRead"], [])

    def test_a_closed_room_is_never_read_from_a_look_alike_open_room(self):
        listed = [{"name": "사진 동아리 25기 수다방", "timeText": label(3), "preview": "사진 올려요", "hasUnread": True},
                  {"name": "사진 동아리 24기 수다방", "timeText": label(4), "preview": "좋아요", "hasUnread": False},
                  {"name": "주말 등산 모입", "timeText": label(5), "preview": "출발", "hasUnread": False}]
        messages = {"사진 동아리 24기 수다방": [message("좋아요", 4)], "주말 등산 모임": [message("출발", 5)]}
        app = FakeApp(listed, ["사진 동아리 24기 수다방", "주말 등산 모임"], messages)
        with patch.object(operations, "_clear_selection"):
            result = app.recent_messages(minutes=60)
        self.assertEqual([room["windowTitle"] for room in result["rooms"]],
                         ["사진 동아리 24기 수다방", "주말 등산 모임"])
        self.assertEqual([(row["name"], row["reason"]) for row in result["notRead"]],
                         [("사진 동아리 25기 수다방", "room_window_not_open")])

    def test_a_nameless_room_is_reported_not_guessed(self):
        listed = [{"name": None, "nameUnreadable": True, "timeText": label(2), "preview": None, "hasUnread": True}]
        listed += self.listed
        app = FakeApp(listed, ["주말 등산 모임"], self.messages)
        with patch.object(operations, "_clear_selection"):
            result = app.recent_messages(minutes=60, open_rooms=True)
        self.assertEqual(result["notRead"][0]["reason"], "name_unreadable")
        self.assertNotIn(None, app.opened)

    def test_selection_is_cleared_once_after_the_outermost_call(self):
        app = FakeApp(self.listed, ["주말 등산 모임"], self.messages)
        chat = SimpleNamespace(hwnd=1)
        original = app.read_messages

        def reading(*args, **kwargs):
            app._selected[1] = chat
            return original(*args, **kwargs)
        app.read_messages = reading
        with patch.object(operations, "_clear_selection") as clear:
            app.recent_messages(minutes=60)
        clear.assert_called_once_with(chat)
        self.assertEqual(app._selected, {})


if __name__ == "__main__":
    unittest.main()
