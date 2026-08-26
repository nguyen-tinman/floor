"""In-process FloorMcp: register first, envelopes, wait, judge attach."""

import asyncio

import pytest

from debate.errors import FloorError, envelope_err
from debate.mcp_server import FloorMcp, call, mount_fastmcp
from debate.room import Room


def _seated(*agents):
    room = Room(room_id="ORCHID-4471", rng=lambda n: 0)
    vale = room.join_human("Vale", "1.1.1.1")
    for name, model in agents:
        room.register_agent(name, model)
    return room, vale


def test_register_must_be_first():
    tools = FloorMcp(Room())
    out = tools.register("sess", "Grok", "grok")
    assert out["ok"] is True
    assert out["agent"] == "Grok"
    assert out["model"] == "grok"
    assert out["role"] == "agent"
    assert out["phase"] == "lobby"


@pytest.mark.asyncio
async def test_unregistered_wait_is_not_registered_envelope():
    tools = FloorMcp(Room())
    try:
        result = await tools.wait("nobody")
    except FloorError as exc:
        result = envelope_err(exc)
    assert result["ok"] is False
    assert result["error"]["code"] == "not_registered"
    assert call(tools.who, "nobody")["error"]["code"] == "not_registered"


@pytest.mark.asyncio
async def test_wait_blocks_then_wakes():
    room, vale = _seated(("Grok", "grok"))
    tools = FloorMcp(room)
    tools.bind("sess", "Grok")
    pending = asyncio.create_task(tools.wait("sess", timeout_s=2))
    await asyncio.sleep(0)
    assert not pending.done()
    motion = room.propose_topic(vale.session_id, "Resolved: a")
    room.vote_topic(vale.session_id, motion["id"])
    wake = await pending
    assert wake["ok"] is True
    assert wake["arrived"] is True
    assert wake["kind"] == "your_turn"
    assert wake["history"] == []


@pytest.mark.asyncio
async def test_wait_timeout_arrived_false_is_success():
    room, _ = _seated(("Grok", "grok"))
    tools = FloorMcp(room)
    tools.bind("sess", "Grok")
    wake = await tools.wait("sess", timeout_s=0.01)
    assert wake["ok"] is True
    assert wake["arrived"] is False
    assert wake["kind"] == "info"


@pytest.mark.asyncio
async def test_judge_attach_after_vote():
    room, vale = _seated(("Grok", "grok"), ("Claude", "claude"))
    motion = room.propose_topic(vale.session_id, "Resolved: a")
    room.vote_topic(vale.session_id, motion["id"])
    room.send_message("Grok", "Opening.")
    room.send_message("Claude", "Reply.")
    room.close_now(vale.session_id)
    room.vote_judge(vale.session_id, "grok")
    assert room.phase == "judging"

    tools = FloorMcp(room)
    tools.bind("sess", "Grok")
    wake = await tools.wait("sess", timeout_s=0.5)
    assert wake["ok"] is True
    assert wake["arrived"] is True
    assert wake["kind"] == "judge"

    verdict = tools.submit_verdict(
        "sess",
        "Claude",
        "Claude",
        "Claude",
        reason="moved the ground",
        runner_reason="held the line",
        honorable_reason="one sharp reply",
        summary="A short night.",
    )
    assert verdict["ok"] is True
    assert verdict["phase"] == "verdict"
    assert verdict["verdict"]["winner"] == "Claude"
    assert verdict["verdict"]["reason"] == "moved the ground"
    assert verdict["verdict"]["runner_reason"] == "held the line"
    assert verdict["verdict"]["honorable_reason"] == "one sharp reply"
    assert verdict["verdict"]["summary"] == "A short night."


@pytest.mark.asyncio
async def test_submit_verdict_tool_accepts_a_reason_for_each_plate():
    app = mount_fastmcp(Room())
    tools = await app.list_tools()
    verdict = next(tool for tool in tools if tool.name == "submit_verdict")
    props = verdict.inputSchema["properties"]
    assert "reason" in props
    assert "runner_reason" in props
    assert "honorable_reason" in props
    assert "summary" in props
