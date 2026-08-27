"""Copy the mockup briefs. Agents see these; heckles never do."""

FLOOR_BRIEF = (
    "You are on the floor of a debate against other AI agents. The motion is "
    "above. Other agents are arguing the same floor and only one of you will "
    "be judged the winner — argue to win it. You will be handed the full "
    "transcript as JSON before each of your turns. Write quickly: each turn "
    "has a hard two-minute timeout, then the room forfeits you and moves on. "
    "Send one message when you are ready. Keep that "
    "message under 200 words, in short paragraphs. You may mark *italics* "
    "and **bold**; the room will set the type. A few key sources are "
    "acceptable when they earn the point, not required if the argument "
    "stands on its own and can be followed. A dump of links or quotations "
    "does not help."
)

JUDGE_BRIEF = (
    "You are judging a debate you did not take part in. The full transcript "
    "follows as JSON. Name a winner, a runner-up and one honorable mention, "
    "and give a short reason for each. Also give up to three high points and "
    "three low points: each is a short note plus a quickfire quote from a "
    "speech, using that speech's id so the room can link back to it. Weigh "
    "the argument, not the prose — reward the debater who answered what was "
    "put to them."
)


def turn_prompt(
    *,
    motion: str,
    speaker: str,
    opponents: list[str],
    question: str | None,
    verbosity: str = "",
) -> str:
    names = ", ".join(opponents) if opponents else "no one else yet"
    text = (
        f"{speaker}, you have the floor.\n\n"
        f"The motion: {motion}\n\n"
        f"{FLOOR_BRIEF}\n\n"
        f"The other agents on this floor: {names}."
    )
    if verbosity == "more":
        text += (
            "\n\nThis round the room voted more verbose: you may take up to "
            "400 words and develop the argument in short paragraphs."
        )
    elif verbosity == "less":
        text += (
            "\n\nThis round the room voted less verbose: keep this speech "
            "under 120 words."
        )
    if question:
        text += (
            "\n\nA human put this question to you; it must be addressed:\n"
            f"{question}"
        )
    return text


def judge_prompt(*, motion: str, judge: str, recused: bool = False) -> str:
    brief = JUDGE_BRIEF
    if recused:
        brief = (
            f"You took a turn on this floor as {judge}. Those speeches are struck "
            "from the transcript you are given. Name a winner, a runner-up and one "
            "honorable mention from the remaining debaters only — you cannot name "
            "yourself. Give up to three high points and three low points, each with "
            "a short note and a quote tied to a speech id. Weigh the argument, not "
            "the prose — reward the debater who answered what was put to them."
        )
    return (
        f"{judge}, you are the bench.\n\n"
        f"The motion: {motion}\n\n"
        f"{brief}"
    )
