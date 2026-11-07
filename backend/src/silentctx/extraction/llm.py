"""The LLM judge: triage (FAST_MODEL), extraction and the V6 relation (CHAT_MODEL), all with
structured output. A timeout or invalid output is retried once, then raises
`ExtractionError` and the thread is parked as `extraction_failed` (BE-3.1 off-nominal).
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel

from silentctx.config import Settings
from silentctx.extraction.models import (
    Extraction,
    Relation,
    Relations,
    RuleDraft,
    Triage,
    TriageKind,
)
from silentctx.ingest.store import ThreadView
from silentctx.llm.chat import ModelRole, get_chat_model
from silentctx.logging import get_logger

log = get_logger(__name__)

TIMEOUT_S = 90.0

TRIAGE_PROMPT = """You read conversations between telecom network operations people and decide
whether a thread holds operational knowledge worth keeping for the next engineer.

KEEP (relevant=true):
- kind=rule: symptom -> action rules ("if counters stay at zero with the link up, bounce
  the port"); "avoid X" warnings; hardware model, generation or firmware quirks; workarounds
- kind=access: site access (keys, codes, gates, barriers, roads, who to call). Keep it even
  when it is about one site only
- kind=correction: advice that corrects, contradicts or retires earlier advice
  ("actually...", "not any more", "since the upgrade")
IGNORE (relevant=false, kind=none):
- greetings, small talk, lunch, holidays; scheduling and logistics
- status pings that teach nothing ("on my way", "ticket opened")
- live incident updates with no reusable rule
- an unanswered question is not knowledge: relevant=false, kind=none, and start the reason
  with "gap candidate:" followed by the question

Answer with relevant, kind and a one-line reason."""

EXTRACT_PROMPT = """Extract the reusable operational knowledge from this thread as rules.
Usually there is one rule; return none if there is no answer that teaches something.

Rules for every field:
- Every quote is copied EXACTLY from one message (same words, no paraphrase). No quote, no field.
- The action is what the answering expert said to do. Never take it from a question.
  A suggestion that was rejected ("So another SFP?" -> "No") goes into avoid.
- value is a short snake_case token. Use these where they fit:
  symptoms: oper_up, rx_power_normal, counters_zero, alarms_cleared
  actions and avoid: bounce_interface, replace_sfp, reboot_node, power_cycle_radio,
  export_config, get_key, get_code
- observed: symptoms someone reported seeing, quoted from where they said it.
- entities: every node, model, site, place or generation named ("TRN-042", "older units",
  "the Kemi gate"), with role observed_on (where it happened), applies_to (which models or
  generations it holds for) or activity (the work being done, e.g. "Changed the SFP").
- valid_from / valid_to: when the KNOWLEDGE starts or stops holding, only as an explicit
  calendar date or weekday ("from 1 December", "until 20 November", "the code lock was
  installed 3 August"), with tense past or future. Never the age or install year of the
  equipment ("installed around 2019" is about the unit, not the rule). Otherwise null.
- topic: reuse one of the known topics when it fits: {topics}"""

RELATE_PROMPT = """A new piece of operational knowledge was captured. For each earlier
assertion, say how the new one relates to it:
- duplicate: the same knowledge
- corroborates: agrees with it, from another source or angle
- supersedes: something changed in the world (new lock, new firmware), so the new one replaces
  it from now on. Use this even when the author calls it a "correction" of their earlier advice
  because of a change.
- corrects: the earlier statement misread the same conversation, or a detail of it was wrong
  from the start (a wrong port, a typo)
- contradicts: they disagree and neither replaces the other
- unrelated"""


RESOLVED_PROMPT = """You see how a telecom operations chat thread started and one later reply.
resolved=true only if the thread start reports a network fault or problem AND the reply says
that problem is now fixed or working again (e.g. "counters moving again", "link passes
traffic now"). If the start reports no fault, the answer is false whatever the reply says.
Plans, questions, thanks for advice not yet tried, and small talk are false."""


class Resolved(BaseModel):
    resolved: bool


class ExtractionError(RuntimeError):
    """The LLM failed twice (timeout or invalid output)."""


def _thread_text(thread: ThreadView) -> str:
    return "\n\n".join(
        f"[{i}] {m.event_time:%Y-%m-%d %H:%M:%S}Z {m.author or 'unknown'}"
        f"{f' ({m.author_role})' if m.author_role else ''}:\n{m.text}"
        for i, m in enumerate(thread.messages, 1)
    )


def _unwrap(schema: type[BaseModel], out: Any) -> Any:
    """Kimi sometimes answers `{"rules": [...]}` as a bare `[...]` or `[{"rules": [...]}]`.
    For a schema that is one list field, take either shape instead of paying for a retry."""
    if not isinstance(out, list):
        return out
    fields = list(schema.model_fields)
    if len(out) == 1 and isinstance(out[0], dict) and set(out[0]) <= set(fields):
        return out[0]
    return {fields[0]: out} if len(fields) == 1 else out


class LlmJudge:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._fast: BaseChatModel | None = None
        self._chat: BaseChatModel | None = None

    def _model(self, role: ModelRole) -> BaseChatModel:
        if role is ModelRole.FAST:
            self._fast = self._fast or get_chat_model(self._settings, ModelRole.FAST)
            return self._fast
        self._chat = self._chat or get_chat_model(self._settings, ModelRole.CHAT)
        return self._chat

    async def _call[T: BaseModel](
        self, role: ModelRole, schema: type[T], system: str, user: str
    ) -> T:
        runnable = self._model(role).with_structured_output(schema)
        last: Exception | None = None
        for attempt in (1, 2):
            start = time.monotonic()
            try:
                out = await asyncio.wait_for(
                    runnable.ainvoke([("system", system), ("user", user)]), TIMEOUT_S
                )
                result = schema.model_validate(_unwrap(schema, out))
            except Exception as exc:  # noqa: BLE001  # timeout, provider error, bad JSON: retry
                last = exc
                log.warning(
                    "llm_call_failed", schema=schema.__name__, attempt=attempt, error=repr(exc)
                )
                continue
            log.info(
                "llm_call",
                schema=schema.__name__,
                role=role.value,
                attempt=attempt,
                seconds=round(time.monotonic() - start, 2),
            )
            return result
        raise ExtractionError(f"{schema.__name__}: {type(last).__name__}: {last}") from last

    async def resolves(self, reply: str, thread_start: str) -> bool:
        """The trigger's model check (BE-2.4) for replies the word list doesn't catch."""
        user = f"Thread start:\n{thread_start}\n\nReply:\n{reply}"
        out = await self._call(ModelRole.FAST, Resolved, RESOLVED_PROMPT, user)
        return out.resolved

    async def triage(self, thread: ThreadView) -> Triage:
        return await self._call(ModelRole.FAST, Triage, TRIAGE_PROMPT, _thread_text(thread))

    async def extract(self, thread: ThreadView, kind: TriageKind, topics: list[str]) -> Extraction:
        system = EXTRACT_PROMPT.format(topics=", ".join(sorted(topics)) or "(none yet)")
        user = f"Kind of knowledge: {kind}\n\n{_thread_text(thread)}"
        return await self._call(ModelRole.CHAT, Extraction, system, user)

    async def relate(self, draft: RuleDraft, candidates: list[dict[str, Any]]) -> list[Relation]:
        if not candidates:
            return []
        earlier = [
            {
                "assertion_id": c["assertion_id"],
                "statement": c.get("statement"),
                "author": c.get("author"),
                "source": c.get("platform"),
                "recorded_at": str(c.get("recorded_at")),
            }
            for c in candidates
        ]
        user = (
            f"New: {draft.statement}\n"
            f"Action: {draft.action.value if draft.action else None}; "
            f"avoid: {[a.value for a in draft.avoid]}\n\n"
            f"Earlier:\n{json.dumps(earlier, indent=1)}"
        )
        out = await self._call(ModelRole.CHAT, Relations, RELATE_PROMPT, user)
        return out.relations
