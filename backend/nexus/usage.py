"""Unified token-usage collection for every provider that reports usage.

The rule this module exists to enforce: **a number is only ever recorded as
billed usage when the provider actually reported it.** DeepSeek's own token
documentation says the same thing — an image token count computed from pixel
dimensions is "an estimate only ... refer to the usage returned by the API as
the source of truth".

So every stored row carries ``usage_source``:

``provider-reported``
    The response contained a usage object. These tokens are real.
``partial``
    The response reported some counters but not all; missing ones stay NULL
    rather than being filled with a guess.
``unavailable``
    No usage was reported (a streaming response without a final usage chunk, an
    error response, a non-OpenAI transport). Token columns stay NULL and the
    row only contributes to call/error counts.

Aggregated "billed" totals count ``provider-reported`` and ``partial`` rows;
``unavailable`` rows are reported separately so the console can show an honest
empty state instead of a fabricated zero or a rounded-up guess.

Streaming is handled by :class:`StreamUsageReader`, which collects SSE deltas
and captures a final usage chunk *when the provider sends one*. Whether a
provider does so is a capability that must be observed, never assumed —
:data:`STREAMING_USAGE_VERIFIED` starts empty.

Duplicate protection is a UNIQUE ``dedupe_key``. A retried request that carries
the same provider request id (or the same caller-supplied idempotency key) can
only ever create one row, so a retry never inflates the bill.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .storage import now

# Provider id -> observed fact that a streaming response carried final usage.
# Empty until a real streaming request proves it for that route.
STREAMING_USAGE_VERIFIED: set[str] = set()

SOURCE_REPORTED = "provider-reported"
SOURCE_PARTIAL = "partial"
SOURCE_UNAVAILABLE = "unavailable"

STATUS_OK = "ok"
STATUS_ERROR = "error"

# Counter names observed in the documented responses this project integrates:
#   * OpenAI-compatible: prompt_tokens / completion_tokens / total_tokens
#   * DeepSeek: prompt_cache_hit_tokens and prompt_cache_miss_tokens
#   * Z.AI (GLM) and newer OpenAI schemas: prompt_tokens_details.cached_tokens
#   * reasoning: completion_tokens_details.reasoning_tokens
# The first alias that yields a valid integer wins.
_COUNTER_ALIASES = {
    "input_tokens": ("prompt_tokens", "input_tokens"),
    "output_tokens": ("completion_tokens", "output_tokens"),
    "total_tokens": ("total_tokens",),
    "cached_tokens": ("prompt_tokens_details.cached_tokens", "cached_tokens",
                      "prompt_cache_hit_tokens", "cache_read_input_tokens", 'cached_input_tokens'),
    "reasoning_tokens": ("completion_tokens_details.reasoning_tokens", "reasoning_tokens"),
}


def _counter_number(value) -> int | None:
    """Accept only a genuine non-negative integer token count.

    ``bool`` is excluded deliberately: ``True`` is an ``int`` in Python and
    would silently become a token count of 1.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    return None


def _dig(source: dict, path: str):
    node = source
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


@dataclass
class Usage:
    """Canonical usage for one model request."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cached_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_source: str = "unavailable"      # provider | derived | unavailable
    source: str = SOURCE_UNAVAILABLE
    credit: float | None = None
    reported_model: str | None = None
    request_id: str | None = None

    @property
    def has_any(self) -> bool:
        return any(value is not None for value in
                   (self.input_tokens, self.output_tokens, self.total_tokens, self.cached_tokens,
                    self.reasoning_tokens))

    def payload(self) -> dict:
        return {key: value for key, value in {
            "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens, "cached_tokens": self.cached_tokens,
            "reasoning_tokens": self.reasoning_tokens, "credit": self.credit,
            "reported_model": self.reported_model, "request_id": self.request_id,
            "usage_source": self.source, "total_source": self.total_source,
        }.items() if value is not None}


def parse_usage(payload) -> Usage:
    """Extract usage from a documented OpenAI-compatible response body.

    Returns ``source="unavailable"`` when the provider reported nothing, which
    is the honest result — not an empty-but-successful usage record.
    """
    if not isinstance(payload, dict):
        return Usage()
    counters: dict[str, int | None] = {}
    for target, aliases in _COUNTER_ALIASES.items():
        for alias in aliases:
            value = _counter_number(_dig(payload, alias))
            if value is not None:
                counters[target] = value
                break
        else:
            counters[target] = None
    # ``total_tokens`` is only trusted when the provider sent it. Otherwise it is
    # derived from the two counters that were sent, and that derivation is
    # recorded as such.
    total_source = "provider" if counters.get("total_tokens") is not None else "unavailable"
    if total_source == "unavailable" and counters.get("input_tokens") is not None and counters.get("output_tokens") is not None:
        counters["total_tokens"] = counters["input_tokens"] + counters["output_tokens"]
        total_source = "derived"
    credit = payload.get("credit")
    if isinstance(credit, bool) or not isinstance(credit, (int, float)) or credit < 0:
        credit = None
    model_id = payload.get("model")
    usage = Usage(
        input_tokens=counters.get("input_tokens"), output_tokens=counters.get("output_tokens"),
        total_tokens=counters.get("total_tokens"), cached_tokens=counters.get("cached_tokens"),
        reasoning_tokens=counters.get("reasoning_tokens"), total_source=total_source,
        credit=float(credit) if credit is not None else None,
        reported_model=model_id if isinstance(model_id, str) and len(model_id) <= 200 else None,
    )
    usage.source = SOURCE_REPORTED if usage.has_any else SOURCE_UNAVAILABLE
    return usage


def merge_usage(base: Usage, extra: Usage) -> Usage:
    """Combine two observations of the same request, keeping the first real value.

    Used by the streaming reader: intermediate chunks may report partial
    counters and the final chunk carries the authoritative object.
    """
    for name in ("input_tokens", "output_tokens", "total_tokens", "cached_tokens",
                 "reasoning_tokens", "credit", "reported_model", "request_id"):
        value = getattr(extra, name)
        if value is not None:
            setattr(base, name, value)
            if name == "total_tokens":
                base.total_source = extra.total_source if extra.total_source != "unavailable" else base.total_source
    base.source = SOURCE_REPORTED if base.has_any else SOURCE_UNAVAILABLE
    return base


@dataclass
class StreamUsageReader:
    """Incremental SSE reader that records usage only when the stream sends it.

    The reader never estimates. ``usage_available`` stays ``False`` for a
    provider that streams content without a final usage object, and the caller
    then stores the request as ``unavailable`` rather than inventing counts.
    """

    usage: Usage = field(default_factory=Usage)
    chunks: int = 0
    content_bytes: int = 0
    finish_reason: str | None = None
    _buffer: str = ""

    def feed(self, chunk: bytes | str) -> None:
        text = chunk.decode("utf-8", "replace") if isinstance(chunk, bytes) else chunk
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._line(line.strip())

    def _line(self, line: str) -> None:
        if not line or line.startswith(":") or not line.startswith("data:"):
            return
        data = line[5:].strip()
        if not data or data == "[DONE]":
            return
        try:
            document = json.loads(data)
        except ValueError:
            return
        if not isinstance(document, dict):
            return
        self.chunks += 1
        self.usage = merge_usage(self.usage, parse_usage(document.get("usage")))
        if isinstance(document.get("model"), str) and self.usage.reported_model is None:
            self.usage.reported_model = document["model"]
        if isinstance(document.get("id"), str) and self.usage.request_id is None:
            self.usage.request_id = document["id"]
        choices = document.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            reason = choices[0].get("finish_reason")
            if isinstance(reason, str):
                self.finish_reason = reason

    @property
    def usage_available(self) -> bool:
        return self.usage.source != SOURCE_UNAVAILABLE


@dataclass(frozen=True)
class UsageEvent:
    """One recorded request. ``dedupe_key`` makes retries idempotent."""

    agent_id: str
    provider_id: str
    model_key: str | None = None
    task_id: str | None = None
    attempt_id: str | None = None
    purpose: str = "task"
    usage: Usage = field(default_factory=Usage)
    streaming: bool = False
    status: str = STATUS_OK
    error_kind: str | None = None
    latency_ms: int | None = None
    idempotency_key: str | None = None
    requested_model: str | None = None


_SAFE_ID = re.compile(r"[^A-Za-z0-9._:@|+-]")


def dedupe_key(event: UsageEvent) -> str:
    """Stable identity of one logical request, for retry/duplicate suppression.

    Prefers the provider's own request id. When the provider sends none, the
    caller must supply an idempotency key; without either, the key falls back to
    the attempt id so a single attempt still cannot double-record, while a
    genuinely distinct retry (new attempt) is counted as its own request.
    """
    parts = [event.provider_id, event.agent_id, event.model_key or "-"]
    identity = event.usage.request_id or event.idempotency_key or event.attempt_id or "anonymous"
    parts.append(identity)
    return _SAFE_ID.sub("_", "|".join(parts))[:400]


def _periods(span: str, start: str | None, end: str | None, reference: datetime | None = None):
    """Resolve a query span into an inclusive-exclusive UTC window."""
    moment = reference or datetime.now(timezone.utc)
    if span == "custom":
        begin = datetime.fromisoformat(start) if start else moment - timedelta(days=30)
        finish = datetime.fromisoformat(end) if end else moment
        if begin.tzinfo is None:
            begin = begin.replace(tzinfo=timezone.utc)
        if finish.tzinfo is None:
            finish = finish.replace(tzinfo=timezone.utc)
        return begin, finish
    day = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    if span == "today":
        return day, day + timedelta(days=1)
    if span == "week":
        begin = day - timedelta(days=day.weekday())
        return begin, begin + timedelta(days=7)
    if span == "month":
        begin = day.replace(day=1)
        return begin, (begin + timedelta(days=32)).replace(day=1)
    if span == "all":
        return datetime(1970, 1, 1, tzinfo=timezone.utc), moment + timedelta(days=1)
    raise ValueError(f"未知时间范围：{span}")


class UsageCollector:
    """SQLite-backed usage store with task / Agent / Provider / global roll-ups."""

    def __init__(self, store, estimator=None):
        self.store = store
        # ``estimator(provider_id, model_key, usage, created_at) -> dict``
        # supplies labelled cost estimates. Absent means costs stay unestimated.
        self.estimator = estimator

    # ------------------------------------------------------------- recording

    def record(self, event: UsageEvent) -> tuple[int, bool]:
        """Persist one request. Returns ``(row_id, duplicate)``.

        A repeated event never adds tokens twice: the unique key makes the
        insert a no-op and only the outcome fields are refreshed.
        """
        key = dedupe_key(event)
        usage = event.usage
        estimate = {}
        if self.estimator is not None:
            try:
                estimate = self.estimator(event.provider_id, event.model_key, usage,
                                          now()) or {}
            except Exception:
                estimate = {}
        stamp = now()
        with self.store.connect() as db:
            existing = db.execute("SELECT id FROM usage_records WHERE dedupe_key=?", (key,)).fetchone()
            if existing:
                db.execute(
                    "UPDATE usage_records SET status=?, error_kind=?, latency_ms=COALESCE(?, latency_ms),"
                    " finished_at=? WHERE id=?",
                    (event.status, event.error_kind, event.latency_ms, stamp, existing["id"]))
                return existing["id"], True
            cursor = db.execute(
                "INSERT INTO usage_records(dedupe_key,task_id,attempt_id,agent_id,provider_id,model_key,"
                "requested_model,reported_model,request_id,input_tokens,output_tokens,total_tokens,"
                "cached_tokens,reasoning_tokens,total_source,usage_source,streaming,status,error_kind,"
                "latency_ms,credit,currency,estimated_cost,cost_source,price_revision,purpose,"
                "created_at,finished_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (key, event.task_id, event.attempt_id, event.agent_id, event.provider_id, event.model_key,
                 (event.requested_model or event.model_key), usage.reported_model, usage.request_id,
                 usage.input_tokens, usage.output_tokens, usage.total_tokens, usage.cached_tokens,
                 usage.reasoning_tokens, usage.total_source, usage.source, int(bool(event.streaming)),
                 event.status, event.error_kind, event.latency_ms, usage.credit,
                 estimate.get("currency"), estimate.get("estimated_cost"),
                 estimate.get("cost_source", "not-estimable"), estimate.get("price_revision"),
                 event.purpose, stamp, stamp))
            return cursor.lastrowid, False

    def mark_streaming_usage_verified(self, provider_id: str) -> None:
        STREAMING_USAGE_VERIFIED.add(provider_id)

    # -------------------------------------------------------------- querying

    def _window(self, span: str, start: str | None, end: str | None):
        begin, finish = _periods(span, start, end)
        return begin.isoformat(), finish.isoformat()

    def rows(self, span: str = "month", start: str | None = None, end: str | None = None,
             agent_id: str | None = None, provider_id: str | None = None,
             model_key: str | None = None, task_id: str | None = None, limit: int = 500, agent_ids=()) -> list[dict]:
        begin, finish = self._window(span, start, end)
        clause = ["created_at >= ?", "created_at < ?"]
        params: list = [begin, finish]
        if agent_ids:
            clause.append('agent_id IN (' + ','.join('?' for _ in agent_ids) + ')')
            params.extend(agent_ids)
        for column, value in (("agent_id", agent_id), ("provider_id", provider_id),
                              ("model_key", model_key), ("task_id", task_id)):
            if value:
                clause.append(f"{column} = ?")
                params.append(value)
        params.append(max(1, min(int(limit), 5000)))
        with self.store.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM usage_records WHERE " + " AND ".join(clause) +
                " ORDER BY id DESC LIMIT ?", params)]

    def totals(self, span: str = "month", start: str | None = None, end: str | None = None,
               **filters) -> dict:
        """Global roll-up. Billed tokens and unavailable calls stay separate."""
        begin, finish = self._window(span, start, end)
        clause = ["created_at >= ?", "created_at < ?"]
        params: list = [begin, finish]
        if filters.get('agent_ids'):
            ids = filters['agent_ids']
            clause.append('agent_id IN (' + ','.join('?' for _ in ids) + ')')
            params.extend(ids)
        for column in ("agent_id", "provider_id", "model_key", "task_id"):
            if filters.get(column):
                clause.append(f"{column} = ?")
                params.append(filters[column])
        where = " AND ".join(clause)
        with self.store.connect() as db:
            row = db.execute(
                "SELECT COUNT(*) AS requests,"
                " SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) AS succeeded,"
                " SUM(CASE WHEN status<>'ok' THEN 1 ELSE 0 END) AS failed,"
                " SUM(CASE WHEN usage_source<>'unavailable' THEN 1 ELSE 0 END) AS usage_reported,"
                " SUM(CASE WHEN usage_source='unavailable' THEN 1 ELSE 0 END) AS usage_unavailable,"
                " SUM(COALESCE(input_tokens,0)) AS input_tokens,"
                " SUM(COALESCE(output_tokens,0)) AS output_tokens,"
                " SUM(COALESCE(total_tokens,0)) AS total_tokens,"
                " SUM(COALESCE(cached_tokens,0)) AS cached_tokens,"
                " SUM(COALESCE(reasoning_tokens,0)) AS reasoning_tokens,"
                " SUM(COALESCE(credit,0)) AS credit,"
                " SUM(CASE WHEN estimated_cost IS NULL THEN 1 ELSE 0 END) AS unpriced_requests,"
                " SUM(COALESCE(estimated_cost,0)) AS estimated_cost"
                f" FROM usage_records WHERE {where}", params).fetchone()
            reported_models = [r[0] for r in db.execute(
                f"SELECT DISTINCT COALESCE(model_key, requested_model, '未标注') FROM usage_records WHERE {where}",
                params)]
        result = {key: (row[key] or 0) for key in row.keys()}
        result["requests"] = row["requests"] or 0
        result["reported_models"] = sorted(reported_models)
        result["currency"] = self._currency(filters.get("provider_id"))
        if result['unpriced_requests']:
            result['currency'] = None
        result["cost_source"] = "nexus-estimate"
        return result

    def _currency(self, provider_id: str | None) -> str | None:
        with self.store.connect() as db:
            if provider_id:
                row = db.execute("SELECT currency FROM usage_records WHERE currency IS NOT NULL AND provider_id=?"
                                 " ORDER BY id DESC LIMIT 1", (provider_id,)).fetchone()
            else:
                row = db.execute("SELECT currency FROM usage_records WHERE currency IS NOT NULL"
                                 " ORDER BY id DESC LIMIT 1").fetchone()
        return row["currency"] if row else None

    def group_by(self, dimension: str, span: str = "month", start: str | None = None,
                 end: str | None = None, agent_ids=()) -> list[dict]:
        """Roll-up per Agent, Provider or model — the four-level statistics."""
        columns = {"agent": "agent_id", "provider": "provider_id",
                   "model": "COALESCE(model_key, requested_model, '未标注')", "task": "task_id"}
        if dimension not in columns:
            raise ValueError(f"未知统计维度：{dimension}")
        column = columns[dimension]
        begin, finish = self._window(span, start, end)
        agent_clause = (' AND agent_id IN (' + ','.join('?' for _ in agent_ids) + ')') if agent_ids else ''
        with self.store.connect() as db:
            rows = db.execute(
                f"SELECT {column} AS name, COUNT(*) AS requests,"
                " SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) AS succeeded,"
                " SUM(CASE WHEN status<>'ok' THEN 1 ELSE 0 END) AS failed,"
                " SUM(CASE WHEN usage_source<>'unavailable' THEN 1 ELSE 0 END) AS usage_reported,"
                " SUM(CASE WHEN usage_source='unavailable' THEN 1 ELSE 0 END) AS usage_unavailable,"
                " SUM(COALESCE(input_tokens,0)) AS input_tokens,"
                " SUM(COALESCE(output_tokens,0)) AS output_tokens,"
                " SUM(COALESCE(total_tokens,0)) AS total_tokens,"
                " SUM(COALESCE(cached_tokens,0)) AS cached_tokens,"
                " SUM(COALESCE(reasoning_tokens,0)) AS reasoning_tokens,"
                " SUM(COALESCE(credit,0)) AS credit,"
                " SUM(COALESCE(estimated_cost,0)) AS estimated_cost,"
                " MAX(created_at) AS last_used_at"
                f" FROM usage_records WHERE created_at>=? AND created_at<?{agent_clause} GROUP BY {column}"
                " ORDER BY total_tokens DESC, requests DESC", (begin, finish, *agent_ids)).fetchall()
        return [dict(row) for row in rows]

    def series(self, span: str = "month", start: str | None = None, end: str | None = None,
               bucket: str = "day", agent_ids=()) -> list[dict]:
        """Token-consumption trend, bucketed by UTC day or hour."""
        fmt = "%Y-%m-%d" if bucket == "day" else "%Y-%m-%dT%H:00"
        begin, finish = self._window(span, start, end)
        agent_clause = (' AND agent_id IN (' + ','.join('?' for _ in agent_ids) + ')') if agent_ids else ''
        with self.store.connect() as db:
            rows = db.execute(
                f"SELECT strftime('{fmt}', created_at) AS bucket, COUNT(*) AS requests,"
                " SUM(COALESCE(total_tokens,0)) AS total_tokens,"
                " SUM(COALESCE(input_tokens,0)) AS input_tokens,"
                " SUM(COALESCE(output_tokens,0)) AS output_tokens,"
                " SUM(CASE WHEN usage_source='unavailable' THEN 1 ELSE 0 END) AS usage_unavailable"
                f" FROM usage_records WHERE created_at>=? AND created_at<?{agent_clause} GROUP BY bucket ORDER BY bucket",
                (begin, finish, *agent_ids)).fetchall()
        return [dict(row) for row in rows]

    def task_costs(self, span: str = "month", start: str | None = None, end: str | None = None,
                   limit: int = 50) -> list[dict]:
        begin, finish = self._window(span, start, end)
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT u.task_id, t.title, COUNT(*) AS requests,"
                " SUM(COALESCE(u.total_tokens,0)) AS total_tokens,"
                " SUM(COALESCE(u.estimated_cost,0)) AS estimated_cost,"
                " GROUP_CONCAT(DISTINCT u.agent_id) AS agents,"
                " GROUP_CONCAT(DISTINCT COALESCE(u.model_key,u.requested_model)) AS models"
                " FROM usage_records u LEFT JOIN tasks t ON t.id=u.task_id"
                " WHERE u.created_at>=? AND u.created_at<? AND u.task_id IS NOT NULL"
                " GROUP BY u.task_id ORDER BY total_tokens DESC LIMIT ?",
                (begin, finish, max(1, min(limit, 500)))).fetchall()
        return [dict(row) for row in rows]

    def counters(self, span: str = "month", start: str | None = None, end: str | None = None) -> dict:
        """Provider/Agent call counts used by the budget guard."""
        begin, finish = self._window(span, start, end)
        with self.store.connect() as db:
            providers = {row[0]: row[1] for row in db.execute(
                "SELECT provider_id, COUNT(*) FROM usage_records WHERE created_at>=? AND created_at<?"
                " GROUP BY provider_id", (begin, finish))}
            agents = {row[0]: row[1] for row in db.execute(
                "SELECT agent_id, COUNT(*) FROM usage_records WHERE created_at>=? AND created_at<?"
                " GROUP BY agent_id", (begin, finish))}
        return {"providers": providers, "agents": agents}

    def overview(self, span: str = "month", start: str | None = None, end: str | None = None) -> dict:
        """Everything the 『用量与额度』 page needs, in one payload."""
        return {
            "range": {"span": span, "start": start, "end": end,
                      "resolved": self._window(span, start, end)},
            "totals": self.totals(span, start, end),
            "by_agent": self.group_by("agent", span, start, end),
            "by_provider": self.group_by("provider", span, start, end),
            "by_model": self.group_by("model", span, start, end),
            "series": self.series(span, start, end),
            "task_costs": self.task_costs(span, start, end),
            "counters": self.counters(span, start, end),
            "streaming_usage_verified": sorted(STREAMING_USAGE_VERIFIED),
            "note": ("Token 数据只来自服务商真实返回的 usage 字段。"
                     "usage_source=unavailable 的请求只计入调用次数，不计入 Token 与费用。"
                     "estimated_cost 为 Nexus 依据价目表推算的估算值，不是服务商账单。"),
        }
