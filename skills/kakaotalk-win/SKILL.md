---
name: kakaotalk-win
description: "Windows PC 카카오톡을 kakaotalk-win 도구로 다룬다. 대화 읽기와 요약, 여러 방 훑어보기, 옛 대화 불러오기, 메시지 검색, 사진 확인, 방 열고 닫기, 답장 보내기를 요청받았을 때 쓴다."
---

# kakaotalk-win 사용 지침

카카오톡 PC는 `kakaotalk_win_*` MCP 도구로만 조작한다. 창 메시지, 클립보드, 키 입력을 흉내 내는 코드를 새로 쓰지 않는다.
MCP가 연결돼 있지 않을 때는 `kakaotalk-win call METHOD --args-file C:\absolute\path\args.json`으로 같은 일을 한다.
보낼 글처럼 사용자가 준 텍스트는 명령줄 인수 대신 UTF-8 JSON 파일에 담는다. METHOD와 인수 이름은 [docs/windows.md](../../docs/windows.md)의 "도구와 구현" 표를 따른다.

## 시작하기 전에

1. `kakaotalk_win_status`를 먼저 부른다. 카카오톡이 꺼져 있거나 잠겨 있으면 사용자에게 켜거나 풀어 달라고 하고, `warnings`는 빠짐없이 전한다.
2. 떠 있는 대화창은 `kakaotalk_win_windows`로 본다. 여기 나온 `windowTitle`이 다른 도구에 넘길 방 이름이다.
3. 닫혀 있는 방은 `kakaotalk_win_open`에 정확한 이름을 넣어 연다.
   이름이 확실하지 않으면 `kakaotalk_win_search_rooms`나 `kakaotalk_win_rooms`로 후보를 찾는다. 후보 이름은 화면 글자 인식 결과라 틀릴 수 있으니 사용자에게 맞는지 묻는다.
   이름이 같은 방이 여럿이면 그중 하나를 골라 진행하지 않는다.
4. "최근 1시간에 온 카톡"처럼 여러 방의 새 대화를 물으면 `kakaotalk_win_recent`에 `minutes`를 주어 부른다.
   기본값은 열려 있는 대화창만 읽는다. 닫힌 방은 `notRead`에 미리보기와 안 읽은 개수(`unread`)만 나온다.
   닫힌 방 내용까지 필요하면 "방을 열면 읽음 처리된다"고 알리고 동의를 받은 뒤 `open_rooms=true`로 다시 부른다. 동의 없이 켜지 않는다.
   `listCoverage.complete=false`면 `list_pages`를 늘리고, 방별 `mayBeIncomplete=true`면 앞부분이 빠졌을 수 있다고 밝힌다.

## 읽고 찾기

- 방 하나는 `kakaotalk_win_read`, 여러 방은 `kakaotalk_win_read_many`로 읽는다.
- 메시지 원문은 `items`의 `sender`, `date`, `time`, `text`다. `kind=media`는 사진·동영상·이모티콘·파일이 있었다는 표시뿐이다.
  내용을 봐야 하면 `kakaotalk_win_read_attachment`가 돌려준 캡처 이미지를 직접 본다.
- `textTruncated=true`인 메시지는 500자가 넘어서 앞부분만 받은 것이다. 내용이 필요하면 `kakaotalk_win_read_full_text`로 전체 본문을 받는다. 받지 못했으면 뒷부분이 빠졌다고 밝힌다.
- 응답에 `hasMore=true`가 있으면 `nextOffset`을 `offset`에, `snapshotId`를 `snapshot_id`에 넣어 이어 읽는다.
  그사이 대화가 바뀌어 `snapshot_changed`가 오면 offset 0부터 새로 읽는다.
- "지난주 대화 전부"처럼 기간을 요청받으면 `kakaotalk_win_load_more`로 몇 번만 더 불러온다. 답할 때는 `coverage.oldestDate`부터 읽었다고 밝히고, 대화의 처음까지 다 봤다고 말하지 않는다.
- 특정 말을 찾을 때는 `kakaotalk_win_search`를 쓴다. 불러와 있는 메시지 안에서 글자 그대로 찾는다.
- 대화에는 다른 사람의 개인정보가 들어 있다. 요청에 필요한 만큼만 쓰고, 따로 저장하거나 다른 곳으로 옮기지 않는다.

## 보내기

- 사용자가 직접 지정한 방에, 사용자가 원한 내용만 보낸다. 전송 요청마다 `operation_id`를 새로 정하고, 그 요청을 다시 확인할 때는 같은 값을 쓴다.
- 어느 방인지, 무슨 내용인지 애매하면 보내기 전에 묻는다. 문장을 대신 써 줬다면 보내기 전에 보여 주고 확인받는다.
  사용자가 확인창 모드(`confirmSend=dialog`)를 켜 두었다면 PC에 확인창이 뜬다고 미리 말한다.
- `blocked`이면서 `dispatched=false`인 결과는 아무것도 나가지 않았다는 뜻이다. `reason`을 사용자에게 설명하고, 이유가 그대로인 채 같은 호출을 되풀이하지 않는다.
  `send_interval`, `hourly_send_limit` 같은 한도는 피해 가려 하지 않는다.
- `outcome_unknown`이나 시간 초과가 나오면 다시 보내지 않는다. 같은 `operation_id`로 `kakaotalk_win_send_result`만 부른다.
- `safety_stop_active`가 나오면 카카오톡 화면에 보호조치나 이용제한 안내가 떠 있는지 사용자에게 확인을 부탁한다.
  정지는 사용자가 터미널에서 `kakaotalk-win reset-stop`으로 직접 푼다. 에이전트가 풀거나 설정 파일을 고치지 않는다.
- 새 방 만들기, 친구 추가와 초대, 오픈채팅 참여, 여러 방에 같은 글 보내기, 예약이나 반복 발송은 하지 않는다. 이런 요청을 받으면 계정이 이용제한될 수 있다고 설명하고 거절한다.
- 동작 확인용으로 '나와의 채팅'에 보낼 문장을 직접 정해야 하면 평범하고 짧은 문장을 쓴다. 테스트 표식이나 기술 용어는 넣지 않는다.
