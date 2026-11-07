"""The eval harness (BE-6.2, plan 003 §2): T0 Elisa and T2 held-out, end to end.

Everything runs through the real interfaces against an **isolated** Neo4j (a throwaway
container unless `--neo4j-uri` is given), never the demo graph:
- capture: messages go in as Slack events through `SlackIngest` (email through the email
  connector), the capture loop triages and extracts with the real models, and the graph is
  built by the pipeline alone (gold is never loaded: the extracted-only condition);
- QA: the ops agent answers over the MCP server, with the question's `as_of` as its now.

Results land in `eval/results/<timestamp>.json` plus a Markdown table. No LLM key: every
LLM-dependent row is `skipped`, never 0%.
"""

from __future__ import annotations

import contextlib
import itertools
import socket
import threading
import time
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, SecretStr

from kaiku.agents.ops_agent import OpsAgent, OpsAnswer
from kaiku.config import Settings
from kaiku.context.models import AssertionView
from kaiku.domain.time import datetime_to_slack_ts
from kaiku.eval.score import (
    Check,
    HeldoutThread,
    ItemResult,
    SetResult,
    field_accuracy,
    load_heldout,
    match,
    scoped,
    score_answer,
    score_assertion,
    score_relations,
)
from kaiku.extraction.graph import CaptureResult, Extractor
from kaiku.extraction.llm import LlmJudge
from kaiku.graph import schema
from kaiku.graph.driver import create_driver
from kaiku.graph.repository import ContextRepository
from kaiku.ingest.backfill import elisa_example
from kaiku.ingest.capture import CaptureLoop
from kaiku.ingest.email import email_event
from kaiku.ingest.slack_events import SlackIngest
from kaiku.ingest.store import CaptureStore, MessageIn
from kaiku.llm.embeddings import FeatherlessEmbedder, embed_query_or_none
from kaiku.logging import get_logger
from kaiku.main import create_app
from kaiku.seed.profiles import Profile
from kaiku.seed.service import SeedRequest, seed
from kaiku.telemetry.store import TelemetryStore

log = get_logger(__name__)
NEO4J_IMAGE = "neo4j:2026.10.1-community"  # same as docker-compose.yml
SNAPSHOT = datetime(2026, 11, 7, tzinfo=UTC)  # eval/heldout: "status at snapshot"
ELISA_MSG8 = datetime(2025, 10, 14, 6, 22, 8, tzinfo=UTC)  # R8: 09:22 EEST, seconds synthetic
ST30_QUESTION = "TRN-017 port 1/1/2: up, optics fine, zero traffic after an SFP swap. What next?"
FAR = datetime(2100, 1, 1, tzinfo=UTC)  # the sweeper's clock: every stored thread is due


class EvalReport(BaseModel):
    started_at: datetime
    finished_at: datetime | None = None
    models: dict[str, str | None] = {}
    graph: str = "extracted-only (gold never loaded)"
    sets: list[SetResult] = []
    caveat: str = (
        "T0 is Elisa's own example. T2 is hand-written and held out, n = 9 threads / "
        "12 questions; one run, so no variance is reported."
    )


@contextlib.contextmanager
def _neo4j(uri: str | None, password: str | None) -> Iterator[tuple[str, str]]:
    if uri is not None:
        yield uri, password or ""
        return
    from testcontainers.community.neo4j import Neo4jContainer  # dev dependency

    container = Neo4jContainer(NEO4J_IMAGE, password="eval-only-pw")  # noqa: S106  # throwaway container
    container.start()
    try:
        yield container.get_connection_url(), "eval-only-pw"
    finally:
        container.stop()


@contextlib.contextmanager
def _serve(app: Any) -> Iterator[str]:
    """The API (and its MCP server) on a free local port, in a thread."""
    import uvicorn

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 90
    while True:
        with contextlib.suppress(httpx.HTTPError):
            if server.started and httpx.get(f"{url}/readyz", timeout=5).status_code == 200:
                break
        if time.monotonic() > deadline:
            raise RuntimeError("the API never became ready")
        time.sleep(0.2)
    try:
        yield url
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def _slack_events(
    channel: str, messages: Sequence[tuple[datetime, str, str]]
) -> list[dict[str, Any]]:
    """(time, user, text) as Slack message events in one thread."""
    root = datetime_to_slack_ts(messages[0][0])
    out = []
    for at, user, text in messages:
        ts = datetime_to_slack_ts(at)
        ev: dict[str, Any] = {"type": "message", "channel": channel, "user": user, "ts": ts}
        if ts != root:
            ev["thread_ts"] = root
        out.append({**ev, "text": text})
    return out


class Harness:
    def __init__(self, settings: Settings, elisa_dir: Path) -> None:
        self.settings = settings
        self.elisa_dir = elisa_dir
        self.driver = create_driver(settings)
        self.repo = ContextRepository(self.driver, settings.neo4j_database)
        self.store = CaptureStore(self.driver, settings.neo4j_database)
        embedder = FeatherlessEmbedder.from_settings(settings)

        async def embed(text: str) -> list[float] | None:
            return await embed_query_or_none(embedder, text)

        extractor = Extractor(
            self.repo, LlmJudge(settings), TelemetryStore(settings.data_dir), embed
        )
        self.loop = CaptureLoop(self.store, extractor, None, idle=timedelta(0), clock=lambda: FAR)
        self.ingest = SlackIngest(self.store, workspace_url="https://eval.slack.example")
        self.agent: OpsAgent | None = None

    async def capture(self, messages: Sequence[MessageIn | dict[str, Any]]) -> CaptureResult:
        for m in messages:
            if isinstance(m, MessageIn):
                await self.store.store([m], datetime.now(UTC))
            else:
                await self.ingest.on_message(m)
        results = await self.loop.sweep_once()
        if len(results) != 1:
            raise RuntimeError(f"expected one thread to triage, got {len(results)}")
        return results[0]

    async def views(self, ids: Sequence[str]) -> list[AssertionView]:
        return await self.repo.assertions(list(ids))

    async def ask(self, qid: str, question: str, now: datetime) -> OpsAnswer:
        assert self.agent is not None
        return await self.agent.ask(question, request_id=f"eval-{qid}", now=now)

    # --- T0 ---------------------------------------------------------------------------

    async def t0(self) -> tuple[SetResult, str | None]:
        items: list[ItemResult] = []
        msgs = elisa_example(self.elisa_dir)
        chat = [m for m in msgs if m.thread_id == "elisa:teams_chat"]
        email = next(m for m in msgs if m.thread_id == "elisa:email")
        increasing = all(a.event_time < b.event_time for a, b in itertools.pairwise(chat))
        items.append(
            ItemResult.of(
                "ST-21",
                [
                    Check(name="strictly_increasing", passed=increasing),
                    Check(
                        name="message_8_time",
                        passed=chat[7].event_time == ELISA_MSG8,
                        detail=str(chat[7].event_time),
                    ),
                ],
            )
        )
        events = _slack_events(
            "C-NOC-TRANSPORT", [(m.event_time, m.author or "", m.text) for m in chat[:9]]
        )
        res = await self.capture(events)
        ids = res.assertion_ids
        items.append(
            ItemResult.of(
                "ST-01",
                [Check(name="one_assertion", passed=len(ids) == 1, detail=f"{ids} {res.reason}")],
            )
        )
        if len(ids) != 1:
            return SetResult(name="T0 Elisa", items=items), None
        rule_id = ids[0]
        [v] = await self.views([rule_id])
        q = v.qualifiers
        items.append(
            ItemResult.of(
                "ST-10",
                [
                    Check(
                        name="observed_on",
                        passed="TRN-042" in scoped(v, "observed_on"),
                        detail=str(sorted(scoped(v, "observed_on"))),
                    ),
                    Check(
                        name="applies_to",
                        passed={"TNC-1X", "Gen1"} <= scoped(v, "applies_to"),
                        detail=str(sorted(scoped(v, "applies_to"))),
                    ),
                    Check(
                        name="activity",
                        passed="sfp_replacement" in scoped(v, "activity"),
                        detail=str(sorted(scoped(v, "activity"))),
                    ),
                    Check(
                        name="action",
                        passed="bounce_interface" in str(q.get("action")),
                        detail=str(q.get("action")),
                    ),
                    Check(
                        name="avoid",
                        passed={"replace_sfp", "reboot_node"} <= set(q.get("avoid") or []),
                        detail=str(q.get("avoid")),
                    ),
                ],
            )
        )
        items.append(
            ItemResult.of(
                "ST-20",
                [
                    Check(
                        name="recorded_at",
                        passed=v.recorded_at == ELISA_MSG8,
                        detail=str(v.recorded_at),
                    ),
                    Check(
                        name="valid_from",
                        passed=v.valid_from == ELISA_MSG8,
                        detail=str(v.valid_from),
                    ),
                    Check(name="valid_to", passed=v.valid_to is None, detail=str(v.valid_to)),
                ],
            )
        )
        before = set((await self.views([rule_id]))[0].corroborates)
        mail = await self.capture([email])
        [after] = await self.views([rule_id])
        new_rules = [i for i in mail.assertion_ids if i not in after.corroborates]
        items.append(
            ItemResult.of(
                "ST-02",
                [
                    Check(name="no_new_rule", passed=not new_rules, detail=str(mail.assertion_ids)),
                    Check(
                        name="corroborates",
                        passed=len(set(after.corroborates) - before) >= 1
                        or any(c.source_type == "email" for c in after.citations),
                        detail=str(after.corroborates),
                    ),
                ],
            )
        )
        if self.agent is not None:
            ans = await self.ask("ST-30", ST30_QUESTION, SNAPSHOT)
            words = " ".join(s.action for s in ans.steps).casefold()
            cited = {s.assertion_id for s in ans.sources}
            items.append(
                ItemResult.of(
                    "ST-30",
                    [
                        Check(
                            name="action",
                            passed=any(
                                w in words for w in ("bounce", "disable", "re-enable", "shut")
                            ),
                            detail=words[:120],
                        ),
                        Check(
                            name="avoid_sfp",
                            passed="sfp" in " ".join(ans.avoid).casefold(),
                            detail=str(ans.avoid),
                        ),
                        Check(
                            name="avoid_reboot",
                            passed="reboot" in " ".join(ans.avoid).casefold(),
                            detail=str(ans.avoid),
                        ),
                        Check(name="citation", passed=rule_id in cited, detail=str(sorted(cited))),
                    ],
                )
            )
        else:
            items.append(ItemResult(id="ST-30", passed=False, skipped=True, note="no agent"))
        return SetResult(name="T0 Elisa", items=items), rule_id

    # --- T2 ---------------------------------------------------------------------------

    def _heldout_messages(self, t: HeldoutThread) -> list[MessageIn | dict[str, Any]]:
        if t.channel == "email":
            m = t.messages[0]
            return [
                email_event(
                    message_id=f"<{t.thread_id}@heldout.example>",
                    sent_at=datetime.fromisoformat(m.ts.replace("Z", "+00:00")),
                    subject=m.subject or "",
                    body=m.text,
                    sender=m.user,
                )
            ]
        channel = "C-" + t.channel_name.lstrip("#").upper()
        rows = [
            (datetime.fromisoformat(m.ts.replace("Z", "+00:00")), m.user, m.text)
            for m in t.messages
        ]
        return list(_slack_events(channel, rows))

    async def t2(self, folder: Path, elisa_rule: str | None) -> list[SetResult]:
        threads, expected, questions = load_heldout(folder)
        by_id = {e.thread_id: e for e in expected}
        cite: dict[str, set[str]] = {}
        if elisa_rule:
            cite["Elisa TRN-042 Teams message 8"] = {elisa_rule}
            cite["Elisa TRN-042 email"] = {elisa_rule}
        items: list[ItemResult] = []
        for t in sorted(threads, key=lambda t: t.messages[0].ts):
            exp = by_id[t.thread_id]
            res = await self.capture(self._heldout_messages(t))
            views = await self.views(res.assertion_ids)
            cite[t.thread_id] = set(res.assertion_ids)
            for v in views:
                cite[t.thread_id] |= set(v.corroborates)
            checks: list[Check] = []
            if not exp.expected_assertions and not exp.expected_relations:
                checks.append(
                    Check(
                        name="nothing_captured",
                        passed=not res.assertion_ids,
                        detail=f"{res.status}: {res.reason}",
                    )
                )
            elif not exp.expected_assertions:  # H-01: corroborate, never duplicate
                dup = [v.assertion_id for v in views if elisa_rule not in v.corroborates]
                checks.append(Check(name="no_duplicate", passed=not dup, detail=str(dup)))
            matched = {}
            for ea in exp.expected_assertions:
                view = match(ea, views)
                matched[ea.key] = view
                checks += [
                    c.model_copy(update={"detail": f"{ea.key}: {c.detail}"})
                    for c in score_assertion(ea, view, SNAPSHOT)
                ]
            checks += score_relations(exp.expected_assertions, matched)
            items.append(ItemResult.of(t.thread_id, checks, note=res.reason[:200]))
        capture = SetResult(name="T2 held-out capture", items=items)
        capture.field_accuracy = field_accuracy(items)

        qa_items: list[ItemResult] = []
        for q in questions:
            if self.agent is None:
                qa_items.append(ItemResult(id=q.id, passed=False, skipped=True, note="no agent"))
                continue
            now = datetime.fromisoformat(q.as_of.replace("Z", "+00:00"))
            try:
                ans = await self.ask(q.id, q.ask, now)
            except Exception as exc:  # noqa: BLE001  # one failed run is a failed item, not a crashed eval
                qa_items.append(
                    ItemResult(id=q.id, passed=False, note=f"{type(exc).__name__}: {exc}"[:200])
                )
                continue
            qa_items.append(
                ItemResult.of(
                    q.id, score_answer(q, ans, cite), note=f"{ans.status}: {ans.summary}"[:200]
                )
            )
        qa = SetResult(name="T2 held-out QA", items=qa_items)
        qa.field_accuracy = field_accuracy(qa_items)
        return [capture, qa]


def _has_llm(settings: Settings) -> bool:
    return settings.featherless_api_key is not None or settings.anthropic_api_key is not None


async def run_eval(
    base: Settings,
    *,
    sets: set[str],
    elisa_dir: Path,
    heldout_dir: Path,
    neo4j_uri: str | None = None,
    neo4j_password: str | None = None,
) -> EvalReport:
    report = EvalReport(
        started_at=datetime.now(UTC),
        models={"chat": base.chat_model, "fast": base.fast_model,
                "embedding": base.embedding_model},
    )  # fmt: skip
    if not _has_llm(base):
        report.sets = [
            SetResult(name=n, items=[ItemResult(id="all", passed=False, skipped=True,
                      note="no LLM key: skipped, not 0%")])
            for n in sorted(sets)
        ]  # fmt: skip
        report.finished_at = datetime.now(UTC)
        return report
    with _neo4j(neo4j_uri, neo4j_password) as (uri, password):
        settings = base.model_copy(
            update={"neo4j_uri": uri, "neo4j_password": SecretStr(password), "sim_url": None}
        )
        await schema.apply_migrations(settings)
        seeded = await seed(settings, SeedRequest(network=True, profile=Profile.DEMO))
        log.info("eval_seeded", violations=seeded.violations)
        h = Harness(settings, elisa_dir)
        try:
            with _serve(create_app(settings)) as url:
                h.agent = OpsAgent(settings.model_copy(update={"mcp_url": f"{url}/mcp"}))
                t0, rule = await h.t0()
                report.sets.append(t0)
                if "heldout" in sets:
                    report.sets += await h.t2(heldout_dir, rule)
        finally:
            await h.driver.close()
    report.finished_at = datetime.now(UTC)
    return report


def to_markdown(report: EvalReport) -> str:
    lines = [
        f"# Eval {report.started_at:%Y-%m-%d %H:%M} UTC",
        "",
        f"Graph: {report.graph}. Models: "
        + ", ".join(f"{k}={v}" for k, v in report.models.items())
        + ".",
        "",
        f"_{report.caveat}_",
        "",
        "| Set | Passed | Field accuracy |",
        "|---|---|---|",
    ]
    for s in report.sets:
        acc = ", ".join(f"{k} {v}" for k, v in s.field_accuracy.items()) or "—"
        lines.append(f"| {s.name} | {s.passed}/{s.scored} | {acc} |")
    for s in report.sets:
        lines += ["", f"## {s.name}", "", "| Item | Result | Failed checks |", "|---|---|---|"]
        for i in s.items:
            result = "skipped" if i.skipped else ("pass" if i.passed else "FAIL")
            failed = "; ".join(f"{c.name} ({c.detail})" for c in i.checks if not c.passed)
            lines.append(f"| {i.id} | {result} | {(failed or i.note).replace('|', '/')[:300]} |")
    return "\n".join(lines) + "\n"


def write(report: EvalReport, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = report.started_at.strftime("%Y%m%dT%H%M%SZ")
    js, md = out_dir / f"{stem}.json", out_dir / f"{stem}.md"
    js.write_text(report.model_dump_json(indent=2), "utf-8")
    md.write_text(to_markdown(report), "utf-8")
    return js, md
