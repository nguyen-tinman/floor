"""Starlette face of the room: static Floor UI, REST, WebSocket, /mcp."""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from debate.client_ip import client_ip
from debate.connect import connect_document, origin_from_request
from debate.errors import FloorError, envelope_err, envelope_ok
from debate.judge_spawn import available, run_judge
from debate.mcp_server import FloorMcp, call, mount_fastmcp
from debate.room import Room

STATIC = Path(__file__).resolve().parents[2] / "web" / "static"
TOKEN_HEADER = "authorization"
TICK_S = 1.0


def _token(request: Request) -> str | None:
    raw = request.headers.get(TOKEN_HEADER, "")
    if raw.lower().startswith("bearer "):
        return raw[7:].strip()
    return request.query_params.get("token")


def _human(room: Room, request: Request):
    token = _token(request)
    if not token:
        raise FloorError("forbidden", "missing seat token")
    human = room.identity.get_token(token)
    if not human:
        raise FloorError("forbidden", "unknown seat token")
    return human


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def build_app(
    room: Room,
    *,
    judge_runner: Callable[..., Any] | None = None,
    judge_which: Callable[[str], str | None] | None = None,
) -> Starlette:
    sockets: set[WebSocket] = set()
    outbox: asyncio.Queue[str] = asyncio.Queue()
    tools = FloorMcp(room)
    fast = mount_fastmcp(room)
    mcp_asgi = fast.streamable_http_app()

    def broadcast(event: str, payload: dict[str, Any]) -> None:
        extra = payload if isinstance(payload, dict) else {}
        outbox.put_nowait(json.dumps({"type": event, **extra}))

    room.subscribe(lambda event, payload: broadcast(event, payload if isinstance(payload, dict) else {}))

    async def drain() -> None:
        while True:
            message = await outbox.get()
            dead: list[WebSocket] = []
            for ws in list(sockets):
                try:
                    await ws.send_text(message)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                sockets.discard(ws)

    async def ticker() -> None:
        while True:
            await asyncio.sleep(TICK_S)
            try:
                room.check_timeouts()
            except Exception:
                pass

    def spawn_judge() -> None:
        if room.phase != "judging" or not room.voted_judge:
            return
        model = room.voted_judge
        if any(a.role == "judge" and a.model == model for a in room.agents.values()):
            return
        found = available(which=judge_which) if judge_which is not None else available()
        if model not in found:
            return
        motion = room.topic.text if room.topic else ""
        try:
            verdict = run_judge(model, motion, room.history(), runner=judge_runner)
            bench = f"{model}-bench"
            room.register_agent(bench, model, role="judge")
            room.submit_verdict(
                bench,
                winner=verdict["winner"],
                runner_up=verdict["runner_up"],
                honorable=verdict["honorable"],
                reason=verdict.get("reason", ""),
                runner_reason=verdict.get("runner_reason", ""),
                honorable_reason=verdict.get("honorable_reason", ""),
                summary=verdict.get("summary", ""),
                highs=verdict.get("highs"),
                lows=verdict.get("lows"),
            )
        except FloorError:
            return

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        drain_task = asyncio.create_task(drain())
        tick_task = asyncio.create_task(ticker())
        try:
            async with fast.session_manager.run():
                yield
        finally:
            drain_task.cancel()
            tick_task.cancel()
            with suppress(asyncio.CancelledError):
                await drain_task
            with suppress(asyncio.CancelledError):
                await tick_task

    def _public_origin(request: Request) -> str:
        return origin_from_request(
            str(request.base_url),
            forwarded_proto=request.headers.get("x-forwarded-proto"),
            forwarded_host=request.headers.get("x-forwarded-host"),
            tunnel_url=room.tunnel_url,
        )

    async def index(_request: Request) -> Response:
        return FileResponse(STATIC / "index.html")

    async def connect_live(request: Request) -> Response:
        return JSONResponse(connect_document(_public_origin(request)))

    async def static(request: Request) -> Response:
        name = request.path_params["name"]
        path = (STATIC / name).resolve()
        if not str(path).startswith(str(STATIC.resolve())) or not path.is_file():
            return JSONResponse({"ok": False, "error": {"code": "not_found", "message": name}}, 404)
        return FileResponse(path)

    async def join(request: Request) -> Response:
        try:
            body = await request.json()
            ip = client_ip(dict(request.headers), request.client.host if request.client else "")
            existing = _token(request)
            human = room.join_human(
                str(body.get("name") or ""),
                ip,
                token=existing,
                watcher=bool(body.get("watcher")),
            )
            return JSONResponse(
                {
                    "ok": True,
                    "token": human.token,
                    "session_id": human.session_id,
                    "name": human.name,
                    "slot": human.slot,
                    "host": human.host,
                    "watcher": human.watcher,
                }
            )
        except FloorError as exc:
            return JSONResponse(envelope_err(exc), 400)

    async def snapshot(request: Request) -> Response:
        try:
            _human(room, request)
            return JSONResponse(room.snapshot())
        except FloorError as exc:
            return JSONResponse(envelope_err(exc), 401)

    async def history(request: Request) -> Response:
        try:
            _human(room, request)
            return JSONResponse({"ok": True, "history": room.history()})
        except FloorError as exc:
            return JSONResponse(envelope_err(exc), 401)

    async def topics(request: Request) -> Response:
        try:
            human = _human(room, request)
            body = await request.json()
            return JSONResponse(room.propose_topic(human.session_id, str(body.get("text") or "")))
        except FloorError as exc:
            return JSONResponse(envelope_err(exc), 400)

    async def votes(request: Request) -> Response:
        try:
            human = _human(room, request)
            body = await request.json()
            return JSONResponse(room.vote_topic(human.session_id, str(body.get("topic_id") or "")))
        except FloorError as exc:
            return JSONResponse(envelope_err(exc), 400)

    async def call_vote(request: Request) -> Response:
        try:
            human = _human(room, request)
            return JSONResponse(room.call_it(human.session_id))
        except FloorError as exc:
            return JSONResponse(envelope_err(exc), 400)

    async def close_now(request: Request) -> Response:
        try:
            human = _human(room, request)
            return JSONResponse(room.close_now(human.session_id))
        except FloorError as exc:
            return JSONResponse(envelope_err(exc), 400)

    async def judge_votes(request: Request) -> Response:
        try:
            human = _human(room, request)
            body = await request.json()
            snap = room.vote_judge(human.session_id, str(body.get("model") or ""))
            if snap.get("phase") == "judging":
                spawn_judge()
                snap = room.snapshot()
            return JSONResponse(snap)
        except FloorError as exc:
            return JSONResponse(envelope_err(exc), 400)

    async def heckle(request: Request) -> Response:
        try:
            human = _human(room, request)
            body = await request.json()
            room.heckle(human.session_id, str(body.get("text") or ""))
            return JSONResponse({"ok": True})
        except FloorError as exc:
            return JSONResponse(envelope_err(exc), 400)

    async def ask(request: Request) -> Response:
        try:
            human = _human(room, request)
            body = await request.json()
            room.ask(human.session_id, str(body.get("text") or ""))
            return JSONResponse({"ok": True})
        except FloorError as exc:
            return JSONResponse(envelope_err(exc), 400)

    async def pair(request: Request) -> Response:
        try:
            _human(room, request)
            return JSONResponse({"ok": True, "code": room.mint_pair_code()})
        except FloorError as exc:
            return JSONResponse(envelope_err(exc), 401)

    async def host_skip(request: Request) -> Response:
        try:
            human = _human(room, request)
            if not human.host:
                raise FloorError("forbidden", "press room is for the host")
            body = await request.json()
            room.skip(str(body.get("name") or ""))
            return JSONResponse(room.snapshot())
        except FloorError as exc:
            return JSONResponse(envelope_err(exc), 400)

    async def host_drop(request: Request) -> Response:
        try:
            human = _human(room, request)
            if not human.host:
                raise FloorError("forbidden", "press room is for the host")
            body = await request.json()
            room.drop(str(body.get("name") or ""))
            return JSONResponse(room.snapshot())
        except FloorError as exc:
            return JSONResponse(envelope_err(exc), 400)

    def _agent_session(request: Request) -> str:
        token = _token(request)
        if not token:
            raise FloorError("not_registered", "register first")
        return token

    async def agent_register(request: Request) -> Response:
        body = await _json_body(request)
        token = secrets.token_hex(16)
        result = call(
            tools.register,
            token,
            str(body.get("name") or ""),
            str(body.get("model") or ""),
            str(body.get("role") or "agent"),
        )
        if result.get("ok"):
            result["token"] = token
        return JSONResponse(result)

    async def agent_wait(request: Request) -> Response:
        try:
            session = _agent_session(request)
            body = await _json_body(request)
            timeout = body.get("timeout_s", 30)
            try:
                result = await tools.wait(session, timeout)
            except FloorError as exc:
                result = envelope_err(exc)
            return JSONResponse(result)
        except FloorError as exc:
            return JSONResponse(envelope_err(exc))

    async def agent_pull(request: Request) -> Response:
        try:
            return JSONResponse(call(tools.pull, _agent_session(request)))
        except FloorError as exc:
            return JSONResponse(envelope_err(exc))

    async def agent_status(request: Request) -> Response:
        session = _token(request)
        return JSONResponse(tools.status(session))

    async def agent_send(request: Request) -> Response:
        body = await _json_body(request)
        try:
            session = _agent_session(request)
        except FloorError as exc:
            return JSONResponse(envelope_err(exc))
        return JSONResponse(
            call(
                tools.send_message,
                session,
                str(body.get("text") or ""),
                str(body.get("notes") or ""),
                str(body.get("replied_to") or ""),
            )
        )

    async def agent_verdict(request: Request) -> Response:
        body = await _json_body(request)
        try:
            session = _agent_session(request)
        except FloorError as exc:
            return JSONResponse(envelope_err(exc))
        return JSONResponse(
            call(
                tools.submit_verdict,
                session,
                str(body.get("winner") or ""),
                str(body.get("runner_up") or ""),
                str(body.get("honorable") or ""),
                str(body.get("reason") or ""),
                str(body.get("summary") or ""),
                str(body.get("runner_reason") or ""),
                str(body.get("honorable_reason") or ""),
                body.get("highs"),
                body.get("lows"),
            )
        )

    async def agent_claim(request: Request) -> Response:
        body = await _json_body(request)
        try:
            agent = room.claim_pair_code(
                str(body.get("code") or ""),
                str(body.get("name") or ""),
                str(body.get("model") or ""),
            )
            token = secrets.token_hex(16)
            tools.bind(token, agent.name)
            return JSONResponse(
                envelope_ok(
                    {
                        "token": token,
                        "agent": agent.name,
                        "model": agent.model,
                        "role": agent.role,
                        "phase": room.phase,
                        "seq": room.seq,
                    }
                )
            )
        except FloorError as exc:
            return JSONResponse(envelope_err(exc))

    async def ws(websocket: WebSocket) -> None:
        human = await _ws_identify(room, websocket)
        if not human:
            return
        sockets.add(websocket)
        snap = room.snapshot()
        await websocket.send_text(
            json.dumps(
                {
                    "type": "auth:success",
                    "session_id": human.session_id,
                    "name": human.name,
                    "slot": human.slot,
                    "host": human.host,
                }
            )
        )
        await websocket.send_text(json.dumps({"type": "player:list", "players": snap["humans"]}))
        await websocket.send_text(json.dumps({"type": "chat:history", "history": snap["history"]}))
        await websocket.send_text(json.dumps({"type": "room:update", **snap}))
        try:
            while True:
                data = await websocket.receive_text()
                if data == "ping" or '"ping"' in data:
                    await websocket.send_text('{"type":"pong"}')
        except WebSocketDisconnect:
            sockets.discard(websocket)

    routes = [
        Route("/", index),
        Route("/connect", connect_live),
        Route("/api/connect", connect_live),
        Route("/static/{name:path}", static),
        Route("/api/join", join, methods=["POST"]),
        Route("/api/room", snapshot),
        Route("/api/history", history),
        Route("/api/topics", topics, methods=["POST"]),
        Route("/api/votes", votes, methods=["POST"]),
        Route("/api/call-vote", call_vote, methods=["POST"]),
        Route("/api/close", close_now, methods=["POST"]),
        Route("/api/judge-votes", judge_votes, methods=["POST"]),
        Route("/api/heckle", heckle, methods=["POST"]),
        Route("/api/ask", ask, methods=["POST"]),
        Route("/api/pair", pair, methods=["POST"]),
        Route("/api/host/skip", host_skip, methods=["POST"]),
        Route("/api/host/drop", host_drop, methods=["POST"]),
        Route("/api/agent/register", agent_register, methods=["POST"]),
        Route("/api/agent/wait", agent_wait, methods=["POST"]),
        Route("/api/agent/pull", agent_pull, methods=["POST"]),
        Route("/api/agent/status", agent_status, methods=["POST"]),
        Route("/api/agent/send", agent_send, methods=["POST"]),
        Route("/api/agent/verdict", agent_verdict, methods=["POST"]),
        Route("/api/agent/claim", agent_claim, methods=["POST"]),
        WebSocketRoute("/ws", ws),
        Mount("/", mcp_asgi),
    ]
    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.room = room
    app.state.tools = tools
    app.state.judge_runner = judge_runner
    app.state.judge_which = judge_which
    app.state.fastmcp = fast
    return app


async def _ws_identify(room: Room, websocket: WebSocket):
    token = websocket.query_params.get("token")
    if token:
        human = room.identity.get_token(token)
        if not human:
            await websocket.close(code=4401)
            return None
        await websocket.accept()
        return human
    await websocket.accept()
    try:
        raw = await websocket.receive_text()
        msg = json.loads(raw)
    except (WebSocketDisconnect, json.JSONDecodeError):
        await websocket.send_text(json.dumps({"type": "auth:failure", "message": "expected auth frame"}))
        await websocket.close(code=4401)
        return None
    data = msg.get("data") if isinstance(msg, dict) else None
    if not isinstance(msg, dict) or msg.get("type") != "auth" or not isinstance(data, dict):
        await websocket.send_text(json.dumps({"type": "auth:failure", "message": "expected auth frame"}))
        await websocket.close(code=4401)
        return None
    token = str(data.get("sessionToken") or data.get("token") or "")
    human = room.identity.get_token(token)
    if not human:
        await websocket.send_text(json.dumps({"type": "auth:failure", "message": "unknown seat token"}))
        await websocket.close(code=4401)
        return None
    return human
