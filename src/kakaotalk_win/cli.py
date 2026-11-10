"""kakaotalk-win: CLI and MCP entry point for KakaoTalk PC on Windows."""

import argparse
import json
import sys
from pathlib import Path

METHODS = ("status", "windows", "rooms", "search_rooms", "open_room", "close_room", "read_messages",
           "read_many", "load_more", "search_messages", "read_full_text", "capture", "send_message", "send_result")
SUCCESS = frozenset({"ok", "partial", "observed_in_window"})


def _emit(value):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdout.write(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def _human_confirms(prompt, word):
    """Safety-loosening commands need a person at an interactive terminal (not an agent's shell)."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise PermissionError("This command must be run by a person in an interactive terminal.")
    print(prompt)
    return input(f"계속하려면 {word} 를 입력하세요: ").strip() == word


def _config(args):
    from . import safety
    changes = {}
    if args.self_name is not None:
        changes["selfName"] = args.self_name.strip() or None
    if args.confirm is not None:
        if args.confirm == "off" and not _human_confirms(
                "전송 전 확인창을 끄고 MCP 클라이언트의 도구 승인에만 맡깁니다.\n"
                "클라이언트에서 전송 도구를 '항상 허용'하면 사람의 확인 없이 전송됩니다.", "OFF"):
            return {"status": "cancelled"}
        changes["confirmSend"] = args.confirm
    if args.limit:
        limits = dict(safety.load_config().get("limits", {}) or {})
        for item in args.limit:
            key, _, value = item.partition("=")
            if key not in safety.CEILINGS:
                raise ValueError(f"Unknown limit {key!r}; choose from {', '.join(safety.CEILINGS)}")
            limits[key] = float(value) if "." in value else int(value)
        changes["limits"] = limits
    current = safety.save_config(changes) if changes else safety.load_config()
    return {"status": "ok", "config": current, "effectiveLimits": safety.limits(),
            "confirmSend": safety.confirm_mode(), "stateDirectory": str(safety.STATE_ROOT),
            "note": "Limits can only be made stricter than the built-in ceilings."}


def _reset_stop():
    from . import safety
    state = safety.breaker_state()
    if not state.get("tripped"):
        return {"status": "ok", "previous": state, "detail": "No safety stop was active."}
    if not _human_confirms(f"전송 안전 정지를 해제합니다. 사유: {state.get('reason')}\n"
                           "카카오톡 화면에서 보호조치·이용제한 안내가 없는지 먼저 확인하세요.", "RESET"):
        return {"status": "cancelled"}
    previous = safety.reset_breaker()
    safety.note_confirmed_send()
    return {"status": "ok", "previous": previous}


def _mcp_config():
    executable = Path(sys.executable).with_name("kakaotalk-win.exe")
    command = str(executable) if executable.exists() else sys.executable
    args = ["mcp"] if executable.exists() else ["-m", "kakaotalk_win", "mcp"]
    return {"status": "ok",
            "claudeDesktop": {"mcpServers": {"kakaotalk-win": {"command": command, "args": args}}},
            "claudeCode": " ".join(["claude", "mcp", "add", "kakaotalk-win", "--", f'"{command}"', *args])}


def _parser():
    parser = argparse.ArgumentParser(prog="kakaotalk-win", description=__doc__)
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True
    sub.add_parser("mcp", help="Serve the tools over MCP stdio")
    sub.add_parser("status", help="App, window, clipboard and safety status (reads no messages)")
    sub.add_parser("windows", help="List open chat windows")
    reader = sub.add_parser("read", help="Read the loaded messages of an open chat window")
    for name, options in (("--window-title", {"required": True, "metavar": "ROOM"}),
                          ("--limit", {"type": int, "default": 50}),
                          ("--offset", {"type": int, "default": 0}),
                          ("--snapshot-id", {"default": "", "metavar": "ID"})):
        reader.add_argument(name, **options)
    caller = sub.add_parser("call", help="Call an operation with JSON arguments from a file")
    caller.add_argument("method", choices=METHODS)
    caller.add_argument("--args-file", type=Path, metavar="JSON", help="UTF-8 file with one JSON object")
    settings = sub.add_parser("config", help="Show or change local settings")
    settings.add_argument("--self-name", help="Your KakaoTalk profile name (enables author checks, require_self)")
    settings.add_argument("--confirm", choices=("off", "dialog"),
                          help="Extra Yes/No dialog before each send (default: off)")
    settings.add_argument("--limit", action="append", metavar="KEY=VALUE", help="Make a send limit stricter")
    sub.add_parser("reset-stop", help="Clear a safety stop after checking KakaoTalk (interactive)")
    sub.add_parser("mcp-config", help="Print MCP client configuration for this installation")
    return parser


def _call(app, args):
    if args.command == "status":
        return app.status()
    if args.command == "windows":
        return app.windows()
    if args.command == "read":
        return app.read_messages(args.window_title, args.limit, args.offset, args.snapshot_id)
    payload = json.loads(args.args_file.read_text(encoding="utf-8")) if args.args_file else {}
    if type(payload) is not dict:
        raise ValueError("The --args-file must hold one JSON object of keyword arguments")
    return getattr(app, args.method)(**payload)


def _failure(error, operation_id):
    shown = isinstance(error, (ValueError, PermissionError))
    report = {"status": "failed", "errorType": error.__class__.__name__,
              "detail": str(error) if shown else str(error)[:300]}
    if operation_id:
        report.update(status="outcome_unknown", operationId=operation_id, retrySafe=False)
        if not shown:
            report["detail"] = "Operation failed; check send_result with the same operation_id before retrying"
    return report


def main(argv=None):
    args = _parser().parse_args(argv)
    if args.command == "mcp":
        from .mcp_server import main as serve
        serve()
        return 0
    local = {"config": lambda: _config(args), "reset-stop": _reset_stop, "mcp-config": _mcp_config}
    pending_operation = None
    try:
        if args.command in local:
            result = local[args.command]()
        else:
            from .operations import KakaoTalkWin
            if args.command == "call" and args.method == "send_message" and args.args_file:
                pending_operation = _peek_operation(args.args_file)
            result = _call(KakaoTalkWin(), args)
    except Exception as error:
        _emit(_failure(error, pending_operation))
        return 1
    _emit(result)
    return 0 if result.get("status") in SUCCESS else 1


def _peek_operation(path):
    """operation_id of a send request, read up front so it can be reported even if the send crashes."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload.get("operation_id") if type(payload) is dict else None


if __name__ == "__main__":
    sys.exit(main())
