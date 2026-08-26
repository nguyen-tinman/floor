"""One-shot judge CLI spawn. Injectable runner; pytest never calls a live LLM.

Spawn cwd is an empty temp directory so the child does not load this repo's
``.mcp.json``, hooks, or project settings. The runner receives that cwd.
Do not default Claude ``--bare`` (bare skips OAuth/keychain).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from typing import Any, Callable

from debate.errors import FloorError
from debate.prompts import judge_prompt

MODELS = ("claude", "codex", "grok", "gemini")
MAX_TURNS = 4
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)

Runner = Callable[..., Any]


def argv_for(model: str, *, prompt: str, prompt_file: str | None = None) -> list[str]:
    if model == "claude":
        return [
            "claude",
            "-p",
            "--output-format",
            "json",
            prompt,
            "--disallowedTools",
            "Bash",
            "Edit",
        ]
    if model == "codex":
        return ["codex", "exec", "-", "--sandbox", "read-only"]
    if model == "grok":
        if not prompt_file:
            raise FloorError("unavailable", "grok requires --prompt-file")
        return [
            "grok",
            "--no-auto-update",
            "--prompt-file",
            prompt_file,
            "--output-format",
            "json",
            "--max-turns",
            str(MAX_TURNS),
        ]
    if model == "gemini":
        return [
            "gemini",
            "-p",
            prompt,
            "--output-format",
            "json",
            "--approval-mode",
            "plan",
        ]
    raise FloorError("unavailable", f"no spawn argv for {model!r}")


def available(which=shutil.which) -> list[str]:
    return [name for name in MODELS if which(name)]


def parse_verdict(stdout: str) -> dict:
    obj = _json_object(stdout)
    if "winner" not in obj:
        obj = _unwrap_cli_envelope(obj)
    missing = [key for key in ("winner", "runner_up", "honorable") if key not in obj]
    if missing:
        raise FloorError("unavailable", "unparseable judge output")
    out = {key: obj[key] for key in ("winner", "runner_up", "honorable")}
    for key in ("reason", "runner_reason", "honorable_reason", "summary", "highs", "lows"):
        if key in obj:
            out[key] = obj[key]
    return out


def run_judge(model: str, prompt: str, history: list, *, runner: Runner | None = None) -> dict:
    """Build judge_prompt + history JSON, spawn via runner, parse a verdict."""
    if runner is None:
        runner = _subprocess_runner
    text = f"{judge_prompt(motion=prompt, judge=model)}\n\n{json.dumps(history)}"
    # Empty temp cwd: isolate the child from this repo's MCP/project files.
    with tempfile.TemporaryDirectory(prefix="floor-judge-") as cwd:
        prompt_file = None
        stdin = None
        if model == "grok":
            fd, prompt_file = tempfile.mkstemp(prefix="brief-", suffix=".txt", dir=cwd)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
        elif model == "codex":
            stdin = text
        argv = argv_for(model, prompt=text, prompt_file=prompt_file)
        completed = runner(argv, input=stdin, cwd=cwd)
        if getattr(completed, "returncode", 1) != 0:
            err = (getattr(completed, "stderr", None) or "judge process failed").strip()
            raise FloorError("unavailable", err)
        return parse_verdict(getattr(completed, "stdout", "") or "")


def _subprocess_runner(argv, *, input=None, cwd=None):
    return subprocess.run(
        argv,
        input=input,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _json_object(text: str) -> dict:
    blob = (text or "").strip()
    if not blob:
        raise FloorError("unavailable", "unparseable judge output")
    candidates = [blob]
    match = _FENCE.search(blob)
    if match:
        candidates.insert(0, match.group(1).strip())
    for candidate in candidates:
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    raise FloorError("unavailable", "unparseable judge output")


def _unwrap_cli_envelope(obj: dict) -> dict:
    for key in ("result", "response", "structured_output"):
        inner = obj.get(key)
        if isinstance(inner, dict) and "winner" in inner:
            return inner
        if isinstance(inner, str) and inner.strip():
            try:
                return _json_object(inner)
            except FloorError:
                continue
    return obj
