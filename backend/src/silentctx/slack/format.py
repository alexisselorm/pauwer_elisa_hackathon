"""Render an OpsAnswer as one Slack message (mrkdwn), short enough to read on a phone."""

from __future__ import annotations

from silentctx.agents.ops_agent import AnswerStatus, OpsAnswer

_CONDITION_ICON = {"met": ":white_check_mark:", "not_met": ":x:", "unknown": ":grey_question:"}
_STATUS_HEADER = {
    AnswerStatus.ANSWERED: "",
    AnswerStatus.PARTIAL: ":warning: *Partly documented*\n",
    AnswerStatus.DISPUTED: ":warning: *Experts disagree*\n",
    AnswerStatus.NOT_DOCUMENTED: ":mag: *Not documented*\n",
    AnswerStatus.NEEDS_CLARIFICATION: "",
}
SLACK_TEXT_LIMIT = 3900  # chat.postMessage truncates around 4k characters


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
            lines.append(f"• {s.assertion_id}: {', '.join(bits)}")
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
