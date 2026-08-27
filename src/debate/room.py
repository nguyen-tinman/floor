"""In-memory floor: seats, motions, turns, waits."""

from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from debate.errors import FloorError
from debate.identity import HumanSession, IdentityBook
from debate.prompts import JUDGE_BRIEF, judge_prompt, turn_prompt

MAX_WAIT_S = 120.0
TURN_LIMIT_S = 120.0
JUDGE_LIMIT_S = 300.0
STALE_S = 60.0
SEATS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
PHASES = ("lobby", "debating", "judge_vote", "judging", "verdict", "expired")


def _now() -> float:
    return time.time()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _clean_agent_name(name: str) -> str:
    cleaned = " ".join(name.split())
    if not cleaned or len(cleaned) > 60:
        raise FloorError("invalid", "agent name must be 1-60 characters")
    return cleaned


def _rel(ts: float, now: float) -> str:
    delta = max(0, int(now - ts))
    if delta < 60:
        return f"{delta}s ago" if delta else "just now"
    if delta < 3600:
        return f"{delta // 60} min ago"
    return f"{delta // 3600} h {delta % 3600 // 60} min"


@dataclass
class Agent:
    name: str
    model: str
    role: str
    seat: str
    joined_at: float
    turns: int = 0
    last_push: float | None = None
    turnarounds: list[float] = field(default_factory=list)
    pending_wake: dict[str, Any] | None = None
    event: asyncio.Event = field(default_factory=asyncio.Event)
    dropped: bool = False
    last_seen: float = 0.0
    parked: bool = False
    away: bool = False


@dataclass
class Topic:
    id: str
    text: str
    by: str
    votes: set[str] = field(default_factory=set)


@dataclass
class Line:
    id: int
    ts: float
    speaker: str
    role: str
    model: str
    text: str
    round: int
    notes: str = ""
    replied_to: str = ""


class Room:
    def __init__(
        self,
        *,
        room_id: str | None = None,
        rng: Callable[[int], int] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.room_id = room_id or _mint_room_id()
        self._rng = rng or (lambda n: secrets.randbelow(n) if n else 0)
        self._clock = clock or _now
        self.identity = IdentityBook()
        self.agents: dict[str, Agent] = {}
        self.phase = "lobby"
        self.seq = 0
        self.topics: list[Topic] = []
        self.topic: Topic | None = None
        self.order: list[str] = []
        self.speaker: str | None = None
        self.opener: str | None = None
        self.round = 0
        self.turn_started: float | None = None
        self.lines: list[Line] = []
        self.heckles: list[dict[str, str]] = []
        self.pending_question: str | None = None
        self.call_it_votes: set[str] = set()
        self.round_hold = False
        self.round_votes: dict[str, str] = {}
        self.verbosity = ""
        self.verbosity_votes: dict[str, str] = {}
        self.kick_votes: dict[str, str] = {}
        self.judge_votes: dict[str, str] = {}
        self.judge_models: dict[str, str] = {}
        self.voted_judge: str | None = None
        self._sitting_from_floor: str | None = None
        self._sitting_prior_seat: str | None = None
        self.verdict: dict[str, Any] | None = None
        self.tunnel_url: str | None = None
        self.tunnel_started: float | None = None
        self.started_at = self._clock()
        self._next_line = 1
        self._listeners: list[Callable[[str, dict[str, Any]], None]] = []
        self._pair: dict[str, float] = {}

    def subscribe(self, fn: Callable[[str, dict[str, Any]], None]) -> None:
        self._listeners.append(fn)

    def humans(self) -> list[HumanSession]:
        return self.identity.humans()

    def join_human(
        self, name: str, ip: str, *, token: str | None = None, watcher: bool = False
    ) -> HumanSession:
        human = self.identity.join(name, ip, token=token, watcher=watcher)
        self._emit("player:joined", human.public())
        self._emit("room:update", self.snapshot())
        return human

    def require_human(self, session_id: str) -> HumanSession:
        human = self.identity.get(session_id)
        if not human:
            raise FloorError("not_registered", "unknown seat")
        return human

    def register_agent(self, name: str, model: str, *, role: str = "agent") -> Agent:
        cleaned = _clean_agent_name(name)
        model = (model or "").strip() or "unknown"
        if role not in {"agent", "judge"}:
            raise FloorError("invalid", "role must be agent or judge")
        if role == "judge":
            if self.phase != "judging":
                raise FloorError("unavailable", "the bench is not sitting")
            if model != self.voted_judge:
                raise FloorError("ineligible", "this is not the voted bench")
            existing = self.agents.get(cleaned)
            if not existing or existing.model != model:
                raise FloorError("ineligible", "only a seated agent can sit")
            self._touch(existing)
            if existing.role != "judge":
                existing.role = "judge"
                existing.seat = "J"
                self._push(existing.name, "judge")
                self._emit("room:update", self.snapshot())
            return existing
        if cleaned in self.agents and self.agents[cleaned].role == role:
            agent = self.agents[cleaned]
            self._touch(agent)
            return agent
        now = self._clock()
        seat = SEATS[len([a for a in self.agents.values() if a.role == "agent"]) % len(SEATS)]
        agent = Agent(
            name=cleaned,
            model=model,
            role=role,
            seat=seat if role == "agent" else "J",
            joined_at=now,
            last_seen=now,
        )
        self.agents[cleaned] = agent
        self._emit("player:joined", {"name": cleaned, "role": role, "model": model})
        if role == "judge":
            self._push(cleaned, "judge")
        elif role == "agent" and self.phase == "debating" and cleaned not in self.order:
            self.order.append(cleaned)
        self._emit("room:update", self.snapshot())
        return agent

    def propose_topic(self, session_id: str, text: str) -> dict[str, Any]:
        human = self._voter(session_id)
        cleaned = " ".join(text.split())
        if not cleaned:
            raise FloorError("invalid", "the motion is empty")
        topic = Topic(id=secrets.token_hex(4), text=cleaned, by=human.name)
        self.topics.append(topic)
        for other in self.topics:
            other.votes.discard(human.session_id)
        topic.votes.add(human.session_id)
        if self.phase == "lobby":
            self._maybe_carry(topic)
        self._emit("room:update", self.snapshot())
        return self._topic_public(topic)

    def vote_topic(self, session_id: str, topic_id: str) -> dict[str, Any]:
        human = self._voter(session_id)
        topic = next((t for t in self.topics if t.id == topic_id), None)
        if not topic:
            raise FloorError("invalid", "unknown motion")
        for other in self.topics:
            other.votes.discard(human.session_id)
        topic.votes.add(human.session_id)
        if self.phase == "lobby":
            self._maybe_carry(topic)
        self._emit("room:update", self.snapshot())
        return self.snapshot()

    def send_message(self, name: str, text: str, *, notes: str = "", replied_to: str = "") -> dict[str, Any]:
        agent = self._agent(name)
        if self.phase != "debating":
            raise FloorError("unavailable", "the floor is not open")
        if self.speaker != agent.name:
            raise FloorError("not_your_turn", f"{self.speaker} has the floor")
        cleaned = text.strip()
        if not cleaned:
            raise FloorError("invalid", "empty statement")
        self._touch(agent)
        if self.turn_started is not None:
            agent.turnarounds.append(self._clock() - self.turn_started)
        self.lines.append(
            Line(
                id=self._next_line,
                ts=self._clock(),
                speaker=agent.name,
                role="agent",
                model=agent.model,
                text=cleaned,
                round=self.round,
                notes=notes,
                replied_to=replied_to,
            )
        )
        self._next_line += 1
        agent.turns += 1
        self.seq += 1
        nxt = self._advance()
        self._emit("chat:message", self.history()[-1])
        if nxt:
            self._push(nxt, "your_turn")
        self._emit("room:update", self.snapshot())
        return envelope_line(self.lines[-1])

    def heckle(self, session_id: str, text: str) -> None:
        human = self.require_human(session_id)
        cleaned = text.strip()
        if not cleaned:
            raise FloorError("invalid", "empty heckle")
        self.heckles.append({"who": human.name, "text": cleaned})
        self._emit("heckle", self.heckles[-1])

    def ask(self, session_id: str, text: str) -> None:
        self._voter(session_id)
        cleaned = text.strip()
        if not cleaned:
            raise FloorError("invalid", "empty question")
        self.pending_question = cleaned

    def call_it(self, session_id: str) -> dict[str, Any]:
        human = self._voter(session_id)
        if human.session_id in self.call_it_votes:
            self.call_it_votes.discard(human.session_id)
        else:
            self.call_it_votes.add(human.session_id)
        if self._majority(len(self.call_it_votes)):
            self._to_judge_vote()
        self._emit("room:update", self.snapshot())
        return self.snapshot()

    def close_now(self, session_id: str) -> dict[str, Any]:
        human = self.require_human(session_id)
        if not human.host:
            raise FloorError("forbidden", "only the host can close it now")
        self._to_judge_vote()
        self._emit("room:update", self.snapshot())
        return self.snapshot()

    def vote_round(self, session_id: str, choice: str) -> dict[str, Any]:
        if self.phase != "debating" or not self.round_hold:
            raise FloorError("unavailable", "the round is still open")
        picked = str(choice or "").strip().lower()
        if picked not in {"advance", "close"}:
            raise FloorError("invalid", "vote advance or close")
        human = self._voter(session_id)
        self.round_votes[human.session_id] = picked
        self._maybe_resolve_round()
        self._emit("room:update", self.snapshot())
        return self.snapshot()

    def vote_verbosity(self, session_id: str, choice: str) -> dict[str, Any]:
        if self.phase != "debating":
            raise FloorError("unavailable", "the floor is not open")
        picked = str(choice or "").strip().lower()
        if picked not in {"more", "less"}:
            raise FloorError("invalid", "vote more or less")
        human = self._voter(session_id)
        self.verbosity_votes[human.session_id] = picked
        self.seq += 1
        self._emit("room:update", self.snapshot())
        return self.snapshot()

    def vote_kick(self, session_id: str, name: str) -> dict[str, Any]:
        if self.phase != "debating":
            raise FloorError("unavailable", "the floor is not open")
        human = self._voter(session_id)
        agent = self._agent(_clean_agent_name(name))
        if agent.dropped or agent.role != "agent":
            raise FloorError("invalid", "that agent is not on the card")
        if self.kick_votes.get(human.session_id) == agent.name:
            self.kick_votes.pop(human.session_id, None)
        else:
            self.kick_votes[human.session_id] = agent.name
        n = sum(1 for voted in self.kick_votes.values() if voted == agent.name)
        if self._majority(n):
            self.kick_votes = {
                sid: voted for sid, voted in self.kick_votes.items() if voted != agent.name
            }
            self.drop(agent.name)
        self.seq += 1
        self._emit("room:update", self.snapshot())
        return self.snapshot()

    def configure_judge(self, model: str, label: str | None = None) -> None:
        self.judge_models[model] = label or model

    def vote_judge(self, session_id: str, model: str) -> dict[str, Any]:
        human = self._voter(session_id)
        if self.phase != "judge_vote":
            raise FloorError("unavailable", "the bench is not being chosen")
        option = next((j for j in self._judge_options() if j["model"] == model), None)
        if not option:
            raise FloorError("invalid", "unknown bench")
        if option["disabled"]:
            raise FloorError("ineligible", option.get("note") or "that bench cannot sit")
        self.judge_votes[human.session_id] = model
        counts: dict[str, int] = {}
        for voted in self.judge_votes.values():
            counts[voted] = counts.get(voted, 0) + 1
        leader, n = max(counts.items(), key=lambda kv: kv[1])
        if self._majority(n):
            sitting = next((a for a in self.agents.values() if a.model == leader and a.role == "agent"), None)
            if not sitting:
                raise FloorError("ineligible", "only a seated agent can sit")
            self.voted_judge = leader
            self.phase = "judging"
            self.seq += 1
            self.turn_started = self._clock()
            self._sitting_from_floor = sitting.name
            self._sitting_prior_seat = sitting.seat
            sitting.role = "judge"
            sitting.seat = "J"
            self._push(sitting.name, "judge")
        self._emit("room:update", self.snapshot())
        return self.snapshot()

    def submit_verdict(
        self,
        name: str,
        *,
        winner: str,
        runner_up: str,
        honorable: str,
        reason: str = "",
        runner_reason: str = "",
        honorable_reason: str = "",
        summary: str = "",
        highs: list[dict[str, Any]] | None = None,
        lows: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        agent = self._agent(name)
        self._touch(agent)
        if agent.role != "judge" or self.phase != "judging":
            raise FloorError("forbidden", "only the sitting bench can verdict")
        if self._was_debater(agent) and agent.name in {winner, runner_up, honorable}:
            raise FloorError("ineligible", "the bench cannot name itself")
        for who in (winner, runner_up, honorable):
            if who not in self.agents and who != winner:
                raise FloorError("invalid", f"unknown debater {who!r}")
        self.verdict = {
            "winner": winner,
            "runner_up": runner_up,
            "honorable": honorable,
            "reason": reason,
            "runner_reason": runner_reason,
            "honorable_reason": honorable_reason,
            "summary": summary,
            "highs": self._cites(highs),
            "lows": self._cites(lows),
            "judge": agent.name,
            "model": agent.model,
        }
        self.phase = "verdict"
        self.seq += 1
        for other in self.agents.values():
            if other.role == "agent":
                self._push(other.name, "ended")
        self._emit("verdict:ready", self.verdict)
        return self.snapshot()

    def history(self, *, omit_speaker: str | None = None) -> list[dict[str, Any]]:
        lines = [envelope_line(line) for line in self.lines]
        if omit_speaker:
            lines = [line for line in lines if line.get("speaker") != omit_speaker]
        return lines

    def peek_wake(self, name: str) -> dict[str, Any]:
        agent = self._agent(name)
        return agent.pending_wake or self._wake(name, "info")

    async def wait(self, name: str, timeout_s: float = 30.0) -> dict[str, Any]:
        agent = self._agent(name)
        self._touch(agent)
        if agent.pending_wake:
            wake = agent.pending_wake
            agent.pending_wake = None
            return wake
        wait = max(0.0, min(float(timeout_s), MAX_WAIT_S))
        agent.event.clear()
        agent.parked = True
        try:
            await asyncio.wait_for(agent.event.wait(), wait)
        except TimeoutError:
            return {
                "ok": True,
                "arrived": False,
                "kind": "info",
                "seq": self.seq,
                "phase": self.phase,
                "prompt": "",
                "history": self._history_for(agent),
            }
        finally:
            agent.parked = False
            self._touch(agent)
        wake = agent.pending_wake or self._wake(agent.name, "info")
        agent.pending_wake = None
        return wake

    def pull(self, name: str) -> dict[str, Any]:
        agent = self._agent(name)
        self._touch(agent)
        snap = self.snapshot()
        snap.pop("heckles", None)
        snap.update({"ok": True, "you": agent.name, "history": self._history_for(agent)})
        return snap

    def status(self, name: str | None = None) -> dict[str, Any]:
        out = {
            "ok": True,
            "phase": self.phase,
            "seq": self.seq,
            "topic": self.topic.text if self.topic else None,
            "speaker": self.speaker,
            "agents": [a.name for a in self.agents.values() if a.role == "agent"],
            "humans": [h.name for h in self.humans()],
        }
        if name and name in self.agents:
            out["you"] = name
        return out

    def mint_pair_code(self) -> str:
        code = f"{secrets.randbelow(10000):04d}"
        self._pair[code] = self._clock() + 240
        return code

    def claim_pair_code(self, code: str, name: str, model: str) -> Agent:
        expires = self._pair.get(code)
        if expires is None or expires < self._clock():
            raise FloorError("invalid", "pairing code is dead")
        del self._pair[code]
        return self.register_agent(name, model)

    def skip(self, name: str) -> None:
        if self.speaker == name:
            nxt = self._advance()
            if nxt:
                self._push(nxt, "your_turn")
            self._emit("room:update", self.snapshot())

    def drop(self, name: str) -> None:
        agent = self._agent(name)
        agent.dropped = True
        if self.speaker == name:
            self.skip(name)
        else:
            self._emit("room:update", self.snapshot())

    def rename_agent(self, session_id: str, name: str, to: str) -> dict[str, Any]:
        human = self.require_human(session_id)
        if not human.host:
            raise FloorError("forbidden", "only the host can rename an agent")
        old = _clean_agent_name(name)
        new = _clean_agent_name(to)
        agent = self._agent(old)
        if new == old:
            return self.snapshot()
        if new in self.agents:
            raise FloorError("invalid", f"{new!r} is already seated")
        self.agents.pop(old)
        agent.name = new
        self.agents[new] = agent
        self.order = [new if item == old else item for item in self.order]
        if self.speaker == old:
            self.speaker = new
        if self.opener == old:
            self.opener = new
        if self._sitting_from_floor == old:
            self._sitting_from_floor = new
        self.kick_votes = {sid: new if voted == old else voted for sid, voted in self.kick_votes.items()}
        for line in self.lines:
            if line.speaker == old:
                line.speaker = new
        if self.verdict:
            for key in ("winner", "runner_up", "honorable", "judge"):
                if self.verdict.get(key) == old:
                    self.verdict[key] = new
            for group in ("highs", "lows"):
                for cite in self.verdict.get(group) or []:
                    if cite.get("speaker") == old:
                        cite["speaker"] = new
        if agent.pending_wake and isinstance(agent.pending_wake.get("you"), dict):
            agent.pending_wake["you"]["name"] = new
        self.seq += 1
        self._emit("agent:renamed", {"from": old, "to": new})
        self._emit("room:update", self.snapshot())
        return self.snapshot()

    def reopen_bench(self) -> None:
        if self.phase != "judging":
            return
        name = self._sitting_from_floor
        if name and name in self.agents:
            agent = self.agents[name]
            agent.role = "agent"
            if self._sitting_prior_seat:
                agent.seat = self._sitting_prior_seat
            self._push(name, "info")
        self.voted_judge = None
        self.judge_votes = {}
        self.turn_started = None
        self._sitting_from_floor = None
        self._sitting_prior_seat = None
        self.phase = "judge_vote"
        self.seq += 1
        self._emit("room:update", self.snapshot())

    def check_timeouts(self) -> None:
        self._check_presence()
        if self.phase == "judging":
            if self.turn_started is not None and self._clock() - self.turn_started >= JUDGE_LIMIT_S:
                self.reopen_bench()
            return
        if self.phase != "debating" or not self.speaker or self.turn_started is None:
            return
        if self._clock() - self.turn_started >= TURN_LIMIT_S:
            self.skip(self.speaker)

    def snapshot(self) -> dict[str, Any]:
        now = self._clock()
        seated = [h for h in self.humans() if not h.watcher]
        agents = [self._agent_public(a, now) for a in self.agents.values() if a.role == "agent"]
        elapsed = 0
        if self.turn_started:
            elapsed = max(0, int(now - self.turn_started))
        return {
            "ok": True,
            "room_id": self.room_id,
            "phase": self.phase,
            "seq": self.seq,
            "human_count": len(seated),
            "agent_count": len([a for a in agents if a["status"] != "idle"]),
            "humans": [
                {**h.public(), "note": self._human_note(h, now)} for h in self.humans()
            ],
            "agents": agents,
            "topics": [self._topic_public(t) for t in self.topics],
            "topic": self._topic_public(self.topic) if self.topic else None,
            "motion": self.topic.text if self.topic else "",
            "speaker": self.speaker,
            "opener": self.opener,
            "round": self.round,
            "order": self._order_public(),
            "history": self.history(),
            "heckles": list(self.heckles),
            "call_it": len(self.call_it_votes),
            "call_it_names": [
                h.name for h in self.humans() if h.session_id in self.call_it_votes
            ],
            "round_hold": self.round_hold,
            "round_vote": self._choice_board(self.round_votes, ("advance", "close")),
            "verbosity": self.verbosity,
            "verbosity_vote": self._choice_board(self.verbosity_votes, ("more", "less")),
            "kick_votes": self._kick_public(),
            "judges": self._judge_options(),
            "verdict": self.verdict,
            "tunnel_url": self.tunnel_url,
            "tunnel_age": _rel(self.tunnel_started, now) if self.tunnel_started else None,
            "turn_elapsed_s": elapsed,
            "turn_limit_s": int(JUDGE_LIMIT_S if self.phase == "judging" else TURN_LIMIT_S),
            "pending_question": self.pending_question,
            "brief": JUDGE_BRIEF if self.phase in {"judge_vote", "judging", "verdict"} else None,
        }

    def _maybe_carry(self, topic: Topic) -> None:
        if not self._majority(len(topic.votes)):
            return
        ready = [a for a in self.agents.values() if a.role == "agent" and not a.dropped]
        if not ready:
            return
        pick = ready[self._rng(len(ready)) % len(ready)]
        self.topic = topic
        self.phase = "debating"
        self.round = 1
        self.round_hold = False
        self.round_votes = {}
        self.verbosity = ""
        self.verbosity_votes = {}
        self.kick_votes = {}
        self.call_it_votes = set()
        self.opener = pick.name
        self.order = [a.name for a in ready]
        start = self.order.index(pick.name)
        self.order = self.order[start:] + self.order[:start]
        self.speaker = pick.name
        self.turn_started = self._clock()
        self.seq += 1
        self._push(pick.name, "your_turn")

    def _advance(self) -> str | None:
        live = [n for n in self.order if n in self.agents and not self.agents[n].dropped]
        if not live:
            self.speaker = None
            self.turn_started = None
            return None
        if self.speaker not in live:
            self.speaker = live[0]
            self.turn_started = self._clock()
            return self.speaker
        idx = (live.index(self.speaker) + 1) % len(live)
        if idx == 0:
            self.round += 1
            self.speaker = None
            self.turn_started = None
            self.round_hold = True
            self.round_votes = {}
            return None
        self.speaker = live[idx]
        self.turn_started = self._clock()
        return self.speaker

    def _maybe_resolve_round(self) -> None:
        if not self.round_hold or self.phase != "debating":
            return
        seated = {h.session_id for h in self._seated()}
        if not seated:
            return
        cast = {sid: choice for sid, choice in self.round_votes.items() if sid in seated}
        if set(cast) < seated:
            return
        closes = sum(1 for choice in cast.values() if choice == "close")
        advances = sum(1 for choice in cast.values() if choice == "advance")
        if closes > advances:
            self.round_hold = False
            self._to_judge_vote()
            return
        self._resume_next_round()

    def _resume_next_round(self) -> None:
        self.verbosity = self._tally_verbosity()
        self.round_hold = False
        self.round_votes = {}
        live = [n for n in self.order if n in self.agents and not self.agents[n].dropped]
        if not live:
            self.speaker = None
            self.turn_started = None
            return
        self.speaker = live[0]
        self.turn_started = self._clock()
        self.seq += 1
        self._push(self.speaker, "your_turn")

    def _tally_verbosity(self) -> str:
        more = sum(1 for choice in self.verbosity_votes.values() if choice == "more")
        less = sum(1 for choice in self.verbosity_votes.values() if choice == "less")
        if more > less:
            return "more"
        if less > more:
            return "less"
        return self.verbosity

    def _to_judge_vote(self) -> None:
        if self.phase not in {"debating", "lobby"}:
            return
        self.phase = "judge_vote"
        self.speaker = None
        self.round_hold = False
        self.seq += 1
        for agent in self.agents.values():
            if agent.role == "agent":
                self._push(agent.name, "info")

    def _push(self, name: str, kind: str) -> None:
        agent = self._agent(name)
        agent.pending_wake = self._wake(name, kind)
        agent.last_push = self._clock()
        agent.event.set()
        self._emit("turn:update", {"speaker": self.speaker, "seq": self.seq, "phase": self.phase})

    def _wake(self, name: str, kind: str) -> dict[str, Any]:
        agent = self._agent(name)
        question = self.pending_question if kind == "your_turn" else None
        if kind == "your_turn":
            self.pending_question = None
            opponents = [a.name for a in self.agents.values() if a.role == "agent" and a.name != name]
            prompt = turn_prompt(
                motion=self.topic.text if self.topic else "",
                speaker=name,
                opponents=opponents,
                question=question,
                verbosity=self.verbosity,
            )
        elif kind == "judge":
            prompt = judge_prompt(
                motion=self.topic.text if self.topic else "",
                judge=name,
                recused=self._was_debater(agent),
            )
        else:
            prompt = ""
        return {
            "ok": True,
            "arrived": True,
            "kind": kind,
            "seq": self.seq,
            "phase": self.phase,
            "prompt": prompt,
            "history": self._history_for(agent),
            "you": {"name": agent.name, "model": agent.model, "role": agent.role, "seat": agent.seat},
        }

    def _voter(self, session_id: str) -> HumanSession:
        human = self.require_human(session_id)
        if human.watcher:
            raise FloorError("forbidden", "watchers do not vote")
        return human

    def _agent(self, name: str) -> Agent:
        agent = self.agents.get(name)
        if not agent:
            raise FloorError("not_registered", f"unknown agent {name!r}")
        return agent

    def _seated(self) -> list[HumanSession]:
        return [h for h in self.humans() if not h.watcher]

    def _majority(self, votes: int) -> bool:
        n = len(self._seated())
        if n == 0:
            return False
        return votes > n / 2

    def _choice_board(self, votes: dict[str, str], options: tuple[str, ...]) -> dict[str, Any]:
        seated = self._seated()
        seated_ids = {h.session_id for h in seated}
        counts = {opt: 0 for opt in options}
        names = {opt: [] for opt in options}
        choices: dict[str, str] = {}
        for sid, choice in votes.items():
            if sid not in seated_ids or choice not in counts:
                continue
            counts[choice] += 1
            choices[sid] = choice
            human = next((h for h in seated if h.session_id == sid), None)
            if human:
                names[choice].append(human.name)
        return {
            "counts": counts,
            "names": names,
            "choices": choices,
            "voters": list(choices),
            "voted": len(choices),
            "needed": len(seated),
        }

    def _kick_public(self) -> list[dict[str, Any]]:
        seated = self._seated()
        out = []
        for agent in self.agents.values():
            if agent.role != "agent" or agent.dropped:
                continue
            voters = [sid for sid, voted in self.kick_votes.items() if voted == agent.name]
            out.append(
                {
                    "name": agent.name,
                    "votes": len(voters),
                    "voters": voters,
                    "bar": _bar(len(voters), len(seated)),
                }
            )
        return out

    def _cites(self, raw: list[dict[str, Any]] | None) -> list[dict[str, str]]:
        by_id = {str(line.id): line for line in self.lines}
        out: list[dict[str, str]] = []
        if not isinstance(raw, list):
            return out
        for item in raw:
            if not isinstance(item, dict) or len(out) >= 3:
                continue
            sid = str(item.get("id") or "").strip()
            line = by_id.get(sid)
            quote = " ".join(str(item.get("quote") or "").split())
            if not line or not quote:
                continue
            out.append(
                {
                    "id": sid,
                    "quote": quote[:180],
                    "note": " ".join(str(item.get("note") or "").split())[:240],
                    "speaker": str(item.get("speaker") or line.speaker),
                }
            )
        return out

    def _history_for(self, agent: Agent) -> list[dict[str, Any]]:
        omit = agent.name if agent.role == "judge" and self._was_debater(agent) else None
        return self.history(omit_speaker=omit)

    def _was_debater(self, agent: Agent) -> bool:
        if agent.name in self.order:
            return True
        return any(line.speaker == agent.name for line in self.lines)

    def _judge_options(self) -> list[dict[str, Any]]:
        seated = [h for h in self.humans() if not h.watcher]
        spoken = {line.speaker for line in self.lines}
        seen: dict[str, dict[str, Any]] = {}
        for a in self.agents.values():
            if a.dropped or a.role not in {"agent", "judge"}:
                continue
            took_turn = a.name in spoken or a.name in self.order
            if took_turn:
                note = "Took a turn — speeches struck; cannot name itself"
            elif a.role == "judge":
                note = "Sitting bench"
            else:
                note = "Seated, no speech yet"
            seen[a.model] = {
                "model": a.model,
                "name": a.name,
                "note": note,
                "disabled": False,
                "votes": 0,
            }
        counts: dict[str, int] = {}
        voters: dict[str, list[str]] = {}
        for sid, model in self.judge_votes.items():
            counts[model] = counts.get(model, 0) + 1
            voters.setdefault(model, []).append(sid)
        out = []
        for item in seen.values():
            item["votes"] = counts.get(item["model"], 0)
            item["voters"] = voters.get(item["model"], [])
            item["mine"] = False
            item["bar"] = _bar(item["votes"], len(seated))
            out.append(item)
        return out

    def _topic_public(self, topic: Topic) -> dict[str, Any]:
        seated = [h for h in self.humans() if not h.watcher]
        return {
            "id": topic.id,
            "text": topic.text,
            "by": topic.by,
            "votes": len(topic.votes),
            "bar": _bar(len(topic.votes), len(seated)),
            "voters": list(topic.votes),
        }

    def _touch(self, agent: Agent) -> None:
        agent.last_seen = self._clock()
        agent.away = False

    def _live(self, agent: Agent, now: float | None = None) -> bool:
        now = self._clock() if now is None else now
        if agent.dropped:
            return False
        if agent.parked:
            return True
        if self.phase == "debating" and self.speaker == agent.name:
            return True
        if self.phase == "judging" and agent.role == "judge":
            return True
        return (now - agent.last_seen) < STALE_S

    def _check_presence(self) -> None:
        now = self._clock()
        became_away: list[str] = []
        became_live = False
        for agent in self.agents.values():
            live = self._live(agent, now)
            was_away = agent.away
            agent.away = not live
            if agent.away and not was_away:
                became_away.append(agent.name)
            elif live and was_away:
                became_live = True
        if not became_away and not became_live:
            return
        if self.phase == "judging" and self._sitting_from_floor in became_away:
            self.reopen_bench()
            return
        if self.phase == "debating" and self.speaker in became_away:
            self._skip_until_live()
        self._emit("room:update", self.snapshot())

    def _skip_until_live(self) -> None:
        if self.phase != "debating" or self.round_hold or not self.speaker:
            return
        start = self.speaker
        for _ in range(len(self.order) + 1):
            agent = self.agents.get(self.speaker)
            if agent and self._live(agent) and not agent.dropped:
                if self.speaker != start:
                    self._push(self.speaker, "your_turn")
                return
            nxt = self._advance()
            if not nxt:
                return

    def _agent_public(self, agent: Agent, now: float) -> dict[str, Any]:
        if agent.dropped:
            status = "idle"
        elif not self._live(agent, now):
            status = "away"
        elif self.phase == "debating" and self.speaker == agent.name:
            status = "floor"
        elif self.phase == "lobby" and agent.turns == 0:
            status = "ready"
        else:
            status = "ready"
        labels = {
            "floor": "On the floor",
            "ready": "Waiting",
            "idle": "Not seated",
            "away": "Away",
        }
        tags = {
            "floor": "tag tag-accent",
            "ready": "tag tag-neutral",
            "idle": "tag tag-outline",
            "away": "tag tag-outline",
        }
        seats = {
            "floor": "seat now",
            "ready": "seat",
            "idle": "seat idle",
            "away": "seat idle",
        }
        avg = "—"
        if agent.turnarounds:
            mean = sum(agent.turnarounds) / len(agent.turnarounds)
            avg = f"{int(mean // 60)}m {int(mean % 60):02d}s avg"
        return {
            "name": agent.name,
            "model": agent.model,
            "seat": agent.seat,
            "joined": _rel(agent.joined_at, now),
            "turns": agent.turns,
            "status": status,
            "statusLabel": labels[status],
            "connected": self._live(agent, now),
            "tagClass": tags[status],
            "seatClass": seats[status],
            "lastPush": _rel(agent.last_push, now) if agent.last_push else "—",
            "turnaround": avg,
        }

    def _order_public(self) -> list[dict[str, str]]:
        out = []
        if not self.order or self.phase != "debating":
            return out
        if self.round_hold:
            for name in self.order:
                if name not in self.agents or self.agents[name].dropped:
                    continue
                out.append(
                    {
                        "name": name,
                        "state": "spoke",
                        "flowClass": "flow done",
                        "seatClass": "seat spoken",
                    }
                )
            return out
        hit_speaker = False
        for name in self.order:
            if name not in self.agents or self.agents[name].dropped:
                continue
            if name == self.speaker:
                state, flow, seat = "writing now", "flow now", "seat now"
                hit_speaker = True
            elif not hit_speaker:
                state, flow, seat = "spoke", "flow done", "seat spoken"
            else:
                state, flow, seat = "up next", "flow", "seat"
            out.append({"name": name, "state": state, "flowClass": flow, "seatClass": seat})
        return out

    def _human_note(self, human: HumanSession, now: float) -> str:
        if human.host:
            return "you · host" if human.connected else "host · away"
        if human.watcher:
            return "watching"
        return "seated"

    def _emit(self, event: str, payload: dict[str, Any]) -> None:
        for fn in list(self._listeners):
            fn(event, payload)


def envelope_line(line: Line) -> dict[str, Any]:
    return {
        "id": line.id,
        "ts": _iso(line.ts),
        "speaker": line.speaker,
        "role": line.role,
        "model": line.model,
        "text": line.text,
        "round": line.round,
        "notes": line.notes,
        "replied_to": line.replied_to,
        "at": datetime.fromtimestamp(line.ts, tz=timezone.utc).strftime("%H:%M"),
    }


def _bar(votes: int, total: int) -> str:
    pct = 0 if total <= 0 else round(100 * votes / total)
    return f"width:{pct}%"


_WORDS = (
    "ORCHID", "WALNUT", "COPPER", "LINEN", "QUILL", "CEDAR", "MARBLE",
    "VIOLET", "AMBER", "PEWTER", "IVORY", "MAPLE",
)


def _mint_room_id() -> str:
    return f"{secrets.choice(_WORDS)}-{secrets.randbelow(10000):04d}"
