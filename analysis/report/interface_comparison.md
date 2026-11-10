# Interface comparison

## Context

- **Source.** Traces come from the live Langfuse project `cartwheel-dev`, not the offline export. The review app loads 350 `support-*` traces in 250 sessions; `pilot-*` scenarios are excluded.
- **Prior inspection.** Before the interface was built, 8 traces were inspected in the standard Langfuse trace view:
  - single-turn traces for each user role;
  - three multi-turn sessions;
  - two traces that call a write tool.

## Friction in the standard Langfuse view

- **Empty wrapper levels.** Content sits three levels deep: `cartwheel.session_message` › `Agent Workflow` › `cartwheel-support.agent`. Two of those levels have empty input and output.
- **One trace per user turn.** Langfuse's own `sessionId` is not set, so the Sessions view groups nothing. Earlier turns appear only as text inside a later generation's input.
- **Tool result far from the reply.** The tool result is a separate TOOL span, far from the reply, which sits only on the trace and root span. The result also appears a second time inside the next generation's input as a Python-repr string.
  - Example: in support-0167, comparing the reply ("Done.") with the `issue_refund` result (`queued_for_approval`) takes several clicks.
- **Nothing to see on generations.** Generation outputs are empty, and the full system prompt is repeated at the start of every generation input.
- **Identifiers buried.** Role, user, scenario and `permission_denied` are nested under metadata › attributes.
- **Misleading token and cost figures.** Generations show 0 completion tokens, and the cost counts input tokens only.

## Design retained from the reference interface

Inline open coding is carried over from `analysis/ui/index.html`:

- select text, write a free-text note in a popover, and see it as a margin note aligned with its highlight;
- hovering links each note to its highlight, and notes can be edited or deleted;
- there are no categories or dropdowns.

This fits the first-failure stopping rule: the reviewer quotes the exact evidence and describes it in their own words. The same JSON-file API pattern from `analysis/server.py` is also kept.

## Design changed after inspecting the traces

**The reviewed trace is shown inside its whole conversation.**

- **Grouping.** All traces that share `cartwheel.session_id` are shown in time order. The reviewed turn has a dark frame, and the other turns stay fully readable.
- **Turn layout.** Each turn shows, in order:
  1. the user message;
  2. thinking and the text before a tool call, recovered from generation input history;
  3. each tool call, with arguments and result taken as JSON from its TOOL span, in one container;
  4. the final reply, directly below.
- **Write tools.** `issue_refund`, `cancel_order` and `escalate_to_human` get a red border, with their `ok` and `status` fields shown as pills.
- **System prompt.** It is shown once per conversation, collapsed.

This directly fixes the layout problems above: split turns, results separated from replies, and duplicated prompts.

## Remaining limitation

- **Final-reply reasoning is not visible.** Generation outputs are empty, so the reasoning behind each final reply is missing. Turns without tool calls show only the reply.
- **Slow startup.** Loading all traces live takes about 30–60 seconds, and new traces appear only after a restart.

Drafted with a coding agent; reviewed by the student.
