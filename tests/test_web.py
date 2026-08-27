"""Starlette hub: seats, WS replay, /mcp mount, injected judge spawn."""

import json

import httpx
import pytest
from starlette.testclient import TestClient

from debate.errors import FloorError
from debate.room import Room
from debate.web import build_app


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _room() -> Room:
    return Room(room_id="ORCHID-4471", rng=lambda n: 0)


def _app(room: Room | None = None, **kwargs):
    kwargs.setdefault("judge_which", lambda _name: None)
    return build_app(room or _room(), **kwargs)


class Completed:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _to_judge_vote(room: Room, *, bench: str = "claude"):
    vale = room.join_human("Vale", "1.1.1.1")
    room.register_agent("Grok", "grok")
    room.configure_judge(bench, bench.title())
    motion = room.propose_topic(vale.session_id, "Resolved: testers first.")
    room.vote_topic(vale.session_id, motion["id"])
    room.close_now(vale.session_id)
    assert room.phase == "judge_vote"
    return vale


@pytest.mark.asyncio
async def test_join_mints_token_httpx():
    app = _app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/join", json={"name": "Vale"})
    body = response.json()
    assert response.status_code == 200
    assert body["ok"] is True
    assert body["name"] == "Vale"
    assert body["token"]
    assert body["session_id"]
    assert body["slot"] == 1


def test_bearer_snapshot_and_unknown_token_401():
    room = _room()
    app = _app(room)
    with TestClient(app) as client:
        joined = client.post("/api/join", json={"name": "Vale"}).json()
        token = joined["token"]
        ok = client.get("/api/room", headers=_auth(token))
        assert ok.status_code == 200
        assert ok.json()["ok"] is True
        assert ok.json()["phase"] == "lobby"
        denied = client.get("/api/room", headers=_auth("not-a-seat"))
        assert denied.status_code == 401
        assert denied.json()["ok"] is False
        assert denied.json()["error"]["code"] == "forbidden"


def test_ws_handshake_replays_required_types():
    room = _room()
    app = _app(room)
    with TestClient(app) as client:
        token = client.post("/api/join", json={"name": "Vale"}).json()["token"]
        with client.websocket_connect(f"/ws?token={token}") as ws:
            types = [json.loads(ws.receive_text())["type"] for _ in range(4)]
    assert types == ["auth:success", "player:list", "chat:history", "room:update"]


def test_ws_accepts_first_frame_auth_when_query_token_missing():
    room = _room()
    app = _app(room)
    with TestClient(app) as client:
        token = client.post("/api/join", json={"name": "Vale"}).json()["token"]
        with client.websocket_connect("/ws") as ws:
            ws.send_text(json.dumps({"type": "auth", "data": {"sessionToken": token}}))
            types = [json.loads(ws.receive_text())["type"] for _ in range(4)]
    assert types[0] == "auth:success"
    assert types == ["auth:success", "player:list", "chat:history", "room:update"]


def test_ws_queue_broadcasts_room_update():
    room = _room()
    app = _app(room)
    with TestClient(app) as client:
        token = client.post("/api/join", json={"name": "Vale"}).json()["token"]
        with client.websocket_connect(f"/ws?token={token}") as ws:
            for _ in range(4):
                ws.receive_text()
            proposed = client.post(
                "/api/topics",
                json={"text": "Resolved: queues beat create_task."},
                headers=_auth(token),
            )
            assert proposed.status_code == 200
            event = json.loads(ws.receive_text())
    assert event["type"] == "room:update"
    assert event["phase"] == "lobby"


def test_ws_agent_register_pushes_room_update():
    room = _room()
    app = _app(room)
    with TestClient(app) as client:
        token = client.post("/api/join", json={"name": "Vale"}).json()["token"]
        with client.websocket_connect(f"/ws?token={token}") as ws:
            for _ in range(4):
                ws.receive_text()
            seated = client.post("/api/agent/register", json={"name": "Grok", "model": "grok"})
            assert seated.status_code == 200
            found = None
            for _ in range(8):
                event = json.loads(ws.receive_text())
                if event.get("type") == "room:update":
                    found = event
                    break
    assert found is not None
    assert any(a.get("name") == "Grok" for a in found.get("agents") or [])


@pytest.mark.asyncio
async def test_mcp_is_not_404():
    app = _app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/mcp")
            if response.status_code == 404:
                response = await client.post(
                    "/mcp",
                    content=b"{}",
                    headers={"content-type": "application/json"},
                )
    assert response.status_code != 404
    assert 200 <= response.status_code < 500


def test_judge_vote_appoints_seated_agent():
    room = _room()
    vale = _to_judge_vote(room)
    app = build_app(room, judge_which=lambda _name: None)
    with TestClient(app) as client:
        response = client.post(
            "/api/judge-votes",
            json={"model": "grok"},
            headers=_auth(vale.token),
        )
    snap = response.json()
    assert response.status_code == 200
    assert snap["phase"] == "judging"
    assert room.agents["Grok"].role == "judge"


def test_verdict_http_keeps_a_reason_for_each_plate():
    room = _room()
    vale = room.join_human("Vale", "1.1.1.1")
    app = _app(room)
    with TestClient(app) as client:
        grok = client.post("/api/agent/register", json={"name": "Grok", "model": "grok"}).json()
        claude = client.post("/api/agent/register", json={"name": "Claude", "model": "claude"}).json()
        assert grok["ok"] and claude["ok"]
        topic = client.post(
            "/api/topics",
            json={"text": "Resolved: testers first."},
            headers=_auth(vale.token),
        ).json()
        client.post("/api/votes", json={"topic_id": topic["id"]}, headers=_auth(vale.token))
        sent = client.post(
            "/api/agent/send",
            json={"text": "Opening."},
            headers=_auth(grok["token"]),
        ).json()
        assert sent["ok"] is True
        client.post("/api/close", headers=_auth(vale.token))
        judged = client.post(
            "/api/judge-votes",
            json={"model": "grok"},
            headers=_auth(vale.token),
        ).json()
        assert judged["phase"] == "judging"
        out = client.post(
            "/api/agent/verdict",
            json={
                "winner": "Claude",
                "runner_up": "Claude",
                "honorable": "Claude",
                "reason": "winner why",
                "runner_reason": "runner why",
                "honorable_reason": "honor why",
                "summary": "bench note",
            },
            headers=_auth(grok["token"]),
        ).json()
    assert out["ok"] is True
    assert out["phase"] == "verdict"
    assert out["verdict"]["reason"] == "winner why"
    assert out["verdict"]["runner_reason"] == "runner why"
    assert out["verdict"]["honorable_reason"] == "honor why"
    assert out["verdict"]["summary"] == "bench note"


def test_never_seated_judge_vote_is_rejected():
    room = _room()
    vale = _to_judge_vote(room, bench="claude")
    app = build_app(room, judge_which=lambda _name: None)
    with TestClient(app) as client:
        response = client.post(
            "/api/judge-votes",
            json={"model": "claude"},
            headers=_auth(vale.token),
        )
    assert response.status_code == 400
    assert response.json()["ok"] is False
    assert room.phase == "judge_vote"


def test_connect_document_is_public_and_has_agent_urls():
    room = _room()
    app = _app(room)
    with TestClient(app) as client:
        joined = client.post("/api/join", json={"name": "Vale"}).json()
        response = client.get("/connect")
        alt = client.get("/api/connect")
    assert response.status_code == 200
    assert alt.status_code == 200
    body = response.json()
    assert body == alt.json()
    assert body["ok"] is True
    assert "/mcp" in body["mcp_url"]
    assert "/api/agent/register" in body["register_url"]
    blob = json.dumps(body)
    assert "X-Floor-Seat" not in blob
    assert joined["token"] not in blob
    assert "/connect" in body["paste"]
    assert "WHAT IS GOING ON" in body["paste"]
    assert "HOW TO PLAY" in body["paste"]
    assert "on the floor of a debate" in body["brief"]


def test_host_rename_http_rebinding_keeps_wait_token():
    room = _room()
    app = _app(room)
    with TestClient(app) as client:
        vale = client.post("/api/join", json={"name": "Vale"}).json()
        guest = client.post("/api/join", json={"name": "Bram"}).json()
        seated = client.post("/api/agent/register", json={"name": "claude", "model": "claude"}).json()
        assert seated["ok"] is True
        denied = client.post(
            "/api/host/rename",
            json={"name": "claude", "to": "sonnet"},
            headers=_auth(guest["token"]),
        )
        assert denied.status_code == 400
        assert denied.json()["error"]["code"] == "forbidden"
        renamed = client.post(
            "/api/host/rename",
            json={"name": "claude", "to": "sonnet"},
            headers=_auth(vale["token"]),
        ).json()
        assert renamed["ok"] is True
        assert any(a["name"] == "sonnet" for a in renamed["agents"])
        pulled = client.post("/api/agent/pull", headers=_auth(seated["token"])).json()
        assert pulled["ok"] is True
        assert pulled["you"] == "sonnet"


def test_floor_votes_http_round_verbosity_and_kick():
    room = _room()
    app = _app(room)
    with TestClient(app) as client:
        vale = client.post("/api/join", json={"name": "Vale"}).json()
        bram = client.post("/api/join", json={"name": "Bram"}).json()
        client.post("/api/agent/register", json={"name": "sol", "model": "sol"})
        client.post("/api/agent/register", json={"name": "luna", "model": "luna"})
        motion = client.post(
            "/api/topics",
            json={"text": "The motion is capacity."},
            headers=_auth(vale["token"]),
        ).json()
        client.post("/api/votes", json={"topic_id": motion["id"]}, headers=_auth(bram["token"]))
        too_soon = client.post(
            "/api/round-vote",
            json={"choice": "advance"},
            headers=_auth(vale["token"]),
        )
        assert too_soon.status_code == 400
        verbose = client.post(
            "/api/verbosity",
            json={"choice": "less"},
            headers=_auth(vale["token"]),
        ).json()
        assert verbose["ok"] is True
        assert verbose["verbosity_vote"]["counts"]["less"] == 1
        kick = client.post(
            "/api/kick",
            json={"name": "luna"},
            headers=_auth(vale["token"]),
        ).json()
        assert kick["ok"] is True
        assert any(row["name"] == "luna" and row["votes"] == 1 for row in kick["kick_votes"])


def test_connect_document_uses_tunnel_on_localhost():
    room = _room()
    room.tunnel_url = "https://orchid-4471.trycloudflare.com"
    app = _app(room)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        body = client.get("/connect").json()
    assert body["origin"] == "https://orchid-4471.trycloudflare.com"
    assert body["mcp_url"] == "https://orchid-4471.trycloudflare.com/mcp"
    assert "X-Floor-Seat" not in json.dumps(body)
