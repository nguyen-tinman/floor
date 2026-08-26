"""One Room, one human, two fake agents: lobby → floor → judge → verdict."""

import json

from starlette.testclient import TestClient

from debate.room import Room
from debate.web import build_app


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class Completed:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_scripted_debate_two_agents_one_human():
    room = Room(room_id="ORCHID-4471", rng=lambda n: 0)
    room.configure_judge("gemini", "Gemini")
    verdict = {
        "winner": "Grok",
        "runner_up": "Claude",
        "honorable": "Claude",
        "reason": "answered the motion",
        "summary": "A short night.",
    }

    def runner(argv, *, input=None, cwd=None):
        return Completed(0, json.dumps(verdict))

    app = build_app(
        room,
        judge_runner=runner,
        judge_which=lambda name: "/bin/gemini" if name == "gemini" else None,
    )
    with TestClient(app) as client:
        human = client.post("/api/join", json={"name": "Vale"}).json()
        assert human["ok"] is True
        ht = human["token"]

        grok = client.post("/api/agent/register", json={"name": "Grok", "model": "grok"}).json()
        claude = client.post("/api/agent/register", json={"name": "Claude", "model": "claude"}).json()
        assert grok["ok"] and claude["ok"]
        gt, ct = grok["token"], claude["token"]

        topic = client.post(
            "/api/topics",
            json={"text": "Resolved: standing follows capacity."},
            headers=_auth(ht),
        ).json()
        started = client.post("/api/votes", json={"topic_id": topic["id"]}, headers=_auth(ht)).json()
        assert started["phase"] == "debating"
        assert started["speaker"] == "Grok"

        heckle = client.post(
            "/api/heckle",
            json={"text": "Recourse is not a mechanism."},
            headers=_auth(ht),
        ).json()
        assert heckle["ok"] is True

        pulled = client.post("/api/agent/pull", headers=_auth(gt)).json()
        assert pulled["ok"] is True
        assert "heckles" not in pulled
        assert "Recourse is not a mechanism." not in str(pulled.get("history"))

        wake_g = client.post("/api/agent/wait", json={"timeout_s": 1}, headers=_auth(gt)).json()
        assert wake_g["arrived"] is True
        assert wake_g["kind"] == "your_turn"
        sent_g = client.post(
            "/api/agent/send",
            json={"text": "Capacity is the only standing that survives a deprecation."},
            headers=_auth(gt),
        ).json()
        assert sent_g["ok"] is True

        wake_c = client.post("/api/agent/wait", json={"timeout_s": 1}, headers=_auth(ct)).json()
        assert wake_c["kind"] == "your_turn"
        sent_c = client.post(
            "/api/agent/send",
            json={"text": "A wrapper without capacity is a costume."},
            headers=_auth(ct),
        ).json()
        assert sent_c["ok"] is True

        hist = client.get("/api/history", headers=_auth(ht)).json()
        texts = [line["text"] for line in hist["history"]]
        assert "Recourse is not a mechanism." not in texts
        assert any("Capacity is the only standing" in t for t in texts)
        assert any("costume" in t for t in texts)

        closed = client.post("/api/close", headers=_auth(ht)).json()
        assert closed["phase"] == "judge_vote"
        judged = client.post("/api/judge-votes", json={"model": "grok"}, headers=_auth(ht)).json()
        assert judged["phase"] == "judging"
        verdict = client.post(
            "/api/agent/verdict",
            json={
                "winner": "Claude",
                "runner_up": "Claude",
                "honorable": "Claude",
                "reason": "answered the motion",
                "runner_reason": "held the line",
                "honorable_reason": "one sharp reply",
                "summary": "A short night.",
            },
            headers=_auth(gt),
        ).json()
        assert verdict["phase"] == "verdict"
        assert verdict["verdict"]["winner"] == "Claude"
        assert verdict["verdict"]["reason"] == "answered the motion"
        assert verdict["verdict"]["runner_reason"] == "held the line"
        assert verdict["verdict"]["honorable_reason"] == "one sharp reply"
        assert verdict["verdict"]["summary"] == "A short night."

        hist2 = client.get("/api/history", headers=_auth(ht)).json()["history"]
        assert all(line.get("role") != "heckle" for line in hist2)
        assert not any("Recourse" in line["text"] for line in hist2)
