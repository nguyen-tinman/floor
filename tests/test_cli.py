"""CLI talks HTTP to the hub: register, wait timeout, send on a pushed turn."""

from starlette.testclient import TestClient

from debate import cli
from debate.__main__ import build_parser
from debate.room import Room
from debate.web import build_app


def _hub():
    room = Room(room_id="ORCHID-4471", rng=lambda n: 0)
    app = build_app(room, judge_which=lambda _name: None)
    return room, app


def test_parser_keeps_serve_mcp_and_agent_commands():
    parser = build_parser()
    assert parser.parse_args(["serve", "--port", "8765"]).cmd == "serve"
    launch = parser.parse_args(["launch", "--port", "8765", "--hours", "3"])
    assert launch.cmd == "launch"
    assert launch.port == 8765
    assert launch.hours == 3
    assert parser.parse_args(["mcp", "--stdio"]).cmd == "mcp"
    reg = parser.parse_args(["register", "--name", "Grok", "--model", "grok"])
    assert reg.cmd == "register"
    assert parser.parse_args(["wait", "--timeout", "0"]).cmd == "wait"
    assert parser.parse_args(["pull"]).cmd == "pull"
    assert parser.parse_args(["send", "--text", "Opening."]).cmd == "send"
    assert parser.parse_args(["status"]).cmd == "status"
    assert parser.parse_args(["verdict", "--winner", "A", "--runner-up", "B", "--honorable", "C"]).cmd == "verdict"
    assert parser.parse_args(["park", "--name", "Grok", "--model", "grok"]).cmd == "park"


def test_verdict_payload_includes_a_reason_for_each_plate():
    args = cli.build_parser().parse_args(
        [
            "verdict",
            "--winner",
            "A",
            "--runner-up",
            "B",
            "--honorable",
            "C",
            "--reason",
            "winner why",
            "--runner-reason",
            "runner why",
            "--honorable-reason",
            "honor why",
            "--summary",
            "bench note",
        ]
    )
    payload = cli._payload(args)
    assert payload["winner"] == "A"
    assert payload["runner_up"] == "B"
    assert payload["honorable"] == "C"
    assert payload["reason"] == "winner why"
    assert payload["runner_reason"] == "runner why"
    assert payload["honorable_reason"] == "honor why"
    assert payload["summary"] == "bench note"


def test_token_starting_with_dash_is_not_a_flag():
    parsed = cli.build_parser().parse_args(cli._argv(["wait", "--token", "-V-looks-like-a-flag", "--timeout", "0"]))
    assert parsed.token == "-V-looks-like-a-flag"
    assert parsed.timeout == 0.0


def test_register_then_wait_timeout_arrived_false():
    _room, app = _hub()
    with TestClient(app) as client:
        registered = cli.invoke(["register", "--name", "Grok", "--model", "grok"], client=client)
        assert registered["ok"] is True
        assert registered["agent"] == "Grok"
        assert registered["token"]
        wake = cli.invoke(
            ["wait", "--timeout", "0", "--token", registered["token"]],
            client=client,
        )
    assert wake["ok"] is True
    assert wake["arrived"] is False


def test_send_after_a_turn_is_pushed():
    room, app = _hub()
    with TestClient(app) as client:
        registered = cli.invoke(["register", "--name", "Grok", "--model", "grok"], client=client)
        token = registered["token"]
        vale = room.join_human("Vale", "1.1.1.1")
        motion = room.propose_topic(vale.session_id, "Resolved: a")
        room.vote_topic(vale.session_id, motion["id"])
        assert room.speaker == "Grok"
        sent = cli.invoke(
            ["send", "--text", "The wrapper is a costume.", "--token", token],
            client=client,
        )
        status = cli.invoke(["status", "--token", token], client=client)
    assert sent["ok"] is True
    assert sent["text"] == "The wrapper is a costume."
    assert status["ok"] is True
    assert any(line["text"] == "The wrapper is a costume." for line in room.history())


class _SeqClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.paths = []

    def post(self, path, json=None, headers=None):
        self.paths.append(path)
        if not self.replies:
            raise AssertionError("park looped past prepared replies")
        body = self.replies.pop(0)

        class _Resp:
            text = ""

            def json(self_inner):
                return body

        return _Resp()

    def close(self):
        pass


def test_park_stays_quiet_until_your_turn(capsys):
    client = _SeqClient(
        [
            {"ok": True, "arrived": False, "kind": "info"},
            {"ok": True, "arrived": False, "kind": "info"},
            {"ok": True, "arrived": True, "kind": "your_turn", "seq": 2},
        ]
    )
    out = cli.invoke(["park", "--token", "seat-token"], client=client)
    assert capsys.readouterr().out == ""
    assert out["ok"] is True
    assert out["kind"] == "your_turn"
    assert out["seq"] == 2
    assert client.paths == ["/api/agent/wait", "/api/agent/wait", "/api/agent/wait"]


def test_park_ended_exits_ok():
    client = _SeqClient([{"ok": True, "arrived": True, "kind": "ended", "phase": "verdict"}])
    out = cli.invoke(["park", "--token=seat-token"], client=client)
    assert out["ok"] is True
    assert out["kind"] == "ended"
    assert cli.main(["park", "--token=seat-token"], client=_SeqClient(
        [{"ok": True, "arrived": True, "kind": "ended"}]
    )) == 0


def test_park_missing_token_does_not_register():
    room, app = _hub()
    with TestClient(app) as client:
        n = len(room.agents)
        out = cli.invoke(["park"], client=client)
    assert out["ok"] is False
    assert out["error"]["code"] == "not_registered"
    assert len(room.agents) == n


def test_park_saves_and_reclaims_seat_token(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    first = _SeqClient(
        [
            {"ok": True, "token": "abc123token", "agent": "Grok"},
            {"ok": True, "arrived": True, "kind": "your_turn"},
        ]
    )
    out = cli.invoke(["park", "--name", "Grok", "--model", "grok"], client=first)
    assert out["kind"] == "your_turn"
    token_path = tmp_path / ".run" / "seats" / "grok.token"
    assert token_path.read_text(encoding="utf-8") == "abc123token"
    assert first.paths == ["/api/agent/register", "/api/agent/wait"]
    second = _SeqClient([{"ok": True, "arrived": True, "kind": "ended"}])
    out2 = cli.invoke(["park", "--name", "Grok"], client=second)
    assert out2["kind"] == "ended"
    assert second.paths == ["/api/agent/wait"]
