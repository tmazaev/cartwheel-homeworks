# HW4 review app

A trace review interface for Cartwheel error analysis. It is a standard-library Python server plus one self-contained HTML file, and it adds no new dependencies. It reuses `analysis/helpers` (`langfuse_io`, `normalization`, `_state`, `selection`).

## Run

From the repository root, with Langfuse running (`docker compose -f observability/docker-compose.yml up -d`):

```bash
uv run python -m analysis.review_app.server
```

Then open http://127.0.0.1:8030/.

| flag | effect |
| --- | --- |
| `--source langfuse` (default) | Live traces through `langfuse_io.fetch_traces`, restricted to `support-*` scenarios. |
| `--source export` | Offline fallback that reads `traces/support_traces.json`. The top bar shows which source is active. |
| `--no-score-writes` (or `REVIEW_APP_SCORE_WRITES=0`) | Saves labels locally only and never writes Langfuse scores. |
| `--port 8030` | The default port. The reference UI uses 8020. |

`CARTWHEEL_ANALYSIS_STATE=/some/dir` points every state file at another directory, which is useful for tests.

## What you review

The unit of review is one trace, meaning one user turn. Each trace is always shown inside its whole conversation: all traces that share `cartwheel.session_id`, oldest first. The turn under review has a dark frame, and the other turns stay fully readable inside a light frame.

Each turn is laid out top to bottom:

1. The user message.
2. Each step, in order:
   - The assistant's thinking, in italics.
   - Its text before a tool call.
   - The tool call. Arguments and result share one container, and both come from the TOOL span as pretty-printed JSON.
3. The final reply.

Write tools (`issue_refund`, `cancel_order`, `escalate_to_human`) get a heavy red border, with `ok`, `status` and `error` shown as pills. The system prompt appears once per conversation, collapsed and dimmed.

## Views

- **Conversation**
  - Select text, write a note in the popover and press Enter to save it.
  - Notes sit in the right margin, aligned with their highlights and linked on hover. Each note can be edited or deleted.
  - Each trace has a "No failure observed" button.
  - There are no categories or dropdowns in this view.
- **Map**: a 2D projection of every support trace, with cluster hulls. Batch traces get a dark ring and annotated traces are orange. Click a dot to open its conversation.
- **Taxonomy**: the modes in `patterns.json` with their definition, boundary, SPEC id and evaluator type. Supporting annotations are clickable, and open codes that are not yet in any mode are listed too.
- **Labels**: a grid with one present/absent judgment per reviewed trace and mode, plus an evidence note.
- **Progress**: per batch, reviewed vs. total, the distinct traces reviewed toward 100, the label judgments still missing, and coverage by role and cluster.
- **Suggestions**: agent suggestions in dashed boxes, each tagged "agent suggestion", with accept/reject on every item and select-all bulk actions. Rejected suggestions stay stored with their status and reason.

## State files (`analysis/state/`)

| file | content |
| --- | --- |
| `samples.json` | One row per trace in the review set: batch, method and reason. |
| `sample_manifest.json` | Batch definitions, plus each trace's batch and selection method. A trace can belong to only one batch, and the server returns 409 for a second one. |
| `annotations.json` | Open codes: `trace_id`, `session_id`, `quote`, `note`, `anchor`, timestamps and `batch`. An edit pushes the old text into `history`. A delete is soft (`deleted_at`) and keeps the original open code. |
| `patterns.json` | The taxonomy, pushed by the coding agent. Every push also appends the previous version to `patterns_history.jsonl`. |
| `suggestions.json` | Agent suggestions with status `pending`, `accepted` or `rejected`. Accepting one creates an annotation. |
| `labels/<mode>.jsonl` | Append-only judgments (`label` 1 = failure present). A changed judgment marks the old row `superseded_by`. Each row records whether its Langfuse score write succeeded. |
| `graph.json` | The map projection, rewritten when the server starts. |

The browser also keeps a `localStorage` backup of annotations and suggestions. Each accepted label is written to Langfuse with `langfuse_io.write_label_score`. If that write fails or is disabled, the label is still saved locally and a warning banner is shown.

## Adding review batches (Part B)

Batch names are `uniform`, `cluster`, `dimension`, `depth` and `final_uniform`.

The server can pick traces that are not yet in any batch:

```bash
curl -s -X POST localhost:8030/api/select -H 'Content-Type: application/json' \
  -d '{"batch":"uniform","method":"random","k":15,"seed":7,"dry_run":true}'
```

- `method` is one of:
  - `random`
  - `cluster` (the trace closest to each cluster centroid, round-robin)
  - `stratified` (with `field` set to `user_role`, `turn_position` or `write_tool`)
- Drop `dry_run` to add the picks.
- For depth searches, post explicit picks to `/api/samples`, for example: `{"batch":"depth","method":"depth search: <mode>","picks":[{"trace_id":"…","reason":"…"}]}`.

Do not use `analysis.helpers.select_traces` with this app: it overwrites `samples.json` in the reference UI's format.

## API

| endpoint | purpose |
| --- | --- |
| `GET /api/source` | The active source and whether score writing is on. |
| `GET /api/conversations` | An index of all support traces. |
| `GET /api/conversations?trace_id=…` | One whole conversation. |
| `GET /api/graph` | The map projection and clusters. |
| `GET` / `POST /api/samples` | The review set; POST adds picks to a batch. |
| `POST /api/select` | Server-side selection for a batch. |
| `GET /api/manifest` | The sample manifest. |
| `GET` / `POST /api/annotations` | POST takes `{op: create, update or delete}`. |
| `GET` / `POST /api/patterns` | The taxonomy. |
| `GET` / `POST /api/suggestions` | POST takes a list (the agent pushes it) or `{op: "decide", ids, status, reason}`. |
| `GET` / `POST /api/labels` | POST takes `{mode, trace_id, label, evidence}`. |
