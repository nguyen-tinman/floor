"""trycloudflare child. Two or three hours, then the process dies."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
WINDOWS_AMD64 = (
    "https://github.com/cloudflare/cloudflared/releases/latest/download/"
    "cloudflared-windows-amd64.exe"
)


def _local_binary(dest_dir: Path) -> Path:
    exe = dest_dir / "cloudflared.exe"
    posix = dest_dir / "cloudflared"
    if exe.is_file():
        return exe
    if posix.is_file():
        return posix
    return dest_dir / ("cloudflared.exe" if os.name == "nt" else "cloudflared")


def _download_cloudflared(dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(WINDOWS_AMD64, headers={"User-Agent": "floor"})
    with urllib.request.urlopen(req, timeout=120) as src, tmp.open("wb") as out:
        while True:
            chunk = src.read(1024 * 256)
            if not chunk:
                break
            out.write(chunk)
    tmp.replace(dest)


def resolve_cloudflared(*, download: bool = True, dest_dir: Path | None = None) -> str:
    env = os.environ.get("FLOOR_CLOUDFLARED", "").strip()
    if env and Path(env).is_file():
        return str(Path(env))
    found = shutil.which("cloudflared")
    if found:
        return found
    dest_dir = dest_dir or Path(".run")
    local = _local_binary(dest_dir)
    if local.is_file():
        return str(local.resolve())
    if download and os.name == "nt":
        _download_cloudflared(local)
        return str(local.resolve())
    raise FileNotFoundError(
        "cloudflared not found. Set FLOOR_CLOUDFLARED, install it on PATH, "
        "or place it in .run/"
    )


class Tunnel:
    def __init__(self, local_url: str, hours: int, dest: Path) -> None:
        if hours not in (2, 3):
            raise ValueError("hours must be 2 or 3")
        self.local_url = local_url
        self.deadline = time.time() + hours * 3600
        self.dest = dest
        self.url: str | None = None
        self.proc: subprocess.Popen[str] | None = None

    def start(self, *, binary: str | None = None) -> str:
        exe = binary or resolve_cloudflared()
        self.proc = subprocess.Popen(
            [exe, "tunnel", "--url", self.local_url],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        assert self.proc.stdout
        for line in self.proc.stdout:
            match = URL_RE.search(line)
            if match:
                self.url = match.group(0)
                self.dest.write_text(self.url + "\n", encoding="utf-8")
                break
        if not self.url:
            raise RuntimeError("cloudflared did not print a trycloudflare URL")
        threading.Thread(target=self._watch, daemon=True).start()
        return self.url

    def _watch(self) -> None:
        while time.time() < self.deadline:
            if self.proc and self.proc.poll() is not None:
                return
            time.sleep(1)
        self.stop()

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
