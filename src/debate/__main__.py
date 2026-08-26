"""CLI: serve the floor, optionally with a trycloudflare tunnel."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from debate.cli import AGENT_COMMANDS, add_commands, run as run_agent
from debate.room import Room
from debate.web import build_app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m debate")
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve", help="Serve the web UI and MCP HTTP endpoint")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--tunnel", action="store_true")
    serve.add_argument("--hours", type=int, default=3, choices=(2, 3))
    launch = sub.add_parser("launch", help="Serve, open a tunnel, and open the browser")
    launch.add_argument("--host", default="127.0.0.1")
    launch.add_argument("--port", type=int, default=8765)
    launch.add_argument("--hours", type=int, default=3, choices=(2, 3))
    launch.add_argument("--no-browser", action="store_true")
    mcp = sub.add_parser("mcp", help="Stdio MCP for a single local agent")
    mcp.add_argument("--stdio", action="store_true")
    add_commands(sub)
    return parser


def _room() -> Room:
    room = Room()
    room.configure_judge("gemini", "Gemini")
    room.configure_judge("mistral", "Mistral Large")
    room.configure_judge("local-qwen", "Local · Qwen 72B")
    return room


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.cmd in AGENT_COMMANDS:
        print(json.dumps(run_agent(args)))
        return
    room = _room()
    if args.cmd == "mcp":
        from debate.mcp_server import mount_fastmcp

        app = mount_fastmcp(room)
        asyncio.run(app.run_stdio_async())
        return
    if args.cmd == "launch":
        from debate.launch import LaunchError, run_launch

        try:
            run_launch(
                host=args.host,
                port=args.port,
                hours=args.hours,
                open_browser=not args.no_browser,
                room=room,
            )
        except KeyboardInterrupt:
            print("Stopped.")
            sys.exit(0)
        except LaunchError as exc:
            print(str(exc), file=sys.stderr)
            sys.exit(1)
        except Exception as exc:
            print(str(exc), file=sys.stderr)
            sys.exit(1)
        return

    from debate.launch import (
        LaunchError,
        SERVE_LOG,
        bind_failure_message,
        ensure_port_free,
        startup_log,
    )

    import uvicorn

    try:
        with startup_log(SERVE_LOG):
            ensure_port_free(args.host, args.port)
            app = build_app(room)
            if args.tunnel:
                from debate.tunnel import Tunnel

                tunnel = Tunnel(
                    f"http://{args.host}:{args.port}",
                    args.hours,
                    Path(".tunnel-url"),
                )
                try:
                    url = tunnel.start()
                except Exception as exc:
                    raise LaunchError(f"Could not start the invite tunnel: {exc}") from exc
                room.tunnel_url = url
                room.tunnel_started = room._clock()
                print(f"The Floor  {url}")
            print(f"The Floor  http://{args.host}:{args.port}")
            print(f"Agents     http://{args.host}:{args.port}/mcp")
            try:
                uvicorn.run(app, host=args.host, port=args.port, log_level="info")
            except SystemExit as exc:
                if exc.code in (0, None):
                    raise
                raise LaunchError(
                    bind_failure_message(exc, args.host, args.port)
                ) from exc
            except OSError as exc:
                raise LaunchError(bind_failure_message(exc, args.host, args.port)) from exc
    except KeyboardInterrupt:
        print("Stopped.")
        sys.exit(0)
    except LaunchError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
