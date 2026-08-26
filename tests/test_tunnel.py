"""trycloudflare wrapper: hours 2 or 3, URL scraped from cloudflared stdout."""

from io import StringIO
from pathlib import Path

import pytest

from debate.tunnel import URL_RE, Tunnel, resolve_cloudflared


@pytest.fixture(autouse=True)
def no_live_cloudflared_download(monkeypatch):
    def blocked(*_a, **_k):
        raise AssertionError("tests must not download cloudflared")

    monkeypatch.setattr("debate.tunnel.urllib.request.urlopen", blocked)


class _FakePopen:
    """Stand-in so tests never spawn real cloudflared."""

    last_args = None
    last_kwargs = None

    def __init__(self, args, **kwargs):
        type(self).last_args = args
        type(self).last_kwargs = kwargs
        self.stdout = StringIO(
            "INF Thank you for trying Cloudflare Tunnel.\n"
            "INF |  https://orchid-4471.trycloudflare.com\n"
        )

    def poll(self):
        return 0

    def terminate(self):
        return None


def test_hours_1_raises_before_popen(monkeypatch, tmp_path):
    _FakePopen.last_args = None
    monkeypatch.setattr("debate.tunnel.subprocess.Popen", _FakePopen)
    with pytest.raises(ValueError, match="hours must be 2 or 3"):
        Tunnel("http://127.0.0.1:8765", 1, tmp_path / "tunnel.txt")
    assert _FakePopen.last_args is None


def test_hours_4_raises_before_popen(monkeypatch, tmp_path):
    _FakePopen.last_args = None
    monkeypatch.setattr("debate.tunnel.subprocess.Popen", _FakePopen)
    with pytest.raises(ValueError, match="hours must be 2 or 3"):
        Tunnel("http://127.0.0.1:8765", 4, tmp_path / "tunnel.txt")
    assert _FakePopen.last_args is None


def test_start_writes_url_from_fake_stdout(monkeypatch, tmp_path):
    dest = tmp_path / "tunnel.txt"
    monkeypatch.setattr("debate.tunnel.subprocess.Popen", _FakePopen)
    tunnel = Tunnel("http://127.0.0.1:8765", 2, dest)
    url = tunnel.start(binary="cloudflared")
    assert url == "https://orchid-4471.trycloudflare.com"
    assert dest.read_text(encoding="utf-8") == url + "\n"
    assert _FakePopen.last_args == [
        "cloudflared",
        "tunnel",
        "--url",
        "http://127.0.0.1:8765",
    ]


def test_start_accepts_three_hours(monkeypatch, tmp_path):
    dest = tmp_path / "tunnel.txt"
    monkeypatch.setattr("debate.tunnel.subprocess.Popen", _FakePopen)
    url = Tunnel("http://127.0.0.1:8765", 3, dest).start(binary="cloudflared")
    assert url == "https://orchid-4471.trycloudflare.com"
    assert dest.read_text(encoding="utf-8") == url + "\n"


def test_start_uses_resolved_binary(monkeypatch, tmp_path):
    dest = tmp_path / "tunnel.txt"
    fake = tmp_path / "fake-cloudflared.exe"
    fake.write_bytes(b"fake")
    monkeypatch.setattr("debate.tunnel.subprocess.Popen", _FakePopen)
    monkeypatch.setattr("debate.tunnel.resolve_cloudflared", lambda **_k: str(fake))
    Tunnel("http://127.0.0.1:8765", 3, dest).start()
    assert _FakePopen.last_args[0] == str(fake)


def _block_download(monkeypatch):
    def blocked(_dest):
        raise AssertionError("tests must not download cloudflared")

    monkeypatch.setattr("debate.tunnel._download_cloudflared", blocked)


def test_resolve_env_beats_path_and_run(monkeypatch, tmp_path):
    _block_download(monkeypatch)
    env_bin = tmp_path / "from-env.exe"
    env_bin.write_bytes(b"env")
    run_dir = tmp_path / ".run"
    run_dir.mkdir()
    (run_dir / "cloudflared.exe").write_bytes(b"run")
    monkeypatch.setenv("FLOOR_CLOUDFLARED", str(env_bin))
    monkeypatch.setattr("debate.tunnel.shutil.which", lambda _name: str(tmp_path / "from-path.exe"))
    assert resolve_cloudflared(dest_dir=run_dir) == str(env_bin)


def test_resolve_path_beats_run(monkeypatch, tmp_path):
    _block_download(monkeypatch)
    monkeypatch.delenv("FLOOR_CLOUDFLARED", raising=False)
    path_bin = tmp_path / "from-path.exe"
    path_bin.write_bytes(b"path")
    run_dir = tmp_path / ".run"
    run_dir.mkdir()
    (run_dir / "cloudflared.exe").write_bytes(b"run")
    monkeypatch.setattr("debate.tunnel.shutil.which", lambda _name: str(path_bin))
    assert resolve_cloudflared(dest_dir=run_dir) == str(path_bin)


def test_resolve_run_when_env_and_path_miss(monkeypatch, tmp_path):
    _block_download(monkeypatch)
    monkeypatch.delenv("FLOOR_CLOUDFLARED", raising=False)
    monkeypatch.setattr("debate.tunnel.shutil.which", lambda _name: None)
    run_dir = tmp_path / ".run"
    run_dir.mkdir()
    bundled = run_dir / "cloudflared.exe"
    bundled.write_bytes(b"run")
    assert Path(resolve_cloudflared(dest_dir=run_dir)).resolve() == bundled.resolve()


def test_resolve_downloads_once_into_run(monkeypatch, tmp_path):
    monkeypatch.delenv("FLOOR_CLOUDFLARED", raising=False)
    monkeypatch.setattr("debate.tunnel.shutil.which", lambda _name: None)
    monkeypatch.setattr("debate.tunnel.os.name", "nt")
    run_dir = tmp_path / ".run"
    calls: list[Path] = []

    def fake_download(dest: Path) -> None:
        calls.append(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"downloaded")

    monkeypatch.setattr("debate.tunnel._download_cloudflared", fake_download)
    first = resolve_cloudflared(dest_dir=run_dir)
    second = resolve_cloudflared(dest_dir=run_dir)
    assert first == second
    assert Path(first).read_bytes() == b"downloaded"
    assert len(calls) == 1
    assert calls[0].name == "cloudflared.exe"


def test_resolve_missing_raises_without_download(monkeypatch, tmp_path):
    _block_download(monkeypatch)
    monkeypatch.delenv("FLOOR_CLOUDFLARED", raising=False)
    monkeypatch.setattr("debate.tunnel.shutil.which", lambda _name: None)
    with pytest.raises(FileNotFoundError, match="cloudflared not found"):
        resolve_cloudflared(download=False, dest_dir=tmp_path / ".run")


def test_url_re_accepts_trycloudflare_host():
    assert URL_RE.search("https://abc-123.trycloudflare.com")
    assert URL_RE.search("see https://foo.trycloudflare.com in logs")


def test_url_re_rejects_non_trycloudflare_hosts():
    assert URL_RE.search("https://evil.example.com") is None
    assert URL_RE.search("https://trycloudflare.com") is None
    assert URL_RE.search("http://orchid-4471.trycloudflare.com") is None
    assert URL_RE.search("https://ORCHID.trycloudflare.com") is None
