"""HTTP agent client: register|wait|pull|send|status|verdict|park against a hub."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

AGENT_COMMANDS = ("register", "wait", "pull", "send", "status", "verdict", "park")
PARK_KINDS = frozenset({"your_turn", "judge", "ended"})

PATHS = {
    "register": "/api/agent/register",
    "wait": "/api/agent/wait",
    "pull": "/api/agent/pull",
    "send": "/api/agent/send",
    "status": "/api/agent/status",
    "verdict": "/api/agent/verdict",
}


def add_commands(sub: argparse._SubParsersAction) -> None:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--url", default=os.environ.get("FLOOR_URL", "http://127.0.0.1:8765"))
    common.add_argument("--token", default=os.environ.get("FLOOR_TOKEN"))
    common.add_argument("--name")
    common.add_argument("--model")
    common.add_argument("--text")
    common.add_argument("--timeout", type=float, default=30.0)
    sub.add_parser("register", parents=[common], help="Join the floor")
    sub.add_parser("wait", parents=[common], help="Block until a wake")
    sub.add_parser("pull", parents=[common], help="Snapshot history")
    sub.add_parser("send", parents=[common], help="Speak on your turn")
    sub.add_parser("status", parents=[common], help="Cheap room card")
    sub.add_parser("park", parents=[common], help="Block until your_turn, judge, or ended")
    verdict = sub.add_parser("verdict", parents=[common], help="Submit a bench verdict")
    verdict.add_argument("--winner")
    verdict.add_argument("--runner-up", dest="runner_up")
    verdict.add_argument("--honorable")
    verdict.add_argument("--reason")
    verdict.add_argument("--runner-reason", dest="runner_reason")
    verdict.add_argument("--honorable-reason", dest="honorable_reason")
    verdict.add_argument("--summary")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m debate")
    sub = parser.add_subparsers(dest="cmd", required=True)
    add_commands(sub)
    return parser


def _payload(args: argparse.Namespace) -> dict[str, Any]:
    if args.cmd == "register":
        return {"name": args.name or "", "model": args.model or ""}
    if args.cmd == "wait":
        return {"timeout_s": args.timeout}
    if args.cmd == "send":
        return {"text": args.text or ""}
    if args.cmd == "verdict":
        return {
            "winner": getattr(args, "winner", None) or "",
            "runner_up": getattr(args, "runner_up", None) or "",
            "honorable": getattr(args, "honorable", None) or "",
            "reason": getattr(args, "reason", None) or "",
            "runner_reason": getattr(args, "runner_reason", None) or "",
            "honorable_reason": getattr(args, "honorable_reason", None) or "",
            "summary": getattr(args, "summary", None) or "",
        }
    return {}


def _slug(name: str) -> str:
    raw = "".join(ch.lower() if ch.isalnum() else "-" for ch in name)
    while "--" in raw:
        raw = raw.replace("--", "-")
    return raw.strip("-") or "seat"


def seat_token_path(name: str) -> Path:
    return Path(".run") / "seats" / f"{_slug(name)}.token"


def _read_seat_token(name: str) -> str:
    path = seat_token_path(name)
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    return ""


def _write_seat_token(name: str, token: str) -> None:
    path = seat_token_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token, encoding="utf-8")


def _body(response: Any) -> dict[str, Any]:
    try:
        body = response.json()
    except Exception:
        return {"ok": False, "error": {"code": "http", "message": getattr(response, "text", "")}}
    return body if isinstance(body, dict) else {"ok": False, "error": {"code": "http", "message": str(body)}}


def _auth(token: str | None) -> dict[str, str]:
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


def park(args: argparse.Namespace, client) -> dict[str, Any]:
    token = (getattr(args, "token", None) or os.environ.get("FLOOR_TOKEN") or "").strip()
    name = (getattr(args, "name", None) or "").strip()
    model = (getattr(args, "model", None) or "").strip()
    if not token and name:
        token = _read_seat_token(name)
    if not token and name and model:
        registered = _body(client.post(PATHS["register"], json={"name": name, "model": model}, headers={}))
        if not registered.get("ok") or not registered.get("token"):
            if registered.get("ok") is False:
                return registered
            return {"ok": False, "error": {"code": "invalid", "message": "register failed"}}
        token = str(registered["token"])
        _write_seat_token(name, token)
    if not token:
        return {"ok": False, "error": {"code": "not_registered", "message": "register first"}}
    timeout = getattr(args, "timeout", None)
    if timeout is None:
        timeout = 30.0
    while True:
        wake = _body(
            client.post(PATHS["wait"], json={"timeout_s": timeout}, headers=_auth(token))
        )
        if not wake.get("ok"):
            return wake
        if wake.get("arrived") and wake.get("kind") in PARK_KINDS:
            return wake


def run(args: argparse.Namespace, client=None) -> dict[str, Any]:
    own = False
    if client is None:
        import httpx

        kwargs: dict[str, Any] = {"base_url": args.url}
        if args.cmd == "park":
            kwargs["timeout"] = (getattr(args, "timeout", None) or 30.0) + 15.0
        client = httpx.Client(**kwargs)
        own = True
    try:
        if args.cmd == "park":
            return park(args, client)
        headers = {}
        if args.token:
            headers["Authorization"] = f"Bearer {args.token}"
        response = client.post(PATHS[args.cmd], json=_payload(args), headers=headers)
        try:
            body = response.json()
        except Exception:
            return {"ok": False, "error": {"code": "http", "message": response.text}}
        return body if isinstance(body, dict) else {"ok": False, "error": {"code": "http", "message": str(body)}}
    finally:
        if own:
            client.close()


def _argv(argv: list[str]) -> list[str]:
    """Keep tokens that start with '-' from being eaten as flags."""
    out: list[str] = []
    i = 0
    while i < len(argv):
        item = argv[i]
        if item in {
            "--token",
            "--url",
            "--name",
            "--model",
            "--text",
            "--reason",
            "--runner-reason",
            "--honorable-reason",
            "--summary",
        } and i + 1 < len(argv):
            out.append(f"{item}={argv[i + 1]}")
            i += 2
            continue
        out.append(item)
        i += 1
    return out


def invoke(argv: list[str], *, client=None) -> dict[str, Any]:
    return run(build_parser().parse_args(_argv(argv)), client=client)


def main(argv: list[str] | None = None, *, client=None) -> int:
    result = invoke(list(sys.argv[1:] if argv is None else argv), client=client)
    print(json.dumps(result))
    return 0 if result.get("ok") else 1
