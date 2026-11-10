# kakaotalk-mcp-win32message

AI 에이전트가 Windows PC 카카오톡의 대화를 읽고, 찾고, 답장할 수 있게 해 주는 MCP 서버입니다.
컴퓨터에 설치된 공식 카카오톡 앱을 사용하여 작동합니다.

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Platform](https://img.shields.io/badge/platform-Windows%2010%20%7C%2011-0078D6)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![MCP](https://img.shields.io/badge/MCP-stdio-8A2BE2)

> [!WARNING]
> 카카오와 관계없는 비공식 도구입니다. 카카오는 이용 환경과 이용 패턴 분석만으로도 보호조치를 할 수 있다고 고지합니다.
> 이 도구는 카카오톡 서버와 통신하는 방식이 아니며, Windows의 기본 제공 기능만으로 구성되어 있습니다.
> 이용제한 위험과 다른 사람의 대화를 다루는 책임은 사용하는 사람에게 있습니다.

## 이런 일을 맡길 수 있습니다

Claude 같은 AI 클라이언트에 연결하여 카카오톡과 관련된 업무를 지시할 수 있습니다.

- "프로젝트방에서 오늘 나온 결정 사항만 정리해 줘"
- "지난주에 '견적' 얘기가 나온 부분을 찾아서 앞뒤 대화까지 보여 줘"
- "안 읽은 방이 뭐가 있는지 알려 주고, 김 대리 방에는 '자료 확인했습니다'라고 답해 줘"
- "이 방에 올라온 사진에 뭐가 적혀 있는지 확인해 줘"

## 특징

- **공식 앱만 사용합니다.** 카카오 서버나 통신 규약에 직접 접속하지 않고, 카카오톡의 DB·파일·메모리도 건드리지 않습니다. 사람이 카카오톡 창에서 하는 동작(복사, 검색, 입력, Enter)을 그 창에 보낼 뿐입니다.
- **화면을 뺏지 않습니다.** 대화창이 다른 창 뒤에 있거나 최소화돼 있어도 읽고 보낼 수 있어서, 다른 작업을 하는 중에도 쓸 수 있습니다.
- **보낸 사람과 시각까지 정리해 줍니다.** 메시지마다 날짜, 보낸 사람, 시각, 본문을 나눠서 돌려주고 사진·파일·이모티콘도 구분합니다.
- **계정 보호 장치가 켜져 있습니다.** 전송 속도와 횟수를 제한하고, 같은 글을 여러 방에 뿌리지 못하게 막습니다. 새 방 만들기나 친구 추가 같은 위험한 동작은 기능 자체를 넣지 않았습니다.

## 빠른 시작

### 준비물

- Windows 10 또는 11
- [Python](https://www.python.org/downloads/) 3.10 이상
- 로그인된 공식 카카오톡 PC 앱
- MCP를 지원하는 AI 클라이언트 (Claude Code, Claude Desktop 등)

### 1. 설치

PowerShell에서 실행합니다. 원하는 폴더에 가상 환경을 만들고 최신 릴리스를 설치합니다.

```powershell
py -3 -m venv "$env:USERPROFILE\.kakaotalk-win"
& "$env:USERPROFILE\.kakaotalk-win\Scripts\pip" install "https://github.com/Kwon-Sungeon/kakaotalk-mcp-win32message/releases/download/v0.1.0/kakaotalk_win-0.1.0-py3-none-any.whl"
```

소스코드를 수정하려면 git 저장소를 받아 설치합니다.

```powershell
git clone https://github.com/Kwon-Sungeon/kakaotalk-mcp-win32message.git
cd kakaotalk-mcp-win32message
py -3 -m venv .venv
.venv\Scripts\pip install -e .
```

### 2. 상태 확인

카카오톡을 켜 둔 상태에서 실행합니다.

```powershell
& "$env:USERPROFILE\.kakaotalk-win\Scripts\kakaotalk-win" status
```

`warnings`가 비어 있으면 준비가 끝난 것입니다.
선택 사항으로 내 카카오톡 프로필 이름을 알려 주면, 보낸 메시지의 작성자가 나인지까지 확인합니다.

```powershell
& "$env:USERPROFILE\.kakaotalk-win\Scripts\kakaotalk-win" config --self-name "내 프로필 이름"
```

### 3. AI 클라이언트에 등록

`kakaotalk-win mcp-config`를 실행하면 설치 경로에 맞는 설정이 출력됩니다.

**Claude Code**

```powershell
claude mcp add kakaotalk-win -- "C:\Users\<사용자>\.kakaotalk-win\Scripts\kakaotalk-win.exe" mcp
```

**Claude Desktop** (`%APPDATA%\Claude\claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "kakaotalk-win": {
      "command": "C:\\Users\\<사용자>\\.kakaotalk-win\\Scripts\\kakaotalk-win.exe",
      "args": ["mcp"]
    }
  }
}
```

Cursor, VS Code 등 다른 클라이언트도 같은 명령(`kakaotalk-win.exe mcp`)을 stdio 서버로 등록하면 됩니다.

### 4. 써 보기

클라이언트를 다시 시작한 뒤 "카카오톡에 열려 있는 대화창 알려 줘"라고 해 보세요.

## 도구

| 도구 | 하는 일 | 앱 상태 변경 |
|---|---|---|
| `kakaotalk_win_status` | 실행·잠금·서명 상태, 전송 한도, 경고 확인 | 없음 |
| `kakaotalk_win_windows` | 열려 있는 대화창 목록과 초안 유무 | 없음 |
| `kakaotalk_win_rooms` | 채팅 목록의 방 이름·미리보기·시각·안 읽음 표시 (화면 글자 인식, 여러 페이지) | 목록을 내렸다가 맨 위로 되돌림 |
| `kakaotalk_win_search_rooms` | 앱의 채팅방 검색으로 방 찾기 | 검색칸을 썼다가 되돌림 |
| `kakaotalk_win_open` | 정확한 이름의 기존 방 열기 | 대화창 열림 |
| `kakaotalk_win_close` | 대화창 닫기 (방 나가기 아님, 초안 있으면 중단) | 대화창 닫힘 |
| `kakaotalk_win_read` | 메시지 읽기 (날짜·보낸 사람·시각·본문, 페이지 지원) | 없음 |
| `kakaotalk_win_read_full_text` | 500자가 넘는 메시지의 전체 본문 읽기 ('전체보기') | 전체보기 창을 열었다가 닫음 |
| `kakaotalk_win_read_many` | 방 최대 10개를 차례로 읽기 | 필요하면 열고 닫음 |
| `kakaotalk_win_read_attachment` | 대화창을 이미지로 캡처해 사진·이모티콘 확인 | 없음 |
| `kakaotalk_win_load_more` | 위로 스크롤해 이전 대화 불러오기 | 스크롤 후 원위치 |
| `kakaotalk_win_search` | 불러온 대화에서 검색하고 앞뒤 맥락 함께 반환 | 없음 |
| `kakaotalk_win_send` | 기존 방에 텍스트 한 건 전송 | 메시지 전송 |
| `kakaotalk_win_send_result` | 전송 결과 다시 확인 (다시 보내지 않음) | 없음 |

인수와 결과 형식은 각 도구 설명과 [docs/windows.md](docs/windows.md)에 있습니다.

## 작동 원리

```text
 AI 클라이언트  ── MCP(stdio) ──▶  kakaotalk-win  ── 창 메시지 ──▶  카카오톡 PC (공식 앱)
 (Claude 등)                     (내 PC에서 실행)                        │
                                       ▲                                 │ 앱이 스스로 복사
                                       └──────── 클립보드 텍스트 ◀────────┘
```

대화창, 메시지 목록, 입력창이 각각 별도의 창으로 나뉘어 있어서, kakaotalk-win은 그 창을 찾아 필요한 동작을 직접 보냅니다.

- **읽기**: 메시지 목록에 Ctrl+A, Ctrl+C를 보내면 카카오톡이 대화를 `[보낸 사람] [오후 3:05] 본문` 형식으로 클립보드에 복사합니다. 이 텍스트를 메시지 단위로 나눕니다. 방 하나에 0.1초가 채 걸리지 않습니다.
- **방 목록**: 목록 창은 글자를 내주지 않아서 창을 캡처한 뒤 Windows에 들어 있는 OCR로 읽습니다. 목록을 한 화면씩 내려 가며 읽고, 끝나면 맨 위로 되돌립니다. 방을 열 때는 OCR 결과와 상관없이 앱의 검색으로 열고, 열린 창의 제목이 요청한 이름과 정확히 같은지 확인합니다.
- **긴 메시지**: 카카오톡은 500자가 넘는 메시지를 앞 500자만 보여 줍니다. 메시지 아래 '전체보기' 위치에 클릭 메시지를 보내 열리는 창에서 전체 본문을 복사하고, 그 창을 바로 닫습니다. 마우스 커서는 움직이지 않습니다.
- **전송**: 입력창에 글을 넣고, 다시 읽어서 똑같이 들어갔는지 확인한 뒤 Enter를 한 번만 보냅니다. 그다음 대화를 다시 읽어 새 메시지가 올라오고 입력창이 비었는지 확인합니다. 확인하지 못하면 다시 보내지 않고 `outcome_unknown`으로 알립니다.
- **찾는 방식**: 창은 카카오톡 내부 이름(`ChatRoomListView_…` 등)으로 찾기 때문에 한국어·영어 UI에서 똑같이 동작합니다.

자세한 구조는 [docs/windows.md](docs/windows.md)에 있습니다.

## 계정 보호 장치

카카오 약관과 운영정책에서 이용제한 사유로 명시한 조항을 바탕으로 구성되어 있습니다. 관련 조항은 [docs/windows.md](docs/windows.md#이용제한을-피하는-설계)에서 확인하실 수 있습니다.

| 장치 | 기본값 |
|---|---|
| 전송 간격 | 10초 이상 |
| 전송 횟수 | 시간당 20건, 하루 100건, 한 방에 시간당 10건 |
| 한 시간에 보낼 수 있는 방 | 10개 |
| 같은 글 반복 | 30자를 넘거나 링크가 든 글은 24시간 동안 다른 방에 못 보냄 |
| 대상 | 대화 기록이 있는 기존 방만 |
| 방 열기 | 분당 6회 |

한도는 설정으로 더 엄격하게만 바꿀 수 있고 기본값보다 높일 수는 없습니다.
새 방 만들기, 친구 추가, 초대, 오픈채팅 참여·개설, 여러 방 동시 발송, 예약 발송은 기능이 없습니다.

대화방에 보호조치나 이용제한 안내가 보이거나, 확인되지 않은 전송이 두 번 이어지면 모든 전송을 멈춥니다. 이 안전 정지는 사람이 카카오톡 화면을 확인한 뒤 터미널에서 직접 풀어야 합니다. AI 도구로는 풀 수 없습니다.

```powershell
kakaotalk-win reset-stop
```

전송 전 확인창은 기본으로 꺼져 있습니다. 이때는 AI 클라이언트가 도구를 쓸 때마다 묻는 승인 창이 사람의 확인을 대신합니다.
보낼 때마다 받는 방과 본문을 직접 확인하고 싶다면 확인창을 켜 두세요.

```powershell
kakaotalk-win config --confirm dialog
```

## 설정

```powershell
kakaotalk-win config                                      # 현재 설정과 실제 적용 한도
kakaotalk-win config --self-name "내 프로필 이름"
kakaotalk-win config --limit per_hour=10 --limit per_day=30
kakaotalk-win config --confirm dialog                     # 전송 전 확인창 켜기
```

| 환경 변수 | 용도 |
|---|---|
| `KAKAOTALK_WIN_STATE` | 상태 파일 위치 (기본 `%LOCALAPPDATA%\kakaotalk-win`) |
| `KAKAOTALK_WIN_SELF_NAME` | 내 프로필 이름 |
| `KAKAOTALK_WIN_CONFIRM` | `off` 또는 `dialog` |
| `KAKAOTALK_WIN_PLACEHOLDERS` | 빈 입력창 안내 문구 추가 (`\|`로 구분, 다른 언어 UI용) |

상태 파일에는 전송 한도 기록과 전송 결과 기록만 남습니다. 메시지 본문은 해시로만 저장합니다.

## 명령줄

MCP 없이 명령줄에서도 같은 기능을 쓸 수 있습니다.

```powershell
kakaotalk-win windows
kakaotalk-win read --window-title "정확한 방 이름" --limit 50
kakaotalk-win call read_many --args-file C:\path\args.json
kakaotalk-win call send_message --args-file C:\path\request.json
```

`call`은 Python API(`kakaotalk_win.operations.KakaoTalkWin`)의 메서드 이름과 인수를 그대로 받습니다. 보낼 글은 명령줄에 직접 쓰지 말고 UTF-8 JSON 파일로 넘기세요.

## 자주 묻는 질문

**카카오톡 창을 화면에 띄워 둬야 하나요?**
아니요. 카카오톡이 실행돼 있고 읽을 대화창이 열려 있기만 하면 됩니다. 창이 가려져 있거나 최소화돼 있어도 됩니다. 닫힌 방은 `kakaotalk_win_open`으로 먼저 엽니다.

**계정이 정지될 수 있나요?**
가능성을 0으로 만들 수는 없습니다. 공식 앱 창에 메시지를 보내는 방식만으로 제한됐다는 보고는 찾지 못했습니다. 그래도 카카오는 이용 패턴을 분석해 보호조치를 합니다. 그래서 사람이 평소에 쓰는 속도와 방식을 넘지 않도록 한도를 걸어 두었습니다.

**대화 내용은 어디로 가나요?**
kakaotalk-win은 대화를 내 PC 밖으로 보내지 않고 저장하지도 않습니다. 다만 AI 클라이언트는 도구 결과를 AI 모델에 보내므로, 읽은 대화는 사용하는 AI 서비스에 전달됩니다. 다른 사람의 대화를 다룰 때는 이 점을 고려하세요.

**전체 대화 기록을 읽을 수 있나요?**
대화창에 불러온 메시지까지만 읽습니다. `load_more`로 더 불러올 수 있지만, 처음까지 다 읽었다고 보장하지는 않습니다. 오래된 대화 전체가 필요하면 카카오톡의 공식 '대화 내용 내보내기'를 쓰는 편이 정확합니다.

**사진이나 파일도 보낼 수 있나요?**
아니요. 텍스트 전송만 지원합니다. 받은 사진은 `read_attachment`로 대화창을 캡처해서 확인할 수 있습니다.

**`chats_tab_not_visible`이 나와요.**
카카오톡 메인 창에서 채팅 탭을 한 번 눌러 둔 뒤 다시 시도하세요. 방 목록 기능은 채팅 탭이 열려 있어야 합니다.

## 이런 용도에는 맞지 않습니다

- **대량 발송이나 마케팅 메시지**: 의도적으로 막아 두었고, 그런 사용은 이용제한 사유입니다.
- **서버에서 24시간 도는 봇**: 실제 PC에서 로그인된 카카오톡이 필요합니다. 가상 머신이나 원격 데스크톱 환경은 카카오가 비정상 이용 환경으로 볼 수 있으니 피하세요. 원격 데스크톱 세션이면 `status`가 경고합니다.
- **대화 백업이나 기록 수집**: 다른 사람의 개인정보를 저장하는 일이라 기능을 넣지 않았습니다.

## 개발

```powershell
.venv\Scripts\python -m unittest discover -s tests -v
```

단위 테스트는 가짜 카카오톡 창으로 전송 절차를 검사하며 실제 카카오톡은 건드리지 않습니다.

```text
src/kakaotalk_win/    구현 (Win32 호출은 ctypes만 사용)
  win32.py            Win32 API 바인딩
  ui.py               카카오톡 창 구조 지도
  clipboard.py        클립보드 저장·복원
  parser.py           복사된 대화 텍스트 해석
  capture.py          창 캡처, Windows OCR
  operations.py       기능별 동작
  sender.py           전송 절차와 결과 확인
  safety.py           한도, 안전 정지, 상태 파일
  mcp_server.py       MCP 도구
  cli.py              kakaotalk-win 명령
docs/windows.md       상세 문서 (구조, 전송 절차, 이용제한 대응 근거)
skills/kakaotalk-win/ 에이전트 스킬
```

확인한 환경은 Windows 11, 카카오톡 PC 26.9.2(한국어 UI)입니다. 카카오톡이 업데이트되면 창 구조나 복사 형식이 바뀔 수 있으니, 동작이 이상하면 버전과 함께 이슈로 알려 주세요.

## 라이선스

[MIT](LICENSE)
