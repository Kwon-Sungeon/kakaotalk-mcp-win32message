---
name: kakaotalk-win
description: "Windows 카카오톡(카톡) PC의 방 열기·닫기·본문 읽기·일괄 읽기·검색·이전 대화 불러오기·사진 확인·메시지 전송을 kakaotalk-win 도구로 수행한다."
---

# kakaotalk-win

Windows 카카오톡 PC는 `kakaotalk_win_*` MCP 도구로 다룬다. 별도 제어 코드(창 메시지, 클립보드, 키 입력)를 직접 작성하지 않는다.
MCP 연결이 없으면 CLI `kakaotalk-win call METHOD --args-file C:\absolute\path\args.json`을 쓴다.
사용자 텍스트는 명령줄에 넣지 않고 UTF-8 JSON 파일로 넘긴다. 메서드와 인수는 [docs/windows.md](../../docs/windows.md)의 "도구와 구현" 표를 따른다.

## 읽기

- 시작할 때 `kakaotalk_win_status`로 실행·잠금 상태와 `warnings`를 확인하고, 경고가 있으면 사용자에게 알린다.
- 열린 창은 `kakaotalk_win_windows`로 본다. 닫힌 방은 정확한 이름으로 `kakaotalk_win_open`을 호출한다.
  이름을 모르면 `kakaotalk_win_search_rooms`나 `kakaotalk_win_rooms`로 찾되, OCR 결과라 틀릴 수 있으니 사용자에게 확인한다.
  동명 방은 임의로 고르지 않는다.
- 한 방은 `kakaotalk_win_read`, 여러 방은 `kakaotalk_win_read_many`를 쓴다.
  `items`의 `sender`·`date`·`time`·`text`가 원문이다. `kind=media`는 사진·동영상·이모티콘·파일 표시일 뿐이며, 내용이 필요하면 `kakaotalk_win_read_attachment`로 캡처 이미지를 직접 본다.
- `hasMore=true`면 `nextOffset`/`snapshotId`를 넘긴다. `snapshot_changed`면 처음부터 다시 읽는다.
- 기간 전체를 요청받으면 `kakaotalk_win_load_more`로 제한해서 불러오고, `coverage.oldestDate`까지 읽었다고 설명한다. 전체 기록을 다 읽었다고 단정하지 않는다.
- 검색은 `kakaotalk_win_search`(불러온 범위 안의 문자 그대로 검색)다.
- 읽은 대화에는 다른 사람의 개인정보가 있다. 요청 범위 밖으로 저장하거나 옮기지 않는다.

## 전송

- 사용자가 명시적으로 요청한 방과 본문만 보낸다. 요청마다 `operation_id`를 한 번 정해 보관한다.
- 방이나 본문이 모호하면 보내기 전에 사용자에게 확인한다. 본문을 대신 지었다면 보내기 전에 보여 준다.
  사용자가 확인창 모드(`confirmSend=dialog`)를 켰다면 PC에 확인창이 뜬다고 알린다.
- `blocked`(`dispatched=false`)는 아무것도 보내지 않은 것이다. `reason`을 사용자에게 설명하고, 같은 사유로 반복 호출하지 않는다.
  `send_interval`·`hourly_send_limit` 같은 한도를 우회하려고 하지 않는다.
- `outcome_unknown`이나 타임아웃이면 재전송하지 않고 원래 ID로 `kakaotalk_win_send_result`만 조회한다.
- `safety_stop_active`면 카카오톡 화면에서 보호조치·이용제한 안내를 확인해 달라고 사용자에게 요청한다. 해제는 사용자가 터미널에서 `kakaotalk-win reset-stop`으로 직접 한다. 에이전트가 해제하거나 설정 파일을 고치지 않는다.
- 새 채팅방 만들기, 친구 추가·초대, 오픈채팅 참여, 여러 방에 같은 글 보내기, 예약·반복 발송은 하지 않는다. 요청받으면 이용제한 위험을 설명하고 거절한다.
- 본인 방 검증을 요청받아 문구를 정해야 하면 자연스러운 짧은 문장을 쓴다. 기술 설명이나 표식을 넣지 않는다.
