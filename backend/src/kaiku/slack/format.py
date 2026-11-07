"""Render an OpsAnswer as one Slack message (mrkdwn), short enough to read on a phone."""

from __future__ import annotations

import json
from typing import Any

from kaiku.agents.ops_agent import AnswerStatus, OpsAnswer

_CONDITION_ICON = {"met": ":white_check_mark:", "not_met": ":x:", "unknown": ":grey_question:"}
_STATUS_HEADER = {
    AnswerStatus.ANSWERED: "",
    AnswerStatus.PARTIAL: ":warning: *Partly documented*\n",
    AnswerStatus.DISPUTED: ":warning: *Experts disagree*\n",
    AnswerStatus.NOT_DOCUMENTED: ":mag: *Not documented*\n",
    AnswerStatus.NEEDS_CLARIFICATION: "",
}
SLACK_TEXT_LIMIT = 3900  # chat.postMessage truncates around 4k characters


def _escape(text: str) -> str:
    """Slack reads <, > and & as markup (an email Message-ID is <...@...>)."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _pretty(token: str) -> str:
    return token.replace("_", " ")


def format_answer(answer: OpsAnswer) -> str:
    lines = [_STATUS_HEADER.get(answer.status, "") + answer.summary.strip()]
    if answer.steps:
        lines.append("")
        lines.append("*Next step*")
        for n, step in enumerate(answer.steps, 1):
            lines.append(f"{n}. {step.action}  _({', '.join(step.assertion_ids)})_")
    if answer.avoid:
        lines.append("*Avoid:* " + "; ".join(_pretty(a) for a in answer.avoid))
    if answer.conditions:
        lines.append("")
        lines.append("*Conditions*")
        for c in answer.conditions:
            icon = _CONDITION_ICON.get(c.status, ":grey_question:")
            detail = f" — {c.detail}" if c.detail else ""
            lines.append(f"{icon} {_pretty(c.name)}{detail}")
    if answer.sources:
        lines.append("")
        lines.append("*Why (sources)*")
        for s in answer.sources[:6]:
            bits = [
                b for b in (s.who, _pretty(s.where) if s.where else None, (s.when or "")[:10]) if b
            ]
            label = (
                f"<{s.link}|{s.assertion_id}>"
                if (s.link or "").startswith("https://")
                else s.assertion_id
            )
            ref = f" · `{_escape(s.ref)}`" if s.ref else ""
            lines.append(f"• {label}: {', '.join(bits)}{ref}")
            if s.detail:
                lines.append(f"    ↳ _{_escape(' '.join(s.detail.split())[:200])}_")
    if answer.records:
        lines.append("")
        lines.append("*From the network record:* " + ", ".join(answer.records[:6]))
    if answer.gap_id:
        lines.append("")
        lines.append(
            f":memo: I logged this as knowledge gap {answer.gap_id} so an expert can fill it in."
        )
    if answer.review_notes:
        lines.append("")
        lines.append("_" + " · ".join(answer.review_notes[:4]) + "_")
    text = "\n".join(lines)
    return text if len(text) <= SLACK_TEXT_LIMIT else text[: SLACK_TEXT_LIMIT - 1] + "…"


SECTION_LIMIT = 2900  # a section block's text is capped at 3000 characters


def text_blocks(text: str) -> list[dict[str, Any]]:
    """mrkdwn sections, split on line breaks to stay under the per-block limit."""
    blocks: list[dict[str, Any]] = []
    chunk: list[str] = []
    for line in text.split("\n"):
        if chunk and len("\n".join([*chunk, line])) > SECTION_LIMIT:
            blocks.append(_section("\n".join(chunk)))
            chunk = []
        chunk.append(line[:SECTION_LIMIT])
    if chunk:
        blocks.append(_section("\n".join(chunk)))
    return blocks


def _section(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text or " "}}


def button(
    text: str, action_id: str, value: dict[str, Any], style: str | None = None
) -> dict[str, Any]:
    b: dict[str, Any] = {
        "type": "button",
        "text": {"type": "plain_text", "text": text, "emoji": True},
        "action_id": action_id,
        "value": json.dumps(value, separators=(",", ":")),
    }
    if style:
        b["style"] = style
    return b


def actions(block_id: str, *buttons: dict[str, Any]) -> dict[str, Any]:
    return {"type": "actions", "block_id": block_id, "elements": list(buttons)}


def context(text: str) -> dict[str, Any]:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def replace_actions(blocks: list[dict[str, Any]], block_id: str, note: str) -> list[dict[str, Any]]:
    """The message's blocks with one actions block swapped for a context line, so a
    button is clicked once and the thread shows what happened."""
    out = [b for b in blocks if b.get("block_id") != block_id]
    return [*out, context(note)]
