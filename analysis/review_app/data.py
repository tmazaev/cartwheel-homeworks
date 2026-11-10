"""Trace loading and conversation assembly for the HW4 review app.

Cartwheel writes one Langfuse trace per user turn. This module loads the
support-* scenario traces (live from Langfuse, or from the Module 1 export as
an offline fallback), groups them by ``cartwheel.session_id`` and turns each
trace into a display-ready turn:

    user message -> [thinking / interim text / tool call+result ...] -> reply

Tool arguments and results come from the TOOL spans (clean JSON), not from
the Python-repr copy inside the next generation's input. The assistant's
thinking and interim text are recovered from the last generation's input
history, because Cartwheel's generation spans do not record their output.

Everything here is read-only: it never writes to Langfuse.
"""

from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from analysis.helpers import langfuse_io, selection
from analysis.helpers.normalization import normalize_trace

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_EXPORT = REPO_ROOT / "traces" / "support_traces.json"
SCENARIO_PREFIX = "support-"

# Tools that change state. Their containers get a distinct border and their
# result status is shown prominently.
WRITE_TOOLS = {"issue_refund", "cancel_order", "escalate_to_human"}
# Tools that retrieve policy/help text (the "retrieval results").
RETRIEVAL_TOOLS = {"search_help_center", "get_policy"}


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------


def load_traces(source: str, export_path: Path | None = None) -> list[dict[str, Any]]:
    """Return normalized support-* traces from ``source`` ("langfuse"|"export")."""
    if source == "langfuse":
        if not langfuse_io.is_configured():
            raise RuntimeError(
                "LANGFUSE_* is not configured; start with --source export to use "
                "the offline Module 1 export"
            )
        traces = langfuse_io.fetch_traces(limit=5000)
    elif source == "export":
        path = export_path or DEFAULT_EXPORT
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(raw, dict) and "traces" in raw:
            raw = raw["traces"]
        # normalize one record at a time: normalize_traces() merges the turns
        # of a scenario into one record, which would lose the per-turn trace ids.
        traces = [normalize_trace(r) for r in raw]
    else:
        raise ValueError(f"unknown source {source!r}")
    keep = [
        t
        for t in traces
        if str(t.get("meta", {}).get("scenario_id") or "").startswith(SCENARIO_PREFIX)
    ]
    if not keep:
        raise RuntimeError(f"no {SCENARIO_PREFIX}* traces found in source {source!r}")
    return keep


# ---------------------------------------------------------------------------
# turn assembly
# ---------------------------------------------------------------------------


def _parts_text(value: Any) -> str:
    """Join the text parts of a [{role, parts:[{type:text, content}]}] list."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    out: list[str] = []
    if isinstance(value, list):
        for msg in value:
            if not isinstance(msg, dict):
                continue
            for part in msg.get("parts") or []:
                if isinstance(part, dict) and part.get("type") == "text" and part.get("content"):
                    out.append(str(part["content"]))
    if out:
        return "\n\n".join(out)
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _attrs(obs: dict[str, Any]) -> dict[str, Any]:
    md = obs.get("metadata") or {}
    if not isinstance(md, dict):
        return {}
    attrs = md.get("attributes")
    if isinstance(attrs, str):
        try:
            attrs = json.loads(attrs)
        except ValueError:
            attrs = {}
    return attrs if isinstance(attrs, dict) else {}


def _maybe_json(value: Any) -> Any:
    if isinstance(value, str):
        s = value.strip()
        if s[:1] in "[{":
            try:
                return json.loads(s)
            except ValueError:
                return value
    return value


def _result_status(result: Any) -> dict[str, Any]:
    """Pull the fields a reviewer must see first out of a tool result."""
    if not isinstance(result, dict):
        return {}
    keep = {}
    for key in ("ok", "status", "error", "note", "ticket_id", "refund_id"):
        if key in result:
            keep[key] = result[key]
    order = result.get("order")
    if isinstance(order, dict):
        for key in ("status", "refund_eligible"):
            if key in order:
                keep[f"order.{key}"] = order[key]
    return keep


def build_turn(trace: dict[str, Any]) -> dict[str, Any]:
    """Turn one normalized trace into an ordered list of display steps."""
    observations = sorted(
        trace.get("observations") or [], key=lambda o: o.get("start_time") or ""
    )
    gens = [o for o in observations if o.get("type") == "GENERATION"]
    tool_spans = [o for o in observations if o.get("type") == "TOOL"]

    system_prompt = None
    history: list[dict[str, Any]] = []
    if gens and isinstance(gens[-1].get("input"), list):
        history = [m for m in gens[-1]["input"] if isinstance(m, dict)]
    if gens and isinstance(gens[0].get("input"), list):
        for m in gens[0]["input"]:
            if isinstance(m, dict) and m.get("role") == "system":
                system_prompt = _parts_text([m])
                break

    # Only the part of the history that belongs to this turn: everything
    # after the last user message.
    last_user = max(
        (i for i, m in enumerate(history) if m.get("role") == "user"), default=-1
    )
    this_turn = history[last_user + 1 :] if last_user >= 0 else []

    steps: list[dict[str, Any]] = []
    tool_items: list[dict[str, Any]] = []
    by_call_id: dict[str, dict[str, Any]] = {}
    for msg in this_turn:
        role = msg.get("role")
        for part in msg.get("parts") or []:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if role == "assistant" and ptype == "thinking" and part.get("thinking"):
                steps.append({"kind": "thinking", "text": str(part["thinking"])})
            elif role == "assistant" and ptype == "text" and part.get("content"):
                steps.append({"kind": "interim", "text": str(part["content"])})
            elif ptype == "tool_call":
                item = {
                    "kind": "tool",
                    "name": part.get("name"),
                    "call_id": part.get("id"),
                    "args": part.get("arguments"),
                    "result": None,
                    "source": "history",
                }
                steps.append(item)
                tool_items.append(item)
                if part.get("id"):
                    by_call_id[str(part["id"])] = item
            elif ptype == "tool_call_response":
                item = by_call_id.get(str(part.get("id")))
                if item is not None and item.get("result") is None:
                    item["history_result"] = part.get("response")

    # Replace arguments/results with the TOOL span copies (clean JSON), matched
    # in order by tool name.
    unused = list(tool_spans)
    for item in tool_items:
        match = next((s for s in unused if s.get("name") == item["name"]), None)
        if match is None:
            continue
        unused.remove(match)
        _fill_from_span(item, match)
    for span in unused:  # spans with no history entry (should be rare)
        item = {"kind": "tool", "name": span.get("name"), "args": None, "result": None}
        _fill_from_span(item, span)
        steps.append(item)
    for item in tool_items:
        if item.get("source") == "history" and item.get("history_result") is not None:
            item["result"] = item.get("history_result")
        item.pop("history_result", None)

    for item in steps:
        if item["kind"] == "tool":
            item["write"] = item.get("name") in WRITE_TOOLS
            item["retrieval"] = item.get("name") in RETRIEVAL_TOOLS
            item["status"] = _result_status(item.get("result"))

    meta = trace.get("metadata") or {}
    tools_used = [s["name"] for s in steps if s["kind"] == "tool"]
    store_id = meta.get("cartwheel.store_id")
    permission_denied = False
    for s in steps:
        if s["kind"] == "tool":
            a = s.get("attrs") or {}
            store_id = store_id or a.get("cartwheel.store_id")
            if str(a.get("cartwheel.permission_denied", "")).lower() == "true":
                permission_denied = True
    if not store_id and system_prompt:
        m = re.search(r"Store id:\s*(\S+)", system_prompt)
        if m and m.group(1).lower() != "none":
            store_id = m.group(1)

    return {
        "trace_id": trace["trace_id"],
        "timestamp": trace.get("timestamp"),
        "scenario_id": trace.get("meta", {}).get("scenario_id"),
        "session_id": trace.get("meta", {}).get("session_id"),
        "user_role": meta.get("cartwheel.user_role"),
        "user_id": meta.get("cartwheel.user_id"),
        "store_id": store_id,
        "prompt_version": meta.get("cartwheel.prompt_version"),
        "user": _parts_text(trace.get("input")),
        "steps": steps,
        "reply": _parts_text(trace.get("output")),
        "tools_used": tools_used,
        "permission_denied": permission_denied,
        "generations": len(gens),
        "input_tokens": sum(
            int((g.get("usage_details") or {}).get("input") or 0) for g in gens
        ),
        "cost": round(
            sum(float((g.get("cost_details") or {}).get("total") or 0) for g in gens), 6
        ),
        "models": trace.get("models") or [],
        "permalink": trace.get("permalink"),
        "_system_prompt": system_prompt,
    }


def _fill_from_span(item: dict[str, Any], span: dict[str, Any]) -> None:
    item["args"] = _maybe_json(span.get("input"))
    item["result"] = _maybe_json(span.get("output"))
    item["attrs"] = {
        k: v
        for k, v in _attrs(span).items()
        if k.startswith("cartwheel.")
    }
    item["span_id"] = span.get("id")
    item["source"] = "tool_span"


# ---------------------------------------------------------------------------
# corpus: conversations, features, outlier flags, 2D map
# ---------------------------------------------------------------------------


class Corpus:
    """All support traces, grouped into conversations, plus derived views."""

    def __init__(self, traces: list[dict[str, Any]], source: str, source_detail: str):
        self.source = source
        self.source_detail = source_detail
        self.turns: dict[str, dict[str, Any]] = {}
        sessions: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for t in traces:
            turn = build_turn(t)
            self.turns[turn["trace_id"]] = turn
            sessions[turn["session_id"] or turn["trace_id"]].append(turn)
        self.sessions: dict[str, list[str]] = {}
        for sid, turns in sessions.items():
            turns.sort(key=lambda x: x["timestamp"] or "")
            for i, turn in enumerate(turns):
                turn["turn_index"] = i + 1
                turn["turn_count"] = len(turns)
            self.sessions[sid] = [x["trace_id"] for x in turns]
        self._features()
        self._flags()
        self.graph = self._graph()

    # -- conversation -----------------------------------------------------

    def conversation(self, trace_id: str) -> dict[str, Any] | None:
        turn = self.turns.get(trace_id)
        if turn is None:
            return None
        sid = turn["session_id"] or trace_id
        ids = self.sessions[sid]
        turns = [self.turns[i] for i in ids]
        system_prompt = next((t["_system_prompt"] for t in turns if t["_system_prompt"]), None)
        tools = sorted({n for t in turns for n in t["tools_used"]})
        return {
            "session_id": sid,
            "focus_trace_id": trace_id,
            "scenario_id": turn["scenario_id"],
            "user_role": turn["user_role"],
            "user_id": turn["user_id"],
            "store_id": next((t["store_id"] for t in turns if t["store_id"]), None),
            "turn_count": len(turns),
            "tools_used": tools,
            "permission_denied": any(t["permission_denied"] for t in turns),
            "system_prompt": system_prompt,
            "flags": turn.get("flags", []),
            "turns": [{k: v for k, v in t.items() if not k.startswith("_")} for t in turns],
        }

    def centroid_distance(self, trace_id: str) -> float:
        return self._dist.get(trace_id, 0.0)

    def index(self) -> list[dict[str, Any]]:
        out = []
        for t in self.turns.values():
            out.append(
                {
                    "trace_id": t["trace_id"],
                    "scenario_id": t["scenario_id"],
                    "session_id": t["session_id"],
                    "user_role": t["user_role"],
                    "turn_index": t["turn_index"],
                    "turn_count": t["turn_count"],
                    "tools_used": t["tools_used"],
                    "timestamp": t["timestamp"],
                    "user_preview": (t["user"] or "")[:140],
                }
            )
        out.sort(key=lambda r: (r["scenario_id"] or "", r["turn_index"]))
        return out

    # -- features / outliers ----------------------------------------------

    _FLAG_FEATURES = {
        "tool_calls": "tool calls",
        "reply_words": "words in reply",
        "user_words": "words in user message",
        "input_tokens": "input tokens",
        "generations": "LLM steps",
    }

    def _features(self) -> None:
        for t in self.turns.values():
            t["features"] = {
                "tool_calls": len(t["tools_used"]),
                "reply_words": len((t["reply"] or "").split()),
                "user_words": len((t["user"] or "").split()),
                "input_tokens": t["input_tokens"],
                "generations": t["generations"],
                "turn_index": t["turn_index"],
                "turn_count": t["turn_count"],
                "write": int(any(n in WRITE_TOOLS for n in t["tools_used"])),
                "retrieval": int(any(n in RETRIEVAL_TOOLS for n in t["tools_used"])),
                "denied": int(t["permission_denied"]),
            }

    def _flags(self) -> None:
        """Flag a turn only where it is in the top or bottom 5% of the corpus."""
        n = len(self.turns)
        for key, label in self._FLAG_FEATURES.items():
            vals = sorted(t["features"][key] for t in self.turns.values())
            for t in self.turns.values():
                v = t["features"][key]
                below = sum(1 for x in vals if x < v) / n
                above = sum(1 for x in vals if x > v) / n
                flags = t.setdefault("flags", [])
                if below >= 0.95:
                    flags.append(f"{v} {label} (more than {round(below * 100)}%)")
                elif above >= 0.95:
                    flags.append(f"{v} {label} (fewer than {round(above * 100)}%)")

    def _graph(self, k: int = 8) -> dict[str, Any]:
        roles = sorted({t["user_role"] or "?" for t in self.turns.values()})
        ids = list(self.turns)
        raw = []
        for tid in ids:
            t = self.turns[tid]
            f = t["features"]
            vec = [
                f["tool_calls"], math.log1p(f["reply_words"]), math.log1p(f["user_words"]),
                f["turn_index"], f["turn_count"], f["write"], f["retrieval"], f["denied"],
            ] + [1.0 if (t["user_role"] or "?") == r else 0.0 for r in roles]
            raw.append([float(x) for x in vec])
        vectors = selection._standardize(raw)
        assign = selection._kmeans(vectors, k=k, seed=7)
        xy = _pca2(vectors)
        centroids: dict[int, list[float]] = {}
        for c in set(assign):
            members = [vectors[i] for i in range(len(ids)) if assign[i] == c]
            centroids[c] = [sum(col) / len(members) for col in zip(*members)]
        self._dist = {
            ids[i]: sum((a - b) ** 2 for a, b in zip(vectors[i], centroids[assign[i]]))
            for i in range(len(ids))
        }
        nodes = []
        for i, tid in enumerate(ids):
            t = self.turns[tid]
            nodes.append(
                {
                    "trace_id": tid,
                    "x": xy[i][0],
                    "y": xy[i][1],
                    "cluster": assign[i],
                    "role": t["user_role"],
                    "scenario_id": t["scenario_id"],
                    "turn": f'{t["turn_index"]}/{t["turn_count"]}',
                    "tools": t["tools_used"],
                }
            )
            t["cluster"] = assign[i]
        clusters = []
        for c in sorted(set(assign)):
            members = [self.turns[ids[i]] for i in range(len(ids)) if assign[i] == c]
            tools = defaultdict(int)
            for m in members:
                for n_ in set(m["tools_used"]):
                    tools[n_] += 1
            top = sorted(tools.items(), key=lambda kv: -kv[1])[:3]
            clusters.append(
                {"id": c, "size": len(members), "top_tools": [t for t, _ in top]}
            )
        return {"nodes": nodes, "clusters": clusters}


def _pca2(vectors: list[list[float]]) -> list[tuple[float, float]]:
    import numpy as np

    arr = np.asarray(vectors, dtype=float)
    if len(arr) < 2:
        return [(0.0, 0.0) for _ in vectors]
    arr = arr - arr.mean(axis=0)
    _, _, vt = np.linalg.svd(arr, full_matrices=False)
    proj = arr @ vt[:2].T
    # deterministic jitter so identical feature vectors do not stack
    out = []
    for i, (x, y) in enumerate(proj):
        jx = ((i * 7919) % 101 - 50) / 400.0
        jy = ((i * 104729) % 97 - 48) / 400.0
        out.append((round(float(x) + jx, 4), round(float(y) + jy, 4)))
    return out
