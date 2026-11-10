"""KakaoTalk for Windows MCP server (stdio): one tool per feature of the official PC app."""

import json

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.types import Image
from mcp.types import ImageContent, TextContent, ToolAnnotations

mcp = FastMCP("kakaotalk-win", instructions=(
    "Windows 카카오톡 PC 앱을 창 메시지로 조작합니다. 카카오 서버·프로토콜·로컬 DB에는 접근하지 않습니다. "
    "도구가 제공하는 기능만 쓰고 별도 제어 코드를 작성하지 마세요. "
    "방 이름은 정확한 제목을 쓰고, 동명 방이면 중단합니다. 닫힌 방은 kakaotalk_win_open으로 먼저 엽니다. "
    "읽기는 창에 불러와진 메시지까지이며 전체 기록을 보장하지 않습니다(coverage 확인). "
    "다음 페이지는 nextOffset과 snapshotId를 넘기고, snapshot_changed면 처음부터 읽으세요. "
    "방 목록(rooms)은 화면 OCR이라 오인식이 있을 수 있으니 이미지를 확인하세요. "
    "전송은 사용자가 명시적으로 요청한 방과 본문에만 하고, 요청마다 operation_id를 한 번 정해 재사용하세요. "
    "방이나 본문이 모호하면 보내기 전에 사용자에게 확인하세요. 본문을 대신 지었다면 보내기 전에 보여 주세요. "
    "blocked(dispatched=false)는 아무것도 입력·전송되지 않았다는 뜻입니다. 같은 사유로 반복 호출하지 마세요. "
    "outcome_unknown이면 재전송하지 말고 원래 ID로 kakaotalk_win_send_result만 조회하세요. "
    "이 도구는 새 채팅방 만들기·친구 추가·초대·오픈채팅 참여·여러 방 동시 발송·예약 발송을 하지 않습니다. "
    "safety_stop_active면 사용자에게 카카오톡 화면 확인을 요청하세요. 해제는 사용자가 CLI로만 합니다. "
    "읽은 대화에는 다른 사람의 개인정보가 들어 있으니 요청 범위 밖으로 저장·공유하지 마세요."
))
READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
UI = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)
NAVIGATE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
SEND = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)


def output(value):
    return json.dumps(value, ensure_ascii=False, indent=2)


def ops():
    from .operations import KakaoTalkWin
    return KakaoTalkWin()


def with_image(result, always=False):
    content: list[TextContent | ImageContent] = [TextContent(type="text", text=output(result))]
    if result.get("status") == "ok" and result.get("imagePath") and always:
        content.append(Image(path=result["imagePath"]).to_image_content())
    return content


@mcp.tool(annotations=READ)
def kakaotalk_win_status() -> str:
    """카카오톡 PC 실행·서명·잠금 상태, 열린 대화창 수, 클립보드 기록 설정, 전송 안전 한도를 확인합니다.

    메시지를 읽거나 창을 바꾸지 않습니다. warnings가 있으면 사용자에게 알리세요.
    """
    return output(ops().status())


@mcp.tool(annotations=READ)
def kakaotalk_win_windows() -> str:
    """열린 대화창의 정확한 제목, 최소화 여부, 입력 중인 초안 유무를 반환합니다.

    방을 열거나 창을 올리지 않으며 메시지 본문·초안 내용은 읽지 않습니다.
    여기 나온 windowTitle을 다른 도구에 그대로 전달하세요. 닫힌 방 목록은 kakaotalk_win_rooms를 쓰세요.
    """
    return output(ops().windows())


@mcp.tool(annotations=UI)
def kakaotalk_win_rooms(query: str = "", include_image: bool = False) -> list[TextContent | ImageContent]:
    """메인 창 채팅 목록에서 현재 보이는 행을 캡처해 Windows 내장 OCR로 방 이름·미리보기·시각을 읽습니다.

    query를 주면 앱의 채팅방 검색칸에 넣은 결과를 읽고, 끝나면 검색칸을 원래대로 되돌립니다.
    보이는 행만이며 전체 목록이 아닙니다. OCR 오인식이 있을 수 있으니 open 전에 이름을 확인하세요.
    include_image=True거나 OCR이 실패하면 목록 이미지를 함께 반환합니다.
    """
    result = ops().rooms(query=query)
    return with_image(result, always=include_image or result.get("ocr", {}).get("status") != "ok")


@mcp.tool(annotations=UI)
def kakaotalk_win_search_rooms(query: str, include_image: bool = False) -> list[TextContent | ImageContent]:
    """앱의 채팅방 검색으로 query와 일치하는 방을 찾아 OCR 결과를 반환합니다. 검색칸은 원래대로 되돌립니다."""
    result = ops().search_rooms(query)
    return with_image(result, always=include_image or result.get("ocr", {}).get("status") != "ok")


@mcp.tool(annotations=UI)
def kakaotalk_win_open(window_title: str) -> str:
    """정확한 이름의 기존 방을 앱의 채팅방 검색으로 엽니다. 창을 앞으로 띄우지 않습니다.

    이미 열려 있으면 다시 열지 않습니다. 검색 첫 결과의 제목이 다르면 그 창을 닫고 중단합니다.
    새 방을 만들지 않습니다. existingHistory=false인 방에는 전송할 수 없습니다.
    """
    return output(ops().open_room(window_title))


@mcp.tool(annotations=UI)
def kakaotalk_win_close(window_title: str) -> str:
    """대화창만 닫습니다(방 나가기 아님). 입력 중인 초안이 있으면 닫지 않고 중단합니다."""
    return output(ops().close_room(window_title))


@mcp.tool(annotations=READ)
def kakaotalk_win_read(window_title: str, limit: int = 50, offset: int = 0, snapshot_id: str = "") -> str:
    """열린 대화창에 불러와진 메시지를 날짜·보낸 사람·시각·본문으로 읽습니다. limit 1..500.

    최신 메시지부터 페이지를 고르고, 페이지 안은 시간순입니다. 다음 페이지는 nextOffset/snapshotId를 넘기세요.
    사진·동영상·이모티콘·파일은 kind=media/mediaType으로만 표시됩니다(내용은 read_attachment).
    창을 앞으로 띄우거나 초안을 바꾸지 않으며, 사용자의 클립보드는 읽은 뒤 원래대로 복원합니다.
    더 이전 대화는 kakaotalk_win_load_more로 불러온 뒤 다시 읽으세요.
    """
    return output(ops().read_messages(window_title, limit, offset, snapshot_id))


@mcp.tool(annotations=NAVIGATE)
def kakaotalk_win_read_attachment(window_title: str, output_path: str | None = None,
                                  ocr: bool = False) -> list[TextContent | ImageContent]:
    """대화창을 현재 화면 그대로 PNG로 캡처해 반환합니다(사진·이모티콘·카드 확인용).

    가려진 창도 캡처하며, 최소화된 창은 활성화하지 않고 잠시 복원했다가 되돌립니다.
    원본 첨부 파일이 아닌 화면 해상도 이미지입니다. ocr=True일 때만 Windows OCR 결과를 덧붙이며 원문과 구분하세요.
    output_path는 절대 경로이고, 생략하면 임시 PNG입니다.
    """
    return with_image(ops().capture(window_title, output_path, ocr), always=True)


@mcp.tool(annotations=UI)
def kakaotalk_win_read_many(window_titles: list[str], limit: int = 50, open_if_needed: bool = True,
                            close_after: bool = False) -> str:
    """1~10개 방을 차례로(필요하면 열어서) 읽습니다. 클립보드가 하나라 병렬로 읽지 않습니다.

    방별 결과와 실패를 따로 반환하며 같은 시점의 스냅샷은 아닙니다.
    close_after=True면 이 호출이 새로 연 창만 닫습니다. 기존 창과 초안은 보존합니다.
    """
    return output(ops().read_many(window_titles, limit, open_if_needed, close_after))


@mcp.tool(annotations=NAVIGATE)
def kakaotalk_win_load_more(window_title: str, pages: int = 1) -> str:
    """대화 목록을 위로 스크롤해 이전 메시지를 최대 pages(1..10)번 불러오고, 끝나면 맨 아래로 되돌립니다.

    늘어나지 않았다고 대화의 시작에 도달했다고 판단하지 마세요(historyComplete=false).
    """
    return output(ops().load_more(window_title, pages))


@mcp.tool(annotations=NAVIGATE)
def kakaotalk_win_search(window_title: str, query: str, limit: int = 100, load_pages: int = 0,
                         case_sensitive: bool = False, context_rows: int = 2) -> str:
    """불러와진 메시지에서 본문·보낸 사람을 문자 그대로 검색하고 앞뒤 context_rows개를 함께 반환합니다.

    load_pages=1..10이면 먼저 이전 대화를 불러옵니다. 앱의 검색 UI는 쓰지 않습니다.
    결과는 searchScope 범위에 한정되며 전체 과거의 모든 일치를 보장하지 않습니다.
    """
    return output(ops().search_messages(window_title, query, limit, load_pages, case_sensitive, context_rows))


@mcp.tool(annotations=SEND)
def kakaotalk_win_send(window_title: str, text: str, operation_id: str, wait_seconds: float = 8,
                       require_self: bool = False) -> str:
    """기존 방에 텍스트 한 건을 보냅니다. 사용자가 요청한 방과 본문만 보내세요.

    사용자가 확인창 모드(confirmSend=dialog)를 켰다면 PC에 확인창이 뜨고 [예]를 눌러야 전송됩니다.
    operation_id는 요청마다 한 번 만들어 재사용하세요. 같은 ID는 다시 입력하지 않고 결과만 다시 확인합니다.
    대화 기록이 없는 방, 초안이 있는 방, 전송 간격·시간당·일일 한도 초과, 같은 긴 문구의 다른 방 전송은 차단됩니다.
    observed_in_window는 새 메시지와 빈 입력창을 관찰했다는 뜻이며 서버 수신·상대 읽음 확인이 아닙니다.
    outcome_unknown이나 타임아웃이면 재전송하지 말고 kakaotalk_win_send_result로만 확인하세요.
    require_self=True는 '나와의 채팅'(설정한 내 이름과 제목·보낸 사람이 모두 같음)만 허용합니다.
    """
    return output(ops().send_message(window_title, text, operation_id, wait_seconds, require_self))


@mcp.tool(annotations=READ)
def kakaotalk_win_send_result(operation_id: str, wait_seconds: float = 0) -> str:
    """전송 기록을 조회하고, 확정되지 않았으면 읽기만으로 다시 확인합니다. 창을 열거나 다시 보내지 않습니다."""
    return output(ops().send_result(operation_id, wait_seconds))


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
