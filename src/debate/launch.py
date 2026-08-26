"""One-click host: serve + tunnel + open the local browser."""

from __future__ import annotations

import http.client
import socket
import sys
import threading
import time
import traceback
import webbrowser
from contextlib import contextmanager
from pathlib import Path

from debate.connect import mcp_url
from debate.room import Room
from debate.tunnel import Tunnel
from debate.web import build_app

LAUNCH_LOG = Path(".run") / "launch.log"
SERVE_LOG = Path(".run") / "serve.log"
_ADDRINUSE = {98, 48, 10048}


class LaunchError(RuntimeError):
    """Startup failed; the message is safe to print to a human."""


class _Tee:
    def __init__(self, *streams) -> None:
        self._streams = streams

    def write(self, data) -> int:
        text = data.decode("utf-8", errors="replace") if isinstance(data, (bytes, bytearray)) else str(data)
        if _looks_secret(text):
            text = "[redacted]\n"
        for stream in self._streams:
            try:
                stream.write(text)
                stream.flush()
            except Exception:
                pass
        return len(text)

    def flush(self) -> None:
        for stream in self._streams:
            try:
                stream.flush()
            except Exception:
                pass

    @property
    def encoding(self) -> str:
        return "utf-8"

    def isatty(self) -> bool:
        return False


def _looks_secret(text: str) -> bool:
    lower = text.lower()
    return "authorization" in lower or "bearer " in lower or "token=" in lower


@contextmanager
def startup_log(path: Path):
    """Tee stdout/stderr into path so a vanishing window still leaves a trace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = path.open("w", encoding="utf-8", errors="replace")
    fh.write(f"Floor startup log ({path})\n")
    fh.flush()
    old_err, old_out = sys.stderr, sys.stdout
    sys.stderr = _Tee(old_err, fh)
    sys.stdout = _Tee(old_out, fh)
    try:
        yield path
    except LaunchError:
        fh.write(traceback.format_exc())
        fh.flush()
        raise
    except BaseException as exc:
        if not isinstance(exc, (KeyboardInterrupt, SystemExit)) or (
            isinstance(exc, SystemExit) and exc.code not in (0, None)
        ):
            traceback.print_exc()
        raise
    finally:
        sys.stderr = old_err
        sys.stdout = old_out
        fh.close()


def _addr_in_use(exc: BaseException) -> bool:
    win = getattr(exc, "winerror", None)
    errno = getattr(exc, "errno", None)
    text = str(exc).lower()
    return win == 10048 or errno in _ADDRINUSE or "10048" in str(exc) or "address already in use" in text


def bind_failure_message(exc: BaseException, host: str, port: int) -> str:
    if _addr_in_use(exc):
        return f"Port {port} is in use by another program."
    return f"Could not bind {host}:{port}: {exc}"


def bind_port(host: str, port: int) -> OSError | None:
    """Try to bind host:port. Return None if it was free (socket is closed)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
        return None
    except OSError as exc:
        return exc
    finally:
        sock.close()


def floor_is_serving(host: str, port: int, timeout: float = 1.0) -> bool:
    """True when GET / looks like Floor (body contains 'The Floor')."""
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("GET", "/")
        resp = conn.getresponse()
        body = resp.read(65536).decode("utf-8", errors="replace")
        return "The Floor" in body
    except Exception:
        return False
    finally:
        conn.close()


def ensure_port_free(host: str, port: int) -> None:
    exc = bind_port(host, port)
    if exc is None:
        return
    raise LaunchError(bind_failure_message(exc, host, port)) from exc


def _adopt_running_floor(host: str, port: int, open_browser: bool, room: Room | None) -> Room:
    url = f"http://{host}:{port}"
    print(f"Floor is already running at {url}")
    if open_browser:
        webbrowser.open(url)
    return room or Room()


def _note_log(path: Path) -> None:
    print(f"Details written to {path}", file=sys.stderr)


def _open_when_ready(server, url: str) -> None:
    for _ in range(200):
        if getattr(server, "started", False):
            break
        time.sleep(0.05)
    webbrowser.open(url)


def run_launch(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    hours: int = 3,
    open_browser: bool = True,
    room: Room | None = None,
    serve=None,
    dest: Path | None = None,
    log_path: Path | None = None,
) -> Room:
    if hours not in (2, 3):
        raise ValueError("hours must be 2 or 3")
    log_path = log_path or LAUNCH_LOG
    with startup_log(log_path):
        try:
            return _run_launch(
                host=host,
                port=port,
                hours=hours,
                open_browser=open_browser,
                room=room,
                serve=serve,
                dest=dest,
            )
        except KeyboardInterrupt:
            print("Stopped.")
            raise
        except LaunchError:
            raise
        except Exception:
            _note_log(log_path)
            raise


def _run_launch(
    *,
    host: str,
    port: int,
    hours: int,
    open_browser: bool,
    room: Room | None,
    serve,
    dest: Path | None,
) -> Room:
    if serve is None:
        bind_exc = bind_port(host, port)
        if bind_exc is not None:
            if _addr_in_use(bind_exc) and floor_is_serving(host, port):
                return _adopt_running_floor(host, port, open_browser, room)
            raise LaunchError(bind_failure_message(bind_exc, host, port)) from bind_exc
    room = room or Room()
    app = build_app(room)
    local = f"http://{host}:{port}"
    tunnel = Tunnel(local, hours, dest or Path(".tunnel-url"))
    try:
        try:
            invite = tunnel.start()
        except LaunchError:
            raise
        except Exception as exc:
            raise LaunchError(f"Could not start the invite tunnel: {exc}") from exc
        room.tunnel_url = invite
        room.tunnel_started = room._clock()
        print(f"The Floor  {local}")
        print(f"Invite     {invite}")
        print(f"Agents     {mcp_url(invite)}")
        if serve is not None:
            if open_browser:
                webbrowser.open(local)
            serve(app, host=host, port=port)
            return room
        import uvicorn

        config = uvicorn.Config(app, host=host, port=port, log_level="info")
        server = uvicorn.Server(config)
        if open_browser:
            threading.Thread(target=_open_when_ready, args=(server, local), daemon=True).start()
        try:
            server.run()
        except SystemExit as exc:
            if exc.code in (0, None):
                raise
            raise LaunchError(bind_failure_message(exc, host, port)) from exc
        except OSError as exc:
            raise LaunchError(bind_failure_message(exc, host, port)) from exc
    finally:
        tunnel.stop()
    return room
