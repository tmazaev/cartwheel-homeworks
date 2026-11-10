"""HW4 review server: stdlib HTTP server over analysis/state/ and Langfuse.

Run from the repository root:

    uv run python -m analysis.review_app.server                   # live Langfuse, :8030
    uv run python -m analysis.review_app.server --source export   # offline Module 1 export
    uv run python -m analysis.review_app.server --no-score-writes # never write Langfuse scores

The unit of review is one trace (one user turn). The app always shows that
trace inside its whole conversation (all traces sharing cartwheel.session_id).

State lives under ``analysis/state/`` (or ``$CARTWHEEL_ANALYSIS_STATE``):

    samples.json            the review set: one row per trace (batch, method, reason)
    sample_manifest.json    batch definitions + each trace's batch and selection method
    annotations.json        open codes; edits keep history, deletes are soft
    patterns.json           the taxonomy (agent-maintained); each push is versioned
                            in patterns_history.jsonl
    suggestions.json        agent suggestions with pending/accepted/rejected status
    labels/<mode>.jsonl     append-only present/absent judgments (label 1 = failure present)
    graph.json              2D projection used by the map view (derived, rewritten on start)

API:

    GET  /api/source                    active trace source and score-writing status
    GET  /api/conversations             index of all support traces
    GET  /api/conversations?trace_id=X  the conversation containing trace X
    GET  /api/graph                     2D projection + clusters
    GET  /api/samples        POST /api/samples   {batch, method, picks:[{trace_id, reason}]}
    POST /api/select                    {batch, method: random|cluster|stratified, k, field?, seed?}
    GET  /api/manifest
    GET  /api/annotations    POST /api/annotations  {op: create|update|delete, ...}
    GET  /api/patterns       POST /api/patterns     {modes:[...]}
    GET  /api/suggestions    POST /api/suggestions  [..] (agent push) or {op: decide, ids, status}
    GET  /api/labels         POST /api/labels       {mode, trace_id, label: 0|1, evidence}
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from analysis.helpers import _state, langfuse_io

from . import data

HERE = Path(__file__).resolve().parent
UI_FILE = HERE / "index.html"

BATCHES: dict[str, dict[str, Any]] = {
    "uniform": {"target": 15, "description": "uniformly sampled traces"},
    "cluster": {"target": 15, "description": "cluster representatives"},
    "dimension": {"target": 30, "description": "spread across one product dimension chosen before reading outcomes"},
    "depth": {"target": 25, "description": "depth searches for candidate modes and close negatives"},
    "final_uniform": {"target": 15, "description": "uniform sample after drafting the taxonomy"},
}
REVIEW_TARGET = 100

LOCK = threading.Lock()
CORPUS: data.Corpus | None = None
SCORE_WRITES = True
LANGFUSE_TRACE_URL: str | None = None  # e.g. http://localhost:3000/project/<id>/traces/


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# state helpers
# ---------------------------------------------------------------------------


def _p(*parts: str) -> Path:
    return _state.state_path(*parts)


def load_samples() -> list[dict[str, Any]]:
    return _state.read_json(_p("samples.json"), default=[]) or []


def load_manifest() -> dict[str, Any]:
    m = _state.read_json(_p("sample_manifest.json"), default=None)
    if not isinstance(m, dict) or "traces" not in m:
        m = {"batches": BATCHES, "traces": {}, "events": []}
    m.setdefault("batches", BATCHES)
    m.setdefault("events", [])
    return m


def load_annotations() -> list[dict[str, Any]]:
    d = _state.read_json(_p("annotations.json"), default={"annotations": []})
    if isinstance(d, dict):
        d = d.get("annotations", [])
    return [a for a in d if isinstance(a, dict)] if isinstance(d, list) else []


def save_annotations(rows: list[dict[str, Any]]) -> None:
    _state.write_json(_p("annotations.json"), {"annotations": rows})


def load_patterns() -> dict[str, Any]:
    d = _state.read_json(_p("patterns.json"), default={"modes": []})
    return d if isinstance(d, dict) else {"modes": []}


def load_suggestions() -> list[dict[str, Any]]:
    d = _state.read_json(_p("suggestions.json"), default=[])
    if isinstance(d, dict):
        d = d.get("suggestions", [])
    return d if isinstance(d, list) else []


def label_rows(mode: str) -> list[dict[str, Any]]:
    return _state.read_jsonl(_p("labels", f"{mode}.jsonl"))


def live_labels() -> dict[str, dict[str, dict[str, Any]]]:
    out: dict[str, dict[str, dict[str, Any]]] = {}
    ldir = _p("labels")
    if ldir.exists():
        for f in sorted(ldir.glob("*.jsonl")):
            live: dict[str, dict[str, Any]] = {}
            for row in _state.read_jsonl(f):
                if row.get("superseded_by"):
                    continue
                live[str(row.get("trace_id"))] = row
            out[f.stem] = live
    return out


# ---------------------------------------------------------------------------
# operations
# ---------------------------------------------------------------------------


class ApiError(Exception):
    def __init__(self, status: int, message: str, **extra: Any):
        super().__init__(message)
        self.status = status
        self.extra = extra


def add_samples(batch: str, method: str, picks: list[dict[str, Any]]) -> dict[str, Any]:
    """Add traces to a batch. All-or-nothing; a trace may belong to one batch only."""
    if batch not in BATCHES:
        raise ApiError(400, f"unknown batch {batch!r}; use one of {sorted(BATCHES)}")
    if not picks:
        raise ApiError(400, "no picks given")
    assert CORPUS is not None
    manifest = load_manifest()
    samples = load_samples()
    seen: set[str] = set()
    conflicts, unknown = [], []
    for p in picks:
        tid = str(p.get("trace_id"))
        if tid not in CORPUS.turns:
            unknown.append(tid)
        elif tid in manifest["traces"] or tid in seen:
            prior = manifest["traces"].get(tid, {}).get("batch", batch)
            conflicts.append({"trace_id": tid, "batch": prior})
        seen.add(tid)
    if unknown:
        raise ApiError(400, "trace ids not in the support-* corpus", unknown=unknown)
    if conflicts:
        raise ApiError(409, "trace already belongs to a batch; a trace counts toward one batch only", conflicts=conflicts)
    at = _now()
    for p in picks:
        tid = str(p["trace_id"])
        turn = CORPUS.turns[tid]
        row = {
            "trace_id": tid,
            "batch": batch,
            "method": p.get("method") or method,
            "reason": p.get("reason") or method,
            "scenario_id": turn["scenario_id"],
            "session_id": turn["session_id"],
            "user_role": turn["user_role"],
            "added_at": at,
        }
        manifest["traces"][tid] = {k: row[k] for k in ("batch", "method", "reason", "scenario_id", "session_id", "added_at")}
        samples.append(row)
    manifest["events"].append({"at": at, "batch": batch, "method": method, "count": len(picks), "source": CORPUS.source})
    _state.write_json(_p("sample_manifest.json"), manifest)
    _state.write_json(_p("samples.json"), samples)
    return {"ok": True, "added": len(picks), "batch": batch}


def select_picks(batch: str, method: str, k: int, field: str | None, seed: int) -> list[dict[str, Any]]:
    """Choose ``k`` traces not yet in any batch."""
    assert CORPUS is not None
    taken = set(load_manifest()["traces"])
    pool = [t for tid, t in CORPUS.turns.items() if tid not in taken]
    pool.sort(key=lambda t: t["trace_id"])
    rng = random.Random(seed)
    if method == "random":
        chosen = rng.sample(pool, min(k, len(pool)))
        return [{"trace_id": t["trace_id"], "reason": f"uniform random (seed {seed})"} for t in chosen]
    if method == "cluster":
        by_c: dict[int, list[dict[str, Any]]] = {}
        for t in pool:
            by_c.setdefault(t["cluster"], []).append(t)
        for c, members in by_c.items():
            members.sort(key=lambda t: CORPUS.centroid_distance(t["trace_id"]))
        out, ci = [], 0
        keys = sorted(by_c)
        while len(out) < k and any(by_c[c] for c in keys):
            c = keys[ci % len(keys)]
            if by_c[c]:
                t = by_c[c].pop(0)
                out.append({"trace_id": t["trace_id"], "reason": f"cluster {c} representative (closest to centroid)"})
            ci += 1
        return out
    if method == "stratified":
        field = field or "user_role"
        groups: dict[str, list[dict[str, Any]]] = {}
        for t in pool:
            groups.setdefault(str(_field_value(t, field)), []).append(t)
        for g in groups.values():
            rng.shuffle(g)
        out, keys, gi = [], sorted(groups), 0
        while len(out) < k and any(groups[g] for g in keys):
            g = keys[gi % len(keys)]
            if groups[g]:
                t = groups[g].pop()
                out.append({"trace_id": t["trace_id"], "reason": f"{field}={g} (stratified, seed {seed})"})
            gi += 1
        return out
    raise ApiError(400, f"unknown method {method!r}; use random, cluster or stratified")


def _field_value(t: dict[str, Any], field: str) -> Any:
    if field == "turn_position":
        return "first turn" if t["turn_index"] == 1 else "follow-up turn"
    if field == "write_tool":
        return "write tool" if t["features"]["write"] else "no write tool"
    return t.get(field)


def annotation_op(body: dict[str, Any]) -> dict[str, Any]:
    rows = load_annotations()
    op = body.get("op")
    at = _now()
    if op == "create":
        tid = str(body.get("trace_id") or "")
        if CORPUS is not None and tid not in CORPUS.turns:
            raise ApiError(400, f"unknown trace {tid}")
        turn = CORPUS.turns[tid] if CORPUS else {}
        manifest = load_manifest()
        kind = body.get("kind") or "note"
        if kind not in ("note", "no_failure"):
            raise ApiError(400, "kind must be note or no_failure")
        if kind == "note" and not str(body.get("note") or "").strip():
            raise ApiError(400, "a note needs text")
        ann = {
            "id": body.get("id") or f"ann-{uuid.uuid4().hex[:10]}",
            "trace_id": tid,
            "session_id": turn.get("session_id"),
            "scenario_id": turn.get("scenario_id"),
            "batch": manifest["traces"].get(tid, {}).get("batch"),
            "kind": kind,
            "quote": body.get("quote") or "",
            "note": (body.get("note") or ("no failure observed" if kind == "no_failure" else "")).strip(),
            "anchor": body.get("anchor"),
            "origin": body.get("origin") or "human",
            "created_at": at,
            "updated_at": at,
            "history": [],
        }
        rows.append(ann)
        save_annotations(rows)
        return {"ok": True, "annotation": ann}
    target = next((a for a in rows if a.get("id") == body.get("id")), None)
    if target is None:
        raise ApiError(404, f"no annotation {body.get('id')}")
    if op == "update":
        note = str(body.get("note") or "").strip()
        if not note:
            raise ApiError(400, "a note needs text")
        if note != target.get("note"):
            target.setdefault("history", []).append({"note": target.get("note"), "at": target.get("updated_at")})
            target["note"] = note
            target["updated_at"] = at
        save_annotations(rows)
        return {"ok": True, "annotation": target}
    if op == "delete":
        target.setdefault("history", []).append({"note": target.get("note"), "at": target.get("updated_at"), "event": "deleted"})
        target["deleted_at"] = at
        save_annotations(rows)
        return {"ok": True, "annotation": target}
    raise ApiError(400, "op must be create, update or delete")


def push_patterns(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict) or not isinstance(body.get("modes"), list):
        raise ApiError(400, "expected {modes: [...]}")
    prior = load_patterns()
    _state.append_jsonl(_p("patterns_history.jsonl"), {"replaced_at": _now(), "patterns": prior})
    body["updated_at"] = _now()
    _state.write_json(_p("patterns.json"), body)
    return {"ok": True, "count": len(body["modes"])}


def push_suggestions(body: Any) -> dict[str, Any]:
    rows = load_suggestions()
    by_id = {s.get("id"): s for s in rows}
    if isinstance(body, dict) and body.get("op") == "decide":
        status = body.get("status")
        if status not in ("accepted", "rejected"):
            raise ApiError(400, "status must be accepted or rejected")
        ids = body.get("ids") or []
        promoted = []
        for sid in ids:
            s = by_id.get(sid)
            if s is None:
                continue
            s["status"] = status
            s["decided_at"] = _now()
            if body.get("reason"):
                s["decision_note"] = body["reason"]
            if status == "accepted" and not s.get("annotation_id"):
                res = annotation_op({
                    "op": "create", "trace_id": s.get("trace_id"), "kind": "note",
                    "quote": s.get("quote") or "",
                    "note": s.get("note") or s.get("mode") or "accepted suggestion",
                    "anchor": s.get("anchor"), "origin": f"suggestion:{sid}",
                })
                s["annotation_id"] = res["annotation"]["id"]
                promoted.append(s["annotation_id"])
        _state.write_json(_p("suggestions.json"), rows)
        return {"ok": True, "decided": len(ids), "promoted": promoted}
    if isinstance(body, dict):
        body = body.get("suggestions", [])
    if not isinstance(body, list):
        raise ApiError(400, "expected a list of suggestions or {op: decide}")
    added = 0
    for s in body:
        if not isinstance(s, dict) or not s.get("trace_id"):
            continue
        sid = s.get("id") or f"sug-{uuid.uuid4().hex[:10]}"
        if sid in by_id:
            # agent may refine text, never overwrite a human decision
            keep = {k: by_id[sid][k] for k in ("status", "decided_at", "annotation_id", "decision_note") if k in by_id[sid]}
            by_id[sid].update(s)
            by_id[sid].update(keep)
            continue
        s = {**s, "id": sid, "status": "pending", "source": "agent", "created_at": _now()}
        rows.append(s)
        by_id[sid] = s
        added += 1
    _state.write_json(_p("suggestions.json"), rows)
    return {"ok": True, "added": added, "total": len(rows)}


def save_label(body: dict[str, Any]) -> dict[str, Any]:
    mode = str(body.get("mode") or "")
    tid = str(body.get("trace_id") or "")
    label = body.get("label")
    if not mode or not mode.replace("_", "").isalnum():
        raise ApiError(400, "mode must be a snake_case name")
    if label not in (0, 1):
        raise ApiError(400, "label must be 0 (absent) or 1 (failure present)")
    if CORPUS is not None and tid not in CORPUS.turns:
        raise ApiError(400, f"unknown trace {tid}")
    path = _p("labels", f"{mode}.jsonl")
    rows = _state.read_jsonl(path)
    n = sum(1 for r in rows if r.get("trace_id") == tid)
    label_id = f"{tid}#{n}"
    record = {
        "trace_id": tid,
        "label": int(label),
        "source": "human",
        "ts": _now(),
        "label_id": label_id,
        "evidence": str(body.get("evidence") or ""),
        "session_id": CORPUS.turns[tid]["session_id"] if CORPUS else None,
    }
    warning = None
    if SCORE_WRITES and langfuse_io.is_configured():
        try:
            langfuse_io.write_label_score(tid, mode, int(label), comment=record["evidence"] or None)
            record["langfuse"] = {"written": True, "at": _now()}
        except Exception as exc:  # network-only path
            record["langfuse"] = {"written": False, "error": str(exc)[:300]}
            warning = f"Saved locally, but the Langfuse score write failed: {exc}"
    else:
        record["langfuse"] = {"written": False, "error": "score writing disabled" if not SCORE_WRITES else "Langfuse not configured"}
        warning = "Saved locally only: Langfuse score writing is " + ("disabled" if not SCORE_WRITES else "not configured")
    # append-only: mark the prior live record superseded, then append
    changed = False
    for r in rows:
        if r.get("trace_id") == tid and not r.get("superseded_by"):
            r["superseded_by"] = label_id
            changed = True
    if changed:
        _state.write_jsonl(path, rows)
    _state.append_jsonl(path, record)
    return {"ok": True, "record": record, "warning": warning}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "CartwheelReview/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:  # quiet polling
        if os.environ.get("REVIEW_APP_VERBOSE"):
            super().log_message(fmt, *args)

    def _send(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> Any:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            return json.loads(raw or b"null")
        except ValueError:
            raise ApiError(400, "body is not JSON")

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        q = parse_qs(url.query)
        path = url.path
        try:
            if path in ("/", "/index.html"):
                body = UI_FILE.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            assert CORPUS is not None
            if path == "/api/source":
                self._send({
                    "source": CORPUS.source, "detail": CORPUS.source_detail,
                    "traces": len(CORPUS.turns), "sessions": len(CORPUS.sessions),
                    "score_writes": SCORE_WRITES and langfuse_io.is_configured(),
                    "state_dir": str(_state.state_root()),
                    "batches": BATCHES, "review_target": REVIEW_TARGET,
                    "langfuse_trace_url": LANGFUSE_TRACE_URL,
                })
            elif path == "/api/conversations":
                tid = (q.get("trace_id") or [None])[0]
                if tid:
                    conv = CORPUS.conversation(tid)
                    self._send(conv if conv else {"error": "unknown trace"}, 200 if conv else 404)
                else:
                    self._send(CORPUS.index())
            elif path == "/api/graph":
                self._send(CORPUS.graph)
            elif path == "/api/samples":
                self._send(load_samples())
            elif path == "/api/manifest":
                self._send(load_manifest())
            elif path == "/api/annotations":
                self._send({"annotations": load_annotations()})
            elif path == "/api/patterns":
                self._send(load_patterns())
            elif path == "/api/suggestions":
                self._send(load_suggestions())
            elif path == "/api/labels":
                self._send(live_labels())
            else:
                self._send({"error": f"unknown path {path}"}, 404)
        except ApiError as e:
            self._send({"error": str(e), **e.extra}, e.status)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            body = self._body()
            with LOCK:
                if path == "/api/samples":
                    if not isinstance(body, dict):
                        raise ApiError(400, "expected {batch, method, picks}")
                    res = add_samples(str(body.get("batch")), str(body.get("method") or "manual"), body.get("picks") or [])
                elif path == "/api/select":
                    if not isinstance(body, dict):
                        raise ApiError(400, "expected {batch, method, k}")
                    picks = select_picks(str(body.get("batch")), str(body.get("method")), int(body.get("k") or 0), body.get("field"), int(body.get("seed") or 7))
                    if body.get("dry_run"):
                        res = {"ok": True, "picks": picks}
                    else:
                        res = add_samples(str(body.get("batch")), str(body.get("method")), picks)
                        res["picks"] = picks
                elif path == "/api/annotations":
                    if not isinstance(body, dict):
                        raise ApiError(400, "expected {op, ...}")
                    res = annotation_op(body)
                elif path == "/api/patterns":
                    res = push_patterns(body)
                elif path == "/api/suggestions":
                    res = push_suggestions(body)
                elif path == "/api/labels":
                    if not isinstance(body, dict):
                        raise ApiError(400, "expected {mode, trace_id, label}")
                    res = save_label(body)
                else:
                    raise ApiError(404, f"cannot POST to {path}")
            self._send(res)
        except ApiError as e:
            self._send({"error": str(e), **e.extra}, e.status)


def main() -> None:
    global CORPUS, SCORE_WRITES, LANGFUSE_TRACE_URL
    ap = argparse.ArgumentParser(description="Cartwheel HW4 review app")
    ap.add_argument("--port", type=int, default=8030)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--source", choices=["langfuse", "export"], default="langfuse",
                    help="langfuse (default, live) or export (offline traces/support_traces.json)")
    ap.add_argument("--export", type=Path, default=data.DEFAULT_EXPORT, help="export path for --source export")
    ap.add_argument("--no-score-writes", action="store_true", help="never write label scores to Langfuse")
    args = ap.parse_args()

    try:
        from dotenv import load_dotenv

        load_dotenv(data.REPO_ROOT / ".env")
    except ImportError:
        pass
    SCORE_WRITES = not args.no_score_writes and os.environ.get("REVIEW_APP_SCORE_WRITES", "1") != "0"

    print(f"loading support-* traces from {args.source} ...", flush=True)
    traces = data.load_traces(args.source, args.export)
    detail = os.environ.get("LANGFUSE_HOST", "") if args.source == "langfuse" else str(args.export)
    CORPUS = data.Corpus(traces, args.source, detail)
    if langfuse_io.is_configured():
        try:  # read-only lookup so the UI can link each turn to the Langfuse trace view
            project = langfuse_io._client().api.projects.get().data[0]
            LANGFUSE_TRACE_URL = f"{os.environ['LANGFUSE_HOST'].rstrip('/')}/project/{project.id}/traces/"
        except Exception:
            LANGFUSE_TRACE_URL = None
    _state.state_root().mkdir(parents=True, exist_ok=True)
    _state.write_json(_p("graph.json"), CORPUS.graph)
    print(f"{len(CORPUS.turns)} traces in {len(CORPUS.sessions)} conversations")
    print(f"state: {_state.state_root()}")
    print(f"Langfuse score writes: {'ON' if SCORE_WRITES and langfuse_io.is_configured() else 'OFF'}")
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"review app on http://{args.host}:{args.port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
