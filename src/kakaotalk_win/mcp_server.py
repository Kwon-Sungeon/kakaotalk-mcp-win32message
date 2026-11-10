"""KakaoTalk for Windows MCP server (stdio): one tool per feature of the official PC app."""

import json

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.types import Image
from mcp.types import ImageContent, TextContent, ToolAnnotations

mcp = FastMCP("kakaotalk-win", instructions=(
    "PC 카카오톡 앱의 창에 Windows 메시지를 보내 동작합니다. 카카오 서버, 통신 규약, 로컬 DB는 건드리지 않습니다. "
    "카카오톡 조작은 여기 있는 도구로만 하고, 창 메시지나 클립보드를 다루는 코드를 따로 짜지 마세요. "
    "방은 창 제목과 글자 하나까지 같은 이름으로 지정합니다. 같은 이름의 창이 둘 이상이면 도구가 멈춥니다. "
    "닫혀 있는 방은 kakaotalk_win_open으로 먼저 여세요. "
    "읽기 결과는 대화창에 불러와 있는 메시지까지입니다. 얼마나 읽었는지는 coverage에 나옵니다. "
    "결과를 이어서 받을 때는 응답의 nextOffset과 snapshotId를 그대로 넘기고, "
    "도중에 대화가 바뀌었다는 snapshot_changed가 나오면 offset 0으로 돌아가 새로 받으세요. "
    "rooms의 방 이름은 화면 글자 인식 결과라 틀릴 수 있으니 이미지와 대조하세요. "
    "메시지는 사용자가 직접 말한 방에, 사용자가 말한 내용만 보냅니다. "
    "전송 요청 하나에 operation_id 하나를 만들고, 그 요청을 다시 확인할 때도 같은 값을 씁니다. "
    "어느 방인지, 무엇을 보낼지 분명하지 않으면 묻고 나서 보내세요. 문장을 대신 지었다면 보내기 전에 사용자에게 보여 주세요. "
    "dispatched=false인 blocked 응답은 입력도 전송도 일어나지 않았다는 뜻이니, 같은 이유로 다시 호출하지 마세요. "
    "결과가 outcome_unknown이면 새로 보내지 말고 kakaotalk_win_send_result에 같은 operation_id를 넣어 상태만 확인하세요. "
    "새 채팅방 만들기, 친구 추가와 초대, 오픈채팅 참여, 여러 방에 같은 글 보내기, 예약 발송 기능은 없습니다. "
    "safety_stop_active가 나오면 카카오톡 화면에 안내가 떠 있는지 사용자에게 확인을 부탁하세요. 정지 해제는 사용자가 터미널에서만 할 수 있습니다. "
    "대화에는 다른 사람의 개인정보가 담겨 있습니다. 요청받은 범위를 넘어 저장하거나 다른 곳에 옮기지 마세요."
))


def _hints(*, read_only, repeat_safe, external=False):
    """MCP tool hints. Nothing here deletes anything; only sending reaches other people."""
    return ToolAnnotations(readOnlyHint=read_only, destructiveHint=False,
                           idempotentHint=repeat_safe, openWorldHint=external)


LOOK = _hints(read_only=True, repeat_safe=True)          # reads only
WINDOW = _hints(read_only=False, repeat_safe=True)       # opens/closes windows, sets the search box back
SCROLL = _hints(read_only=False, repeat_safe=False)      # moves the view or writes a capture file
DELIVER = _hints(read_only=False, repeat_safe=False, external=True)

Reply = list[TextContent | ImageContent]


def _json(value):
    return json.dumps(value, indent=2, ensure_ascii=False)


def _app():
    from .operations import KakaoTalkWin
    return KakaoTalkWin()


def _with_picture(result, attach):
    parts = [TextContent(type="text", text=_json(result))]
    pictures = result.get("imagePaths") or ([result["imagePath"]] if result.get("imagePath") else [])
    if attach and result.get("status") == "ok":
        parts.extend(Image(path=picture).to_image_content() for picture in pictures)
    return parts


def _ocr_failed(result):
    return (result.get("ocr") or {}).get("status") != "ok"


@mcp.tool(annotations=LOOK)
def kakaotalk_win_status() -> str:
    """카카오톡 PC가 켜져 있는지, 정식 서명 파일인지, 잠겨 있는지와 열린 대화창 수, 클립보드 기록 설정, 전송 한도를 알려 줍니다.

    메시지는 읽지 않고 창 상태도 그대로 둡니다. 응답에 warnings가 있으면 사용자에게 전해 주세요.
    """
    return _json(_app().status())


@mcp.tool(annotations=LOOK)
def kakaotalk_win_windows() -> str:
    """지금 떠 있는 대화창마다 정확한 제목, 최소화 여부, 입력창에 쓰다 만 글이 있는지를 돌려줍니다.

    새로 열거나 앞으로 가져오는 창은 없고, 메시지와 쓰다 만 글의 내용도 읽지 않습니다.
    다른 도구에는 여기 나온 windowTitle을 고치지 말고 넘기세요. 열려 있지 않은 방은 kakaotalk_win_rooms로 찾습니다.
    """
    return _json(_app().windows())


@mcp.tool(annotations=WINDOW)
def kakaotalk_win_rooms(query: str = "", pages: int = 1, include_image: bool = False) -> Reply:
    """메인 창의 채팅 목록을 캡처하고 Windows OCR로 방 이름, 참여 인원, 미리보기, 시각, 안 읽음 표시를 읽습니다.

    목록은 최근 대화 순입니다. pages(1~10)만큼 목록을 아래로 내려 가며 읽고, 끝나면 맨 위로 되돌립니다.
    endOfList=true면 목록 끝까지 읽은 것입니다. 방을 열지 않으므로 읽음 처리도 되지 않습니다.
    query가 있으면 앱의 채팅방 검색칸에 넣어 나온 결과를 읽은 뒤, 검색칸을 원래 내용으로 돌려놓습니다.
    글자 인식이 틀릴 수 있으니 방을 열기 전에 이름을 확인하세요(nameVariants는 아이콘이 글자로 읽혔을 때의 후보).
    include_image=True이거나 OCR이 실패하면 캡처한 목록 이미지도 같이 보냅니다.
    """
    result = _app().rooms(query=query, pages=pages)
    return _with_picture(result, include_image or _ocr_failed(result))


@mcp.tool(annotations=WINDOW)
def kakaotalk_win_search_rooms(query: str, pages: int = 1, include_image: bool = False) -> Reply:
    """앱의 채팅방 검색에 query를 넣어 걸리는 방을 OCR로 읽어 옵니다. 끝나면 검색칸을 원래 내용으로 돌려놓습니다."""
    result = _app().search_rooms(query, pages=pages)
    return _with_picture(result, include_image or _ocr_failed(result))


@mcp.tool(annotations=WINDOW)
def kakaotalk_win_open(window_title: str) -> str:
    """이름이 정확히 같은 기존 방을 앱의 채팅방 검색으로 엽니다. 열린 창은 앞으로 나오지 않습니다.

    이미 떠 있는 방이면 그대로 둡니다. 검색 결과 맨 위 방의 제목이 다르면 그 창을 바로 닫고 멈춥니다.
    방을 새로 만드는 일은 없습니다. existingHistory=false로 나온 방에는 메시지를 보낼 수 없습니다.
    """
    return _json(_app().open_room(window_title))


@mcp.tool(annotations=WINDOW)
def kakaotalk_win_close(window_title: str) -> str:
    """대화창을 닫기만 하고 방에서 나가지는 않습니다. 입력창에 쓰다 만 글이 있으면 닫지 않고 멈춥니다."""
    return _json(_app().close_room(window_title))


@mcp.tool(annotations=LOOK)
def kakaotalk_win_read(window_title: str, limit: int = 50, offset: int = 0, snapshot_id: str = "") -> str:
    """대화창에 불러와 있는 메시지를 날짜, 보낸 사람, 시각, 본문으로 나눠 돌려줍니다. limit은 1~500입니다.

    가장 최근 메시지 쪽에서 limit개를 잘라 오고, 그 안에서는 오래된 것부터 놓입니다.
    더 앞의 메시지는 응답의 nextOffset을 offset에, snapshotId를 snapshot_id에 넣어 다시 부르면 됩니다.
    사진, 동영상, 이모티콘, 파일은 kind=media와 mediaType으로만 나옵니다. 내용을 보려면 read_attachment를 쓰세요.
    500자가 넘는 메시지는 앱이 앞 500자만 내주므로 textTruncated=true가 붙습니다. 전체 본문은 kakaotalk_win_read_full_text로 받으세요.
    창 위치와 쓰다 만 글은 바꾸지 않고, 잠시 쓴 클립보드는 읽은 뒤 원래 내용으로 돌려놓습니다.
    아직 불러오지 않은 옛 대화는 kakaotalk_win_load_more를 먼저 부른 다음 읽으세요.
    """
    return _json(_app().read_messages(window_title, limit, offset, snapshot_id))


@mcp.tool(annotations=SCROLL)
def kakaotalk_win_read_full_text(window_title: str, max_messages: int = 3, scroll_pages: int = 10) -> str:
    """500자가 넘어 앞부분만 읽힌 메시지(textTruncated=true)의 전체 본문을 가져옵니다. 최근 것부터 max_messages개(1~10)입니다.

    대화창에서 메시지 아래 '전체보기'를 눌러 열리는 창의 글을 복사한 뒤 그 창을 바로 닫습니다.
    클릭은 창 메시지로 보내므로 마우스 커서가 움직이지 않고 다른 창의 포커스도 바꾸지 않습니다.
    버튼이 화면 밖에 있으면 대화를 한 화면씩 위로 올려 찾고(최대 scroll_pages번, 0~30), 끝나면 맨 아래로 돌아갑니다.
    찾지 못한 메시지는 notFound에 나옵니다. 돌려주는 index는 kakaotalk_win_read 결과의 index와 같습니다.
    """
    return _json(_app().read_full_text(window_title, max_messages, scroll_pages))


@mcp.tool(annotations=SCROLL)
def kakaotalk_win_read_attachment(window_title: str, output_path: str | None = None, ocr: bool = False) -> Reply:
    """대화창이 지금 보이는 모습 그대로를 PNG로 캡처해 돌려줍니다. 사진, 이모티콘, 카드 메시지를 볼 때 씁니다.

    다른 창에 가려 있어도 캡처되고, 최소화된 창은 포커스를 주지 않고 잠깐 펼쳤다가 다시 내립니다.
    받은 파일 원본이 아니라 화면에 보이는 해상도의 그림입니다.
    ocr=True면 Windows OCR로 읽은 글자를 붙이는데, 실제 메시지 본문과는 따로 다루세요.
    output_path는 절대 경로로 주고, 비워 두면 임시 PNG 파일에 저장합니다.
    """
    return _with_picture(_app().capture(window_title, output_path, ocr), attach=True)


@mcp.tool(annotations=WINDOW)
def kakaotalk_win_read_many(window_titles: list[str], limit: int = 50, open_if_needed: bool = True,
                            close_after: bool = False) -> str:
    """방 1~10개를 하나씩 차례로 읽고, 닫혀 있으면 열어서 읽습니다. 클립보드를 하나만 쓰므로 동시에 읽지 않습니다.

    방마다 결과나 실패 이유가 따로 나오고, 방끼리 읽은 시점은 조금씩 다릅니다.
    close_after=True로 부르면 읽으려고 새로 띄운 창은 끝난 뒤 닫고, 원래 떠 있던 창과 쓰다 만 글은 건드리지 않습니다.
    """
    return _json(_app().read_many(window_titles, limit, open_if_needed, close_after))


@mcp.tool(annotations=WINDOW)
def kakaotalk_win_recent(minutes: int = 60, open_rooms: bool = False, list_pages: int = 3,
                         max_rooms: int = 10) -> str:
    """최근 minutes분(1~1440) 동안 여러 방에 온 메시지를 한 번에 모읍니다. "최근 1시간 카톡 요약해 줘" 같은 요청에 씁니다.

    채팅 목록(list_pages 페이지, 열지 않고 읽음)의 시각으로 해당 시간대에 대화가 있었던 방을 고르고,
    방마다 그 시간 이후의 메시지만 돌려줍니다. 방 이름은 열린 창 제목과 맞춰 정확한 이름으로 바꿉니다.
    open_rooms=False(기본)면 이미 열려 있는 대화창만 읽고, 닫힌 방은 notRead에 미리보기와 함께 적습니다.
    닫힌 방까지 읽으려면 사용자에게 '열면 읽음 처리된다'고 알리고 동의를 받은 뒤 open_rooms=True로 다시 부르세요.
    그때 연 방은 markedAsRead에 나오고 끝나면 다시 닫힙니다.
    listCoverage.complete=false면 목록을 끝까지 보지 못한 것이니 list_pages를 늘리세요.
    """
    return _json(_app().recent_messages(minutes, open_rooms, list_pages, max_rooms))


@mcp.tool(annotations=SCROLL)
def kakaotalk_win_load_more(window_title: str, pages: int = 1) -> str:
    """메시지 목록을 위로 올려 옛 메시지를 최대 pages번(1~10) 더 불러오고, 마지막에 맨 아래로 돌아갑니다.

    메시지 수가 늘지 않았다고 대화의 처음까지 왔다고 보면 안 됩니다. historyComplete는 항상 false입니다.
    """
    return _json(_app().load_more(window_title, pages))


@mcp.tool(annotations=SCROLL)
def kakaotalk_win_search(window_title: str, query: str, limit: int = 100, load_pages: int = 0,
                         case_sensitive: bool = False, context_rows: int = 2) -> str:
    """불러와 있는 메시지의 본문과 보낸 사람에서 query 글자를 찾고, 찾은 메시지 앞뒤로 context_rows개씩 붙여 줍니다.

    load_pages를 1~10으로 주면 옛 메시지를 그만큼 먼저 불러온 다음 찾습니다. 앱의 대화 검색 화면은 열지 않습니다.
    찾는 범위는 searchScope에 적힌 곳까지라, 이 방의 모든 과거 대화에서 찾았다는 뜻은 아닙니다.
    """
    return _json(_app().search_messages(window_title, query, limit, load_pages, case_sensitive, context_rows))


@mcp.tool(annotations=DELIVER)
def kakaotalk_win_send(
    window_title: str,
    text: str,
    operation_id: str,
    wait_seconds: float = 8,
    require_self: bool = False,
) -> str:
    """대화 기록이 있는 방에 글 한 건을 보냅니다. 사용자가 지정한 방과 내용만 보내세요.

    사용자가 확인창 모드(confirmSend=dialog)를 켜 두었다면 PC에 확인창이 뜨고, [예]를 눌러야 나갑니다.
    operation_id는 전송 요청마다 새로 정하고, 같은 요청을 다시 부를 때는 같은 값을 씁니다. 같은 ID로는 다시 입력하지 않고 결과만 확인합니다.
    대화 기록이 없는 방, 쓰다 만 글이 있는 방, 간격·시간당·하루 한도를 넘은 경우, 긴 글을 다른 방에도 똑같이 보내려는 경우는 막힙니다.
    observed_in_window는 대화창에 새 메시지가 보이고 입력창이 비었다는 뜻일 뿐, 서버가 받았거나 상대가 읽었다는 뜻이 아닙니다.
    outcome_unknown이나 시간 초과가 나오면 새로 보내지 말고 kakaotalk_win_send_result로 확인만 하세요.
    require_self=True면 '나와의 채팅'에만 보냅니다. 방 제목과 보낸 사람이 모두 설정한 내 이름과 같아야 합니다.
    """
    return _json(_app().send_message(window_title, text, operation_id, wait_seconds, require_self))


@mcp.tool(annotations=LOOK)
def kakaotalk_win_send_result(operation_id: str, wait_seconds: float = 0) -> str:
    """기록해 둔 전송 결과를 알려 주고, 아직 확정되지 않았으면 대화창을 읽어서만 다시 확인합니다. 창을 열거나 다시 보내지는 않습니다."""
    return _json(_app().send_result(operation_id, wait_seconds))


def main():
    mcp.run("stdio")


if __name__ == "__main__":
    main()
