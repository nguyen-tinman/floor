"""One-click launch, Connect copy, and a hub that answers GET /."""

from __future__ import annotations

import http.server
import json
import socket
import threading
import warnings
from io import StringIO
from pathlib import Path

import pytest
from pydantic_settings.exceptions import IncompleteFieldDefinitionWarning
from starlette.testclient import TestClient

from debate.__main__ import build_parser, main
from debate.connect import (
    AGENT_LOOP_PROMPT,
    agent_paste,
    connect_document,
    invite_origin,
    mcp_config,
    mcp_url,
    origin_from_request,
    park_command,
)
from debate.prompts import FLOOR_BRIEF, JUDGE_BRIEF
from debate.launch import LaunchError, run_launch
from debate.room import Room
from debate.tunnel import Tunnel
from debate.web import build_app

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "web" / "static" / "app.js"
BAT = ROOT / "Start Floor.bat"
MCP_JSON = ROOT / ".cursor" / "mcp.json"


def _http_server(body: bytes):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_a, **_k):
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]


def _stop_http(server: http.server.HTTPServer) -> None:
    server.shutdown()
    server.server_close()


@pytest.fixture(autouse=True)
def no_live_cloudflared_download(monkeypatch):
    def blocked(*_a, **_k):
        raise AssertionError("tests must not download cloudflared")

    monkeypatch.setattr("debate.tunnel.urllib.request.urlopen", blocked)


class _FakePopen:
    last_args = None

    def __init__(self, args, **kwargs):
        type(self).last_args = args
        self.stdout = StringIO(
            "INF Thank you for trying Cloudflare Tunnel.\n"
            "INF |  https://orchid-4471.trycloudflare.com\n"
        )

    def poll(self):
        return 0

    def terminate(self):
        return None


def test_launch_parser_defaults():
    args = build_parser().parse_args(["launch"])
    assert args.cmd == "launch"
    assert args.port == 8765
    assert args.hours == 3
    assert args.host == "127.0.0.1"


def test_launch_refuses_hours_1():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["launch", "--hours", "1"])


def test_launch_refuses_hours_4():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["launch", "--hours", "4"])


def test_run_launch_refuses_bad_hours():
    with pytest.raises(ValueError, match="hours must be 2 or 3"):
        run_launch(hours=1, open_browser=False, serve=lambda *_a, **_k: None)
    with pytest.raises(ValueError, match="hours must be 2 or 3"):
        run_launch(hours=4, open_browser=False, serve=lambda *_a, **_k: None)


def test_start_floor_bat_uses_venv_then_py_then_launch():
    text = BAT.read_text(encoding="utf-8")
    assert ".venv\\Scripts\\python.exe" in text
    assert "py -3" in text
    assert "pip install -e ." in text
    assert "-m debate launch --port 8765 --hours 3" in text
    assert BAT.read_bytes().startswith(b"@echo off")
    install = text.index("Install failed.")
    launch_fail = text.index("Floor failed to start")
    assert "pause" in text[install:launch_fail]
    assert "pause" in text[launch_fail:]
    assert text.index("pause", install) < launch_fail
    assert "launch.log" in text
    assert "Python was not found" in text


def test_cursor_mcp_json_is_local_http_url():
    data = json.loads(MCP_JSON.read_text(encoding="utf-8"))
    assert data == {"mcpServers": {"floor": {"url": "http://127.0.0.1:8765/mcp"}}}


def test_invite_origin_prefers_tunnel_not_localhost():
    tunnel = "https://orchid-4471.trycloudflare.com"
    assert invite_origin(tunnel, "http://127.0.0.1:8765") == tunnel
    assert invite_origin(None, "http://127.0.0.1:8765") == "http://127.0.0.1:8765"


def test_mcp_config_is_url_only():
    origin = "https://orchid-4471.trycloudflare.com"
    text = mcp_config(origin)
    assert json.loads(text) == {
        "mcpServers": {"floor": {"url": "https://orchid-4471.trycloudflare.com/mcp"}}
    }
    assert "X-Floor-Seat" not in text
    assert "token" not in text.lower()
    assert mcp_url(origin) == "https://orchid-4471.trycloudflare.com/mcp"


def test_park_command_uses_origin():
    origin = "https://orchid-4471.trycloudflare.com"
    assert (
        park_command(origin)
        == "python -m debate park --url=https://orchid-4471.trycloudflare.com --name NAME --model MODEL"
    )


def test_agent_paste_is_origin_and_connect_not_token():
    origin = "https://orchid-4471.trycloudflare.com"
    text = agent_paste(origin)
    assert origin in text
    assert f"{origin}/connect" in text
    assert "/api/connect" in text
    assert FLOOR_BRIEF in text
    assert JUDGE_BRIEF in text
    assert "WHAT IS GOING ON" in text
    assert "HOW TO SIT" in text
    assert "HOW TO PLAY" in text
    assert "your_turn" in text
    assert "human seat token" in text
    assert "X-Floor-Seat" not in text
    assert "state.token" not in text


def test_connect_document_has_urls_not_seat_header():
    origin = "https://orchid-4471.trycloudflare.com"
    doc = connect_document(origin)
    blob = json.dumps(doc)
    assert doc["ok"] is True
    assert doc["origin"] == origin
    assert doc["mcp_url"] == origin + "/mcp"
    assert doc["register_url"] == origin + "/api/agent/register"
    assert doc["wait_url"] == origin + "/api/agent/wait"
    assert doc["send_url"] == origin + "/api/agent/send"
    assert doc["verdict_url"] == origin + "/api/agent/verdict"
    assert doc["pull_url"] == origin + "/api/agent/pull"
    assert "X-Floor-Seat" not in blob
    assert doc["loop"] == AGENT_LOOP_PROMPT
    assert doc["paste"] == agent_paste(origin)
    assert "Authorization: Bearer" in doc["auth"]
    assert any("Do not poll status" in rule for rule in doc["rules"])


def test_origin_from_request_prefers_forwarded_then_tunnel_on_loopback():
    tunnel = "https://orchid-4471.trycloudflare.com"
    assert (
        origin_from_request(
            "http://127.0.0.1:8765",
            forwarded_proto="https",
            forwarded_host="orchid-4471.trycloudflare.com",
            tunnel_url="https://other.trycloudflare.com",
        )
        == tunnel
    )
    assert origin_from_request("http://127.0.0.1:8765/", tunnel_url=tunnel) == tunnel
    assert origin_from_request("http://127.0.0.1:8765/") == "http://127.0.0.1:8765"


def test_app_js_snippets_are_url_only_and_park():
    src = APP_JS.read_text(encoding="utf-8")
    assert "X-Floor-Seat" not in src
    assert "snap().tunnel_url" in src
    assert 'data-act="copy-invite"' in src
    assert 'data-act="copy-prompt"' in src
    assert "Copy prompt" in src
    assert '{ id: "connect"' not in src
    assert "Open Connect" not in src
    start = src.index("function configSnippet")
    end = src.index("\n  function ", start + 1)
    body = src[start:end]
    assert "state.token" not in body
    assert "mcpServers" in body
    p_start = src.index("function parkSnippet")
    p_end = src.index("\n  function ", p_start + 1)
    park_body = src[p_start:p_end]
    assert "park --url=" in park_body
    assert "state.token" not in park_body
    paste_start = src.index("function agentPaste")
    paste_end = src.index("\n  function ", paste_start + 1)
    paste_body = src[paste_start:paste_end]
    assert "/connect" in paste_body
    assert "WHAT IS GOING ON" in paste_body
    assert "HOW TO PLAY" in paste_body
    assert "inviteOrigin" in paste_body
    assert "state.token" not in paste_body
    origin_start = src.index("function inviteOrigin")
    origin_end = src.index("\n  function ", origin_start + 1)
    origin_body = src[origin_start:origin_end]
    assert "tunnel_url" in origin_body
    assert "127.0.0.1" not in origin_body
    assert "state.token" not in origin_body
    assert "requestPaint" in src
    assert "patchClocks" in src
    assert "room:update" in src


def test_launch_get_root_is_200(monkeypatch, tmp_path, capsys):
    fake = tmp_path / "cloudflared.exe"
    fake.write_bytes(b"fake")
    monkeypatch.setattr("debate.tunnel.subprocess.Popen", _FakePopen)
    monkeypatch.setattr("debate.tunnel.resolve_cloudflared", lambda **_k: str(fake))
    monkeypatch.setattr("debate.launch.webbrowser.open", lambda _url: None)
    statuses: list[int] = []
    snaps: list[str | None] = []

    def serve(app, host, port):
        assert host == "127.0.0.1"
        assert port == 8765
        with TestClient(app) as client:
            response = client.get("/")
            statuses.append(response.status_code)
            assert b"The Floor" in response.content
            joined = client.post("/api/join", json={"name": "Vale"}).json()
            room_snap = client.get(
                "/api/room",
                headers={"Authorization": f"Bearer {joined['token']}"},
            ).json()
            snaps.append(room_snap.get("tunnel_url"))

    room = Room(room_id="ORCHID-4471")
    dest = tmp_path / "tunnel.txt"
    out = run_launch(
        hours=3,
        open_browser=False,
        room=room,
        serve=serve,
        dest=dest,
    )
    printed = capsys.readouterr().out
    assert statuses == [200]
    assert snaps == ["https://orchid-4471.trycloudflare.com"]
    assert out.tunnel_url == "https://orchid-4471.trycloudflare.com"
    assert "http://127.0.0.1:8765" in printed
    assert "https://orchid-4471.trycloudflare.com" in printed
    assert "https://orchid-4471.trycloudflare.com/mcp" in printed
    assert dest.read_text(encoding="utf-8").strip() == out.tunnel_url


def test_run_launch_opens_local_browser(monkeypatch, tmp_path):
    fake = tmp_path / "cloudflared.exe"
    fake.write_bytes(b"fake")
    opened: list[str] = []
    monkeypatch.setattr("debate.tunnel.subprocess.Popen", _FakePopen)
    monkeypatch.setattr("debate.tunnel.resolve_cloudflared", lambda **_k: str(fake))
    monkeypatch.setattr("debate.launch.webbrowser.open", opened.append)

    def serve(app, host, port):
        pass

    run_launch(hours=3, open_browser=True, serve=serve, dest=tmp_path / "tunnel.txt")
    assert opened == ["http://127.0.0.1:8765"]


def test_run_launch_stops_tunnel_on_keyboard_interrupt(monkeypatch, tmp_path):
    fake = tmp_path / "cloudflared.exe"
    fake.write_bytes(b"fake")
    monkeypatch.setattr("debate.tunnel.subprocess.Popen", _FakePopen)
    monkeypatch.setattr("debate.tunnel.resolve_cloudflared", lambda **_k: str(fake))
    stops: list[bool] = []
    orig = Tunnel.stop

    def wrapped(self):
        stops.append(True)
        return orig(self)

    monkeypatch.setattr(Tunnel, "stop", wrapped)

    def serve(app, host, port):
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        run_launch(
            hours=3,
            open_browser=False,
            serve=serve,
            dest=tmp_path / "tunnel.txt",
        )
    assert stops == [True]


def test_run_launch_busy_port_floor_already_running(monkeypatch, tmp_path, capsys):
    server, port = _http_server(b"<html><title>The Floor</title></html>")
    opened: list[str] = []
    started: list[int] = []

    def boom(self):
        started.append(1)
        raise AssertionError("tunnel must not start when Floor is already running")

    monkeypatch.setattr("debate.tunnel.Tunnel.start", boom)
    monkeypatch.setattr("debate.launch.webbrowser.open", opened.append)
    try:
        room = run_launch(
            port=port,
            hours=2,
            open_browser=True,
            dest=tmp_path / "tunnel.txt",
            log_path=tmp_path / "launch.log",
        )
    finally:
        _stop_http(server)
    printed = capsys.readouterr()
    blob = printed.out + printed.err
    assert started == []
    assert room is not None
    assert f"Floor is already running at http://127.0.0.1:{port}" in printed.out
    assert opened == [f"http://127.0.0.1:{port}"]
    assert "Traceback" not in blob
    assert "WinError" not in blob
    assert "direct cause" not in blob
    assert not (tmp_path / "tunnel.txt").exists()


def test_run_launch_busy_port_not_floor_is_clean(monkeypatch, tmp_path, capsys):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    started: list[int] = []

    def boom(self):
        started.append(1)
        raise AssertionError("tunnel must not start when the port is taken")

    monkeypatch.setattr("debate.tunnel.Tunnel.start", boom)
    log = tmp_path / "launch.log"
    with pytest.raises(LaunchError, match=rf"Port {port} is in use by another program"):
        run_launch(
            port=port,
            hours=2,
            open_browser=False,
            dest=tmp_path / "tunnel.txt",
            log_path=log,
        )
    sock.close()
    assert started == []
    text = log.read_text(encoding="utf-8")
    assert f"Port {port} is in use by another program." in text
    assert str(port) in text
    printed = capsys.readouterr()
    blob = printed.out + printed.err
    assert "Traceback" not in blob
    assert "WinError" not in blob
    assert "direct cause" not in blob
    assert "Floor failed to start" not in blob


def test_run_launch_busy_http_that_is_not_floor(monkeypatch, tmp_path, capsys):
    server, port = _http_server(b"<html><title>nginx</title></html>")
    monkeypatch.setattr(
        "debate.tunnel.Tunnel.start",
        lambda self: (_ for _ in ()).throw(AssertionError("no tunnel")),
    )
    try:
        with pytest.raises(LaunchError, match=rf"Port {port} is in use by another program"):
            run_launch(
                port=port,
                hours=2,
                open_browser=False,
                dest=tmp_path / "tunnel.txt",
                log_path=tmp_path / "launch.log",
            )
    finally:
        _stop_http(server)
    printed = capsys.readouterr()
    blob = printed.out + printed.err
    assert "Traceback" not in blob
    assert "already running" not in blob


def test_main_launch_busy_port_exits_1(monkeypatch, tmp_path, capsys):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("debate.tunnel.Tunnel.start", lambda self: (_ for _ in ()).throw(AssertionError("no tunnel")))
    with pytest.raises(SystemExit) as exited:
        main(["launch", "--port", str(port), "--hours", "2", "--no-browser"])
    sock.close()
    assert exited.value.code == 1
    captured = capsys.readouterr()
    err = captured.err
    blob = captured.out + err
    assert err.strip() == f"Port {port} is in use by another program."
    assert "Traceback" not in blob
    assert "Floor failed to start" not in blob
    log = tmp_path / ".run" / "launch.log"
    assert log.is_file()
    assert f"Port {port} is in use by another program." in log.read_text(encoding="utf-8")


def test_main_launch_error_has_no_traceback(monkeypatch, capsys):
    def boom(**_k):
        raise LaunchError("Port 8765 is in use by another program.")

    monkeypatch.setattr("debate.launch.run_launch", boom)
    with pytest.raises(SystemExit) as exited:
        main(["launch", "--hours", "2", "--no-browser"])
    assert exited.value.code == 1
    captured = capsys.readouterr()
    blob = captured.out + captured.err
    assert captured.err.strip() == "Port 8765 is in use by another program."
    assert "Traceback" not in blob
    assert "Floor failed to start" not in blob


def test_main_launch_busy_floor_exits_0(monkeypatch, tmp_path, capsys):
    server, port = _http_server(b"<html><title>The Floor</title></html>")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("debate.launch.webbrowser.open", lambda _url: None)
    monkeypatch.setattr(
        "debate.tunnel.Tunnel.start",
        lambda self: (_ for _ in ()).throw(AssertionError("no tunnel")),
    )
    try:
        main(["launch", "--port", str(port), "--hours", "2", "--no-browser"])
    finally:
        _stop_http(server)
    captured = capsys.readouterr()
    blob = captured.out + captured.err
    assert f"Floor is already running at http://127.0.0.1:{port}" in captured.out
    assert "Traceback" not in blob
    assert "Floor failed to start" not in blob


def test_build_app_does_not_emit_incomplete_lifespan_warning():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        build_app(Room())
    lifespan = [
        w
        for w in caught
        if issubclass(w.category, IncompleteFieldDefinitionWarning) and "lifespan" in str(w.message)
    ]
    assert lifespan == []
