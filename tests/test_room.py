"""Room phases: motion, floor, judge, verdict — matching the mockup."""

import asyncio

import pytest

from debate.errors import FloorError
from debate.room import JUDGE_LIMIT_S, TURN_LIMIT_S, Room

PAIR_CODE_TTL_S = 240


def _room(*humans, agents=(), clock=None):
    room = Room(room_id="ORCHID-4471", rng=lambda n: 0, clock=clock)
    seats = []
    for name, ip in humans:
        seats.append(room.join_human(name, ip))
    for name, model in agents:
        room.register_agent(name, model)
    return room, seats


def test_majority_carries_motion_and_picks_opener():
    room, (vale, bram, _) = _room(
        ("Vale", "1.1.1.1"),
        ("Bram", "2.2.2.2"),
        ("Odaline", "3.3.3.3"),
        agents=(("Codex", "codex"), ("Claude", "claude"), ("Grok", "grok")),
    )
    motion = room.propose_topic(vale.session_id, "Resolved: standing follows capacity.")
    room.vote_topic(vale.session_id, motion["id"])
    assert room.snapshot()["phase"] == "lobby"
    room.vote_topic(bram.session_id, motion["id"])
    snap = room.snapshot()
    assert snap["phase"] == "debating"
    assert snap["topic"]["text"].startswith("Resolved:")
    assert snap["speaker"] == "Codex"
    assert snap["opener"] == "Codex"


def test_one_human_vote_is_a_majority():
    room, (vale,) = _room(("Vale", "1.1.1.1"), agents=(("Grok", "grok"),))
    motion = room.propose_topic(vale.session_id, "Resolved: wrappers are costumes.")
    room.vote_topic(vale.session_id, motion["id"])
    assert room.snapshot()["phase"] == "debating"


def test_round_robin_advances_after_send():
    room, _ = _room(
        ("Vale", "1.1.1.1"),
        agents=(("Codex", "codex"), ("Claude", "claude"), ("Grok", "grok")),
    )
    vale = room.humans()[0]
    motion = room.propose_topic(vale.session_id, "Resolved: a")
    room.vote_topic(vale.session_id, motion["id"])
    room.send_message("Codex", "Opening.")
    assert room.snapshot()["speaker"] == "Claude"
    room.send_message("Claude", "Answer.")
    assert room.snapshot()["speaker"] == "Grok"


def test_wrong_agent_cannot_speak():
    room, _ = _room(("Vale", "1.1.1.1"), agents=(("Codex", "codex"), ("Grok", "grok")))
    vale = room.humans()[0]
    motion = room.propose_topic(vale.session_id, "Resolved: a")
    room.vote_topic(vale.session_id, motion["id"])
    with pytest.raises(FloorError) as err:
        room.send_message("Grok", "Not my turn.")
    assert err.value.code == "not_your_turn"


def test_heckle_is_not_in_agent_history():
    room, (vale,) = _room(("Vale", "1.1.1.1"), agents=(("Grok", "grok"),))
    motion = room.propose_topic(vale.session_id, "Resolved: a")
    room.vote_topic(vale.session_id, motion["id"])
    room.heckle(vale.session_id, "Codex has said recourse eleven times.")
    history = room.history()
    assert history == []
    assert room.snapshot()["heckles"][0]["text"].startswith("Codex")
    pulled = room.pull("Grok")
    assert "heckles" not in pulled


def test_question_rides_the_next_wait_payload():
    room, (vale,) = _room(
        ("Vale", "1.1.1.1"),
        agents=(("Codex", "codex"), ("Claude", "claude")),
    )
    motion = room.propose_topic(vale.session_id, "Resolved: a")
    room.vote_topic(vale.session_id, motion["id"])
    room.ask(vale.session_id, "Whose capacity?")
    room.send_message("Codex", "Opening.")
    wake = room.peek_wake("Claude")
    assert "Whose capacity?" in wake["prompt"]
    assert wake["kind"] == "your_turn"


def test_majority_call_it_moves_to_judge_vote():
    room, (vale, bram, _) = _room(
        ("Vale", "1.1.1.1"),
        ("Bram", "2.2.2.2"),
        ("Odaline", "3.3.3.3"),
        agents=(("Grok", "grok"),),
    )
    motion = room.propose_topic(vale.session_id, "Resolved: a")
    room.vote_topic(vale.session_id, motion["id"])
    room.vote_topic(bram.session_id, motion["id"])
    assert room.snapshot()["phase"] == "debating"
    room.call_it(vale.session_id)
    assert room.snapshot()["phase"] == "debating"
    room.call_it(bram.session_id)
    assert room.snapshot()["phase"] == "judge_vote"


def test_host_can_close_now():
    room, (vale, _) = _room(
        ("Vale", "1.1.1.1"),
        ("Bram", "2.2.2.2"),
        agents=(("Grok", "grok"),),
    )
    motion = room.propose_topic(vale.session_id, "Resolved: a")
    room.vote_topic(vale.session_id, motion["id"])
    room.close_now(vale.session_id)
    assert room.snapshot()["phase"] == "judge_vote"


def _close_after_two_speeches():
    room, (vale,) = _room(("Vale", "1.1.1.1"), agents=(("Grok", "grok"), ("Claude", "claude")))
    motion = room.propose_topic(vale.session_id, "Resolved: a")
    room.vote_topic(vale.session_id, motion["id"])
    room.send_message("Grok", "Capacity is standing.")
    room.send_message("Claude", "A wrapper is a costume.")
    room.close_now(vale.session_id)
    return room, vale


def test_debater_can_be_appointed_judge():
    room, vale = _close_after_two_speeches()
    judges = {j["model"]: j for j in room.snapshot()["judges"]}
    assert judges["grok"]["disabled"] is False
    assert judges["claude"]["disabled"] is False
    snap = room.vote_judge(vale.session_id, "grok")
    assert snap["phase"] == "judging"
    assert room.agents["Grok"].role == "judge"


def test_appointed_debater_cannot_name_self():
    room, vale = _close_after_two_speeches()
    room.vote_judge(vale.session_id, "grok")
    with pytest.raises(FloorError) as err:
        room.submit_verdict(
            "Grok",
            winner="Grok",
            runner_up="Claude",
            honorable="Claude",
            reason="moved the ground",
        )
    assert err.value.code == "ineligible"
    room.submit_verdict(
        "Grok",
        winner="Claude",
        runner_up="Claude",
        honorable="Claude",
        reason="answered what was put",
        summary="Grok recused.",
    )
    assert room.snapshot()["verdict"]["winner"] == "Claude"


def test_verdict_keeps_highs_and_lows_capped():
    room, vale = _close_after_two_speeches()
    room.vote_judge(vale.session_id, "grok")
    claude_id = next(line["id"] for line in room.history() if line["speaker"] == "Claude")
    extras = [
        {"id": claude_id, "quote": "A wrapper is a costume.", "note": "Named the clash.", "speaker": "Claude"},
        {"id": claude_id, "quote": "second", "note": "n2"},
        {"id": claude_id, "quote": "third", "note": "n3"},
        {"id": claude_id, "quote": "dropped", "note": "n4"},
    ]
    room.submit_verdict(
        "Grok",
        winner="Claude",
        runner_up="Claude",
        honorable="Claude",
        reason="answered",
        highs=extras,
        lows=[{"id": 999, "quote": "missing"}, {"id": claude_id, "quote": "costume", "note": "Thin on standing."}],
    )
    cites = room.snapshot()["verdict"]
    assert [c["quote"] for c in cites["highs"]] == [
        "A wrapper is a costume.",
        "second",
        "third",
    ]
    assert cites["lows"] == [
        {"id": str(claude_id), "quote": "costume", "note": "Thin on standing.", "speaker": "Claude"}
    ]


def test_appointed_debater_history_omits_own_lines():
    room, vale = _close_after_two_speeches()
    room.vote_judge(vale.session_id, "grok")
    wake = room.peek_wake("Grok")
    assert wake["kind"] == "judge"
    speakers = [line["speaker"] for line in wake["history"]]
    assert "Grok" not in speakers
    assert "Claude" in speakers
    pulled = room.pull("Grok")
    assert "Grok" not in [line["speaker"] for line in pulled["history"]]
    assert "cannot name yourself" in wake["prompt"]


def test_never_seated_model_cannot_judge():
    room, vale = _close_after_two_speeches()
    room.configure_judge("gemini", "Gemini · never seated")
    models = {j["model"] for j in room.snapshot()["judges"]}
    assert "gemini" not in models
    with pytest.raises(FloorError) as err:
        room.vote_judge(vale.session_id, "gemini")
    assert err.value.code in {"invalid", "ineligible"}
    assert room.phase == "judge_vote"


def test_judge_timeout_returns_to_judge_vote():
    now = [1_000.0]
    room, (vale,) = _room(
        ("Vale", "1.1.1.1"),
        agents=(("Grok", "grok"), ("Claude", "claude")),
        clock=lambda: now[0],
    )
    motion = room.propose_topic(vale.session_id, "Resolved: a")
    room.vote_topic(vale.session_id, motion["id"])
    room.send_message("Grok", "Capacity is standing.")
    room.send_message("Claude", "A wrapper is a costume.")
    room.close_now(vale.session_id)
    room.vote_judge(vale.session_id, "grok")
    assert room.phase == "judging"
    assert room.agents["Grok"].role == "judge"
    assert room.snapshot()["turn_limit_s"] == int(JUDGE_LIMIT_S)
    now[0] = room.turn_started + TURN_LIMIT_S
    room.check_timeouts()
    assert room.phase == "judging"
    now[0] = room.turn_started + JUDGE_LIMIT_S
    room.check_timeouts()
    assert room.phase == "judge_vote"
    assert room.voted_judge is None
    assert room.agents["Grok"].role == "agent"
    snap = room.vote_judge(vale.session_id, "claude")
    assert snap["phase"] == "judging"
    assert room.agents["Claude"].role == "judge"


@pytest.mark.asyncio
async def test_wait_blocks_until_harness_pushes():
    room, _ = _room(("Vale", "1.1.1.1"), agents=(("Grok", "grok"),))
    vale = room.humans()[0]
    pending = asyncio.create_task(room.wait("Grok", timeout_s=2))
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
async def test_wait_timeout_returns_arrived_false():
    room, _ = _room(("Vale", "1.1.1.1"), agents=(("Grok", "grok"),))
    wake = await room.wait("Grok", timeout_s=0.01)
    assert wake["arrived"] is False
    assert wake["ok"] is True


def test_claim_pair_code_registers_agent():
    now = [1_000.0]
    room, _ = _room(clock=lambda: now[0])
    code = room.mint_pair_code()
    agent = room.claim_pair_code(code, "Grok", "grok")
    assert agent.name == "Grok"
    assert agent.model == "grok"
    assert "Grok" in room.agents


def test_expired_pair_code_is_dead():
    now = [1_000.0]
    room, _ = _room(clock=lambda: now[0])
    code = room.mint_pair_code()
    now[0] += PAIR_CODE_TTL_S + 1
    with pytest.raises(FloorError) as err:
        room.claim_pair_code(code, "Grok", "grok")
    assert err.value.code == "invalid"
    assert err.value.message == "pairing code is dead"
    assert "Grok" not in room.agents


def test_pair_code_is_single_use():
    room, _ = _room()
    code = room.mint_pair_code()
    room.claim_pair_code(code, "Grok", "grok")
    with pytest.raises(FloorError) as err:
        room.claim_pair_code(code, "Claude", "claude")
    assert err.value.code == "invalid"
    assert err.value.message == "pairing code is dead"
    assert "Claude" not in room.agents


def test_timeout_forfeits_and_advances_speaker():
    now = [1_000.0]
    room, (vale,) = _room(
        ("Vale", "1.1.1.1"),
        agents=(("Codex", "codex"), ("Claude", "claude")),
        clock=lambda: now[0],
    )
    motion = room.propose_topic(vale.session_id, "Resolved: a")
    room.vote_topic(vale.session_id, motion["id"])
    assert room.speaker == "Codex"
    assert room.snapshot()["turn_limit_s"] == int(TURN_LIMIT_S)
    now[0] = room.turn_started + TURN_LIMIT_S
    room.check_timeouts()
    assert room.speaker == "Claude"
    assert room.history() == []


def test_watcher_cannot_propose_or_vote_topic():
    room, (vale,) = _room(("Vale", "1.1.1.1"))
    watcher = room.join_human("Nara", "9.9.9.9", watcher=True)
    with pytest.raises(FloorError) as err:
        room.propose_topic(watcher.session_id, "Resolved: standing follows capacity.")
    assert err.value.code == "forbidden"
    motion = room.propose_topic(vale.session_id, "Resolved: standing follows capacity.")
    with pytest.raises(FloorError) as err:
        room.vote_topic(watcher.session_id, motion["id"])
    assert err.value.code == "forbidden"


def test_late_joiner_enters_rotation_for_later_turn():
    room, (vale,) = _room(("Vale", "1.1.1.1"), agents=(("Claude", "claude"),))
    motion = room.propose_topic(vale.session_id, "Resolved: a")
    room.vote_topic(vale.session_id, motion["id"])
    assert room.speaker == "Claude"
    room.register_agent("Grok", "grok")
    assert room.speaker == "Claude"
    assert "Grok" in {a["name"] for a in room.snapshot()["agents"]}
    room.send_message("Claude", "Opening.")
    assert room.speaker == "Grok"
    wake = room.peek_wake("Grok")
    assert wake["kind"] == "your_turn"
    assert wake["arrived"] is True


def test_same_name_reregister_is_not_a_second_speaker():
    room, (vale,) = _room(("Vale", "1.1.1.1"), agents=(("Claude", "claude"),))
    motion = room.propose_topic(vale.session_id, "Resolved: a")
    room.vote_topic(vale.session_id, motion["id"])
    first = room.agents["Claude"]
    again = room.register_agent("Claude", "claude")
    assert again is first
    room.register_agent("Grok", "grok")
    names = [o["name"] for o in room.snapshot()["order"]]
    assert names.count("Claude") == 1
    room.send_message("Claude", "Opening.")
    assert room.speaker == "Grok"
    room.send_message("Grok", "Reply.")
    assert room.speaker == "Claude"
    assert [o["name"] for o in room.snapshot()["order"]].count("Claude") == 1


def test_timeout_after_late_join_advances_to_late_joiner():
    now = [1_000.0]
    room, (vale,) = _room(
        ("Vale", "1.1.1.1"),
        agents=(("Claude", "claude"),),
        clock=lambda: now[0],
    )
    motion = room.propose_topic(vale.session_id, "Resolved: a")
    room.vote_topic(vale.session_id, motion["id"])
    room.register_agent("Grok", "grok")
    now[0] = room.turn_started + TURN_LIMIT_S
    room.check_timeouts()
    assert room.speaker == "Grok"
    assert room.peek_wake("Grok")["kind"] == "your_turn"


def test_register_agent_emits_room_update():
    room = Room(room_id="ORCHID-4471", rng=lambda n: 0)
    events = []
    room.subscribe(lambda event, _payload: events.append(event))
    room.register_agent("Grok", "grok")
    assert "player:joined" in events
    assert "room:update" in events
