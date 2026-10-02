"""Manual testing CLI: python -m openagy <command>"""

from __future__ import annotations

import argparse
import json
import sys

from .api import Openagy
from .client import RpcError
from .discover import DiscoveryError


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="openagy", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="show language server target + default model")
    sub.add_parser("models", help="list available models")
    sub.add_parser("quota", help="show quota usage per model group")

    sp = sub.add_parser("list", help="list conversations")
    sp.add_argument("--limit", type=int, default=25)

    sp = sub.add_parser("ask", help="ask a prompt (new or existing conversation)")
    sp.add_argument("prompt")
    sp.add_argument("--workspace", default=None, help="workspace dir for a new conversation (default: cwd)")
    sp.add_argument("--model", default=None, help="model id, e.g. gemini-3.8-flash-high")
    sp.add_argument("--conversation", default=None, help="existing conversation id")
    sp.add_argument("--raw", action="store_true", help="skip sanitization")
    sp.add_argument("--no-wait", action="store_true", help="send without waiting for the reply")
    sp.add_argument("--timeout", type=float, default=240.0)

    sp = sub.add_parser("read", help="read a conversation transcript")
    sp.add_argument("conversation_id")
    sp.add_argument("--tools", action="store_true", help="include tool steps")

    sp = sub.add_parser("wait", help="wait for a conversation to finish (bounded, resumable)")
    sp.add_argument("conversation_id")
    sp.add_argument("--timeout", type=float, default=240.0, help="max seconds to wait this call")

    sp = sub.add_parser("answer", help="answer a pending question in a conversation")
    sp.add_argument("conversation_id")
    sp.add_argument("--option", action="append", default=[], help="selected option id (repeatable)")
    sp.add_argument("--write-in", default=None, help="free-text answer")
    sp.add_argument("--skip", action="store_true", help="skip the question")
    sp.add_argument("--cancel", action="store_true", help="dismiss the question")
    sp.add_argument("--timeout", type=float, default=240.0)

    sp = sub.add_parser("approve", help="approve/deny a pending confirmation")
    sp.add_argument("conversation_id")
    sp.add_argument("--deny", action="store_true", help="deny instead of approve")
    sp.add_argument("--scope", default="PERMISSION_SCOPE_ONCE")
    sp.add_argument("--timeout", type=float, default=240.0)

    sp = sub.add_parser("undo", help="rewind a conversation ('Undo to this point')")
    sp.add_argument("conversation_id")
    sp.add_argument("--step", type=int, default=None, help="keep steps 0..N (default: drop last turn)")
    sp.add_argument("--keep-message", action="store_true", help="keep last user message, drop only its response")
    sp.add_argument("--with-files", action="store_true", help="also roll back workspace file edits")

    sp = sub.add_parser("reask", help="rewind the last turn and re-ask with a new prompt")
    sp.add_argument("conversation_id")
    sp.add_argument("prompt")
    sp.add_argument("--raw", action="store_true")
    sp.add_argument("--keep-message", action="store_true")
    sp.add_argument("--timeout", type=float, default=240.0)

    sp = sub.add_parser("delete", help="delete a conversation")
    sp.add_argument("conversation_id")

    args = p.parse_args(argv)
    api = Openagy()

    try:
        if args.cmd == "status":
            _print(api.status())
        elif args.cmd == "models":
            _print(api.list_models())
        elif args.cmd == "quota":
            _print(api.quota())
        elif args.cmd == "list":
            _print(api.list(args.limit))
        elif args.cmd == "ask":
            _print(api.ask(
                args.prompt, workspace=args.workspace, model=args.model,
                conversation_id=args.conversation, raw=args.raw,
                wait=not args.no_wait, inactivity_timeout=args.timeout,
            ))
        elif args.cmd == "read":
            _print(api.read(args.conversation_id, include_tools=args.tools))
        elif args.cmd == "wait":
            _print(api.wait(args.conversation_id, timeout_seconds=args.timeout))
        elif args.cmd == "answer":
            answer = {}
            if args.option:
                answer["selectedOptionIds"] = args.option
            if args.write_in:
                answer["writeInResponse"] = args.write_in
            if args.skip:
                answer["skipped"] = True
            answers = [answer] if answer else []
            _print(api.answer_question(args.conversation_id, answers=answers,
                                       cancelled=args.cancel,
                                       inactivity_timeout=args.timeout))
        elif args.cmd == "approve":
            _print(api.approve(args.conversation_id, allow=not args.deny,
                               scope=args.scope, inactivity_timeout=args.timeout))
        elif args.cmd == "undo":
            _print(api.undo(args.conversation_id, step_index=args.step,
                            keep_message=args.keep_message,
                            conversation_only=not args.with_files))
        elif args.cmd == "reask":
            _print(api.reask(args.conversation_id, args.prompt, raw=args.raw,
                             keep_message=args.keep_message,
                             inactivity_timeout=args.timeout))
        elif args.cmd == "delete":
            _print(api.delete(args.conversation_id))
    except (DiscoveryError, RpcError, KeyError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
