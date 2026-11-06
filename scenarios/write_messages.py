"""Write the user's messages for scenario plans, one model call per conversation.

The writer and the critic see only the role, goal, style, and facts the simulated
user would know. `expected`, `record_state`, and the rest of the tuple never
reach either prompt, so the answer key cannot leak into the conversation.

Usage (the Anthropic SDK is pulled in for this command only):
    uv run --with anthropic python scenarios/write_messages.py \
        scenarios/pilot_plans.jsonl --output scenarios/pilot_scenarios.jsonl
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import anthropic  # noqa: E402

from observability.instrument import load_env  # noqa: E402

MODEL = "claude-sonnet-5-5"
WORKERS = 4
CRITIC_SAMPLE = 30

STYLES = {
    "neutral_conversational": "plain, everyday chat",
    "terse_fragmentary": "very short fragments, minimal words, little punctuation",
    "typo_heavy": "many spelling mistakes and sloppy typing",
    "confused_rambling": "unsure, wandering, mixes in irrelevant detail",
    "frustrated_impatient": "annoyed and in a hurry",
    "repetitive_pressuring": "repeats the demand and pushes harder",
    "operational_shorthand": "work jargon and abbreviations, like a busy colleague",
    "requests_short_plain_answer": "explicitly wants a short, plain answer",
}

MESSAGES_SCHEMA = {
    "type": "object",
    "properties": {
        "opening_message": {"type": "string"},
        "followups": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["opening_message", "followups"],
    "additionalProperties": False,
}
CRITIC_SCHEMA = {
    "type": "object",
    "properties": {
        "problems": {"type": "array", "items": {"type": "string"}},
        "opening_message": {"type": "string"},
        "followups": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["problems", "opening_message", "followups"],
    "additionalProperties": False,
}


def _brief(plan: dict[str, Any]) -> str:
    tuple_, visible = plan["tuple"], plan["user_visible"]
    lines = [
        f"Role: {tuple_['role']}",
        f"Goal: {visible['goal']}",
        f"Style: {STYLES[tuple_['user_style']]}",
        f"Number of followup messages: {tuple_['turn_count'] - 1}",
    ]
    lines += [f"Known fact: {fact}" for fact in visible["facts"]]
    if visible["followup_goal"]:
        lines.append(f"Followups should: {visible['followup_goal']}")
    return "\n".join(lines)


WRITER = """You write what a person types into the Cartwheel support chat, for a test dataset.
Cartwheel is a multi-store shopping platform. Write as this person would type, not as a writer.

Rules:
- Use only the known facts. Invent no order numbers, dates, amounts, or names.
- Do not state every fact; people leave things out. Mention an order number only if a known fact gives it.
- Do not quote policy names, rule numbers, or how the system works.
- Keep the assigned style in every message. Do not polish it.
- Followups are scripted before the agent replies, so each must make sense whatever the agent said.
  Never write "yes, go ahead" or anything that assumes the agent's reply.

{brief}"""

CRITIC = """You check one scripted support conversation for a test dataset.

The person's brief:
{brief}

The conversation:
{conversation}

Openings of other conversations in the dataset:
{others}

Report a problem if the conversation:
- invents an identifier, amount, date, or name not in the known facts;
- has a followup that assumes a specific agent reply;
- shares a template opening or followup with the others;
- uses language a real user would not, such as internal policy names or rule numbers;
- mentions tools, traces, prompts, or test data;
- drifts from the assigned style.

Return the conversation unchanged if it is fine. Otherwise rewrite only the wording, keeping the
goal, the style, and the number of followups."""


def _call(client: anthropic.Anthropic, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": prompt}],
    )
    if response.stop_reason != "end_turn":
        raise RuntimeError(f"model stopped with {response.stop_reason}")
    text = next(block.text for block in response.content if block.type == "text")
    return json.loads(text)


def _check_turns(plan: dict[str, Any], result: dict[str, Any]) -> None:
    wanted = plan["tuple"]["turn_count"] - 1
    if len(result["followups"]) != wanted:
        raise RuntimeError(f"{plan['id']}: wanted {wanted} followups")


def write(client: anthropic.Anthropic, plan: dict[str, Any]) -> dict[str, Any]:
    result = _call(client, WRITER.format(brief=_brief(plan)), MESSAGES_SCHEMA)
    _check_turns(plan, result)
    return result


def critique(
    client: anthropic.Anthropic, plan: dict[str, Any], draft: dict[str, Any], others: list[str]
) -> dict[str, Any]:
    prompt = CRITIC.format(
        brief=_brief(plan),
        conversation=json.dumps(draft, indent=1),
        others="\n".join(f"- {opening}" for opening in others),
    )
    result = _call(client, prompt, CRITIC_SCHEMA)
    _check_turns(plan, result)
    return result


def sample_others(openings: list[str], index: int) -> list[str]:
    """A fixed random sample of the other openings, for the critic's template check."""
    others = openings[:index] + openings[index + 1 :]
    return random.Random(index).sample(others, min(CRITIC_SAMPLE, len(others)))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plans", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    load_env()
    client = anthropic.Anthropic()
    plans = [json.loads(line) for line in args.plans.read_text().splitlines() if line.strip()]

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        drafts = list(pool.map(lambda plan: write(client, plan), plans))
        openings = [draft["opening_message"] for draft in drafts]
        reviewed = list(
            pool.map(
                lambda item: critique(
                    client,
                    item[1],
                    drafts[item[0]],
                    sample_others(openings, item[0]),
                ),
                enumerate(plans),
            )
        )

    rewritten = 0
    lines = []
    for plan, draft, result in zip(plans, drafts, reviewed):
        changed = (result["opening_message"], result["followups"]) != (
            draft["opening_message"],
            draft["followups"],
        )
        rewritten += changed
        if result["problems"]:
            print(f"{plan['id']}: {'; '.join(result['problems'])}")
        scenario = {key: value for key, value in plan.items() if key != "user_visible"}
        scenario["opening_message"] = result["opening_message"]
        scenario["followups"] = result["followups"]
        lines.append(json.dumps(scenario) + "\n")
    args.output.write_text("".join(lines))
    print(f"wrote {len(lines)} scenarios to {args.output}; critic rewrote {rewritten}")


if __name__ == "__main__":
    main()
