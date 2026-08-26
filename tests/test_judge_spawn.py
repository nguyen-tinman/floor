"""Judge CLI argv and injectable runner — never spawn a live model."""

import json
import os

import pytest

from debate.errors import FloorError
from debate.judge_spawn import argv_for, available, parse_verdict, run_judge
from debate.prompts import JUDGE_BRIEF

VERDICT = {
    "winner": "Codex",
    "runner_up": "Claude",
    "honorable": "Grok",
    "reason": "answered the motion",
    "runner_reason": "close second",
    "honorable_reason": "one sharp reply",
    "summary": "capacity carried the floor",
}

HISTORY = [
    {
        "id": 1,
        "ts": "2026-08-24T08:00:00+00:00",
        "speaker": "Grok",
        "role": "agent",
        "model": "grok",
        "text": "Standing follows capacity.",
    }
]


class Completed:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class Recorder:
    def __init__(self, result):
        self.result = result
        self.argv = None
        self.input = None
        self.cwd = None
        self.cwd_names = None

    def __call__(self, argv, *, input=None, cwd=None):
        self.argv = list(argv)
        self.input = input
        self.cwd = cwd
        self.cwd_names = os.listdir(cwd) if cwd and os.path.isdir(cwd) else None
        return self.result


def test_claude_argv_is_print_json_without_bare():
    prompt = "Name a winner."
    argv = argv_for("claude", prompt=prompt)
    assert argv[:5] == ["claude", "-p", "--output-format", "json", prompt]
    assert argv[5:] == ["--disallowedTools", "Bash", "Edit"]
    assert "--bare" not in argv


def test_codex_argv_is_exec_stdin_readonly():
    argv = argv_for("codex", prompt="Name a winner.")
    assert argv == ["codex", "exec", "-", "--sandbox", "read-only"]


def test_grok_argv_uses_prompt_file_not_yolo():
    argv = argv_for("grok", prompt="Name a winner.", prompt_file="C:\\tmp\\brief.txt")
    assert argv[:4] == ["grok", "--no-auto-update", "--prompt-file", "C:\\tmp\\brief.txt"]
    assert argv[4:] == ["--output-format", "json", "--max-turns", "4"]
    assert "--yolo" not in argv
    assert "prompt" not in argv


def test_gemini_argv_is_prompt_json_plan():
    prompt = "Name a winner."
    argv = argv_for("gemini", prompt=prompt)
    assert argv == ["gemini", "-p", prompt, "--output-format", "json", "--approval-mode", "plan"]


def test_argv_for_unknown_model_is_unavailable():
    with pytest.raises(FloorError) as err:
        argv_for("agy", prompt="x")
    assert err.value.code == "unavailable"


def test_run_judge_records_claude_argv_and_empty_temp_cwd():
    runner = Recorder(Completed(0, json.dumps(VERDICT)))
    out = run_judge("claude", "Resolved: testers first.", HISTORY, runner=runner)
    assert out["winner"] == "Codex"
    assert out["runner_up"] == "Claude"
    assert out["honorable"] == "Grok"
    assert runner.argv[:4] == ["claude", "-p", "--output-format", "json"]
    assert "--bare" not in runner.argv
    assert "Bash" in runner.argv and "Edit" in runner.argv
    assert runner.input is None
    assert runner.cwd is not None
    assert "floor-judge-" in os.path.basename(runner.cwd)
    assert runner.cwd_names == []
    prompt = runner.argv[4]
    assert JUDGE_BRIEF in prompt
    assert "Resolved: testers first." in prompt
    assert json.dumps(HISTORY) in prompt


def test_run_judge_codex_sends_prompt_on_stdin():
    runner = Recorder(Completed(0, json.dumps(VERDICT)))
    run_judge("codex", "Resolved: testers first.", HISTORY, runner=runner)
    assert runner.argv == ["codex", "exec", "-", "--sandbox", "read-only"]
    assert runner.input is not None
    assert JUDGE_BRIEF in runner.input
    assert "Resolved: testers first." in runner.input
    assert json.dumps(HISTORY) in runner.input
    assert runner.cwd is not None


def test_run_judge_grok_writes_prompt_file():
    seen = {}

    def runner(argv, *, input=None, cwd=None):
        seen["argv"] = list(argv)
        seen["input"] = input
        seen["cwd"] = cwd
        path = argv[argv.index("--prompt-file") + 1]
        seen["file"] = path
        seen["text"] = open(path, encoding="utf-8").read()
        return Completed(0, json.dumps(VERDICT))

    out = run_judge("grok", "Resolved: testers first.", HISTORY, runner=runner)
    assert out["winner"] == "Codex"
    assert seen["argv"][:3] == ["grok", "--no-auto-update", "--prompt-file"]
    assert seen["argv"][4:] == ["--output-format", "json", "--max-turns", "4"]
    assert "--yolo" not in seen["argv"]
    assert seen["input"] is None
    assert JUDGE_BRIEF in seen["text"]
    assert json.dumps(HISTORY) in seen["text"]


def test_run_judge_gemini_records_plan_argv():
    runner = Recorder(Completed(0, json.dumps(VERDICT)))
    run_judge("gemini", "Resolved: testers first.", HISTORY, runner=runner)
    assert runner.argv[0:2] == ["gemini", "-p"]
    assert runner.argv[3:] == ["--output-format", "json", "--approval-mode", "plan"]
    assert JUDGE_BRIEF in runner.argv[2]
    assert json.dumps(HISTORY) in runner.argv[2]


def test_parse_verdict_raw_json():
    parsed = parse_verdict(json.dumps(VERDICT))
    assert parsed["winner"] == "Codex"
    assert parsed["runner_up"] == "Claude"
    assert parsed["honorable"] == "Grok"
    assert parsed["reason"] == "answered the motion"
    assert parsed["summary"] == "capacity carried the floor"


def test_parse_verdict_fenced_json():
    blob = "Here is the bench:\n```json\n" + json.dumps(VERDICT) + "\n```\n"
    parsed = parse_verdict(blob)
    assert parsed["winner"] == "Codex"
    assert parsed["honorable"] == "Grok"


def test_parse_verdict_claude_result_string():
    inner = json.dumps({"winner": "A", "runner_up": "B", "honorable": "C"})
    envelope = json.dumps({"type": "result", "result": inner})
    parsed = parse_verdict(envelope)
    assert parsed == {"winner": "A", "runner_up": "B", "honorable": "C"}


def test_parse_verdict_claude_result_fenced():
    inner = "```json\n" + json.dumps({"winner": "A", "runner_up": "B", "honorable": "C"}) + "\n```"
    parsed = parse_verdict(json.dumps({"result": inner}))
    assert parsed["winner"] == "A"


def test_nonzero_returncode_is_unavailable():
    runner = Recorder(Completed(1, "", "cli missing"))
    with pytest.raises(FloorError) as err:
        run_judge("codex", "motion", HISTORY, runner=runner)
    assert err.value.code == "unavailable"


def test_unparseable_stdout_is_unavailable():
    runner = Recorder(Completed(0, "the winner is obviously Codex"))
    with pytest.raises(FloorError) as err:
        run_judge("codex", "motion", HISTORY, runner=runner)
    assert err.value.code == "unavailable"


def test_available_uses_injected_which():
    found = {
        "claude": r"C:\Users\mguye\.local\bin\claude.exe",
        "grok": r"C:\Users\mguye\.grok\bin\grok.exe",
        "agy": r"C:\Users\mguye\AppData\Local\agy\bin\agy.exe",
        "mistral": r"C:\bin\mistral.exe",
        "qwen": r"C:\bin\qwen.cmd",
    }

    def which(name):
        return found.get(name)

    assert available(which=which) == ["claude", "grok"]


def test_available_empty_when_none_on_path():
    assert available(which=lambda name: None) == []
