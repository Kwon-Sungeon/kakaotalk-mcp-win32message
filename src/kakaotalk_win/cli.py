"""kakaotalk-win: CLI and MCP entry point for KakaoTalk PC on Windows."""

import argparse
import json
import sys
from pathlib import Path

METHODS = ("status", "windows", "rooms", "search_rooms", "open_room", "close_room", "read_messages",
           "read_many", "load_more", "search_messages", "capture", "send_message", "send_result")
OK_STATUSES = ("ok", "partial", "observed_in_window")


def _print(value):
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(value, ensure_ascii=False, indent=2))


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


def main(argv=None):
    parser = argparse.ArgumentParser(prog="kakaotalk-win", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("mcp", help="Serve the tools over MCP stdio")
    commands.add_parser("status", help="App, window, clipboard and safety status (reads no messages)")
    commands.add_parser("windows", help="List open chat windows")
    command = commands.add_parser("read", help="Read the loaded messages of an open chat window")
    command.add_argument("--window-title", required=True)
    command.add_argument("--limit", type=int, default=50)
    command.add_argument("--offset", type=int, default=0)
    command.add_argument("--snapshot-id", default="")
    command = commands.add_parser("call", help="Call an operation with JSON arguments from a file")
    command.add_argument("method", choices=METHODS)
    command.add_argument("--args-file", type=Path)
    command = commands.add_parser("config", help="Show or change local settings")
    command.add_argument("--self-name", help="Your KakaoTalk profile name (enables author checks, require_self)")
    command.add_argument("--confirm", choices=("off", "dialog"),
                         help="Extra Yes/No dialog before each send (default: off)")
    command.add_argument("--limit", action="append", metavar="KEY=VALUE", help="Make a send limit stricter")
    commands.add_parser("reset-stop", help="Clear a safety stop after checking KakaoTalk (interactive)")
    commands.add_parser("mcp-config", help="Print MCP client configuration for this installation")
    args = parser.parse_args(argv)

    if args.command == "mcp":
        from .mcp_server import main as serve
        serve()
        return 0
    sending = None
    try:
        if args.command == "config":
            value = _config(args)
        elif args.command == "reset-stop":
            value = _reset_stop()
        elif args.command == "mcp-config":
            value = _mcp_config()
        else:
            from .operations import KakaoTalkWin
            ops = KakaoTalkWin()
            if args.command == "status":
                value = ops.status()
            elif args.command == "windows":
                value = ops.windows()
            elif args.command == "read":
                value = ops.read_messages(args.window_title, args.limit, args.offset, args.snapshot_id)
            else:
                request = json.loads(args.args_file.read_text(encoding="utf-8")) if args.args_file else {}
                if not isinstance(request, dict):
                    raise ValueError("Arguments must be a JSON object")
                if args.method == "send_message":
                    sending = request.get("operation_id")
                method = "capture" if args.method == "capture" else args.method
                value = getattr(ops, method)(**request)
        _print(value)
        return 0 if value.get("status") in OK_STATUSES else 1
    except Exception as error:
        _print({"status": "outcome_unknown" if sending else "failed",
                **({"operationId": sending, "retrySafe": False} if sending else {}),
                "errorType": type(error).__name__,
                "detail": str(error) if isinstance(error, (ValueError, PermissionError)) else (
                    "Operation failed; check send_result with the same operation_id before retrying"
                    if sending else str(error)[:300])})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
