# Homework 3 progress

Model for every run: `claude-haiku-4-5` (same value in `.env` and `--model`).
HW2 is my own implementation, so the reference patch is not applied.

## Done

- 2026-11-06: Preparation, data reset with `uv run python -m seed.generate`
  (10000 orders, 525 users, 20 stores, 800 products, 6 damaged records, 18 policy docs).
- 2026-11-06: Langfuse healthy, server running on port 8010 (restart it in a new session).
- 2026-11-06: One labeled test request, trace b8697f228d3d2c78c53259b9a61b1aef (label smoke-0001):
  conversation, model, tool call and label all present; token usage shows input tokens only.
- 2026-11-06: Part A dimension plan approved. Role; difficulty (well specified, ambiguous, missing
  information, boundary); user style (8 validator values); intent (order status, return deadline,
  refund, cancellation, policy question, product search, dispute, account change, out of scope);
  record (order in window, past window, above $100, placed, shipped, someone else's, damaged;
  product; store policy page; none); applicable policy (platform, store override, none);
  tools needed (none, one lookup, several). Turn count recorded per scenario. No prompt injection.
- 2026-11-06: 30 pilot plans built by scenarios/build_plans.py into scenarios/pilot_plans.jsonl
  (18 coverage, 12 challenge; 16 shopper, 7 merchant, 7 support), no model used.
- 2026-11-06: User messages written by scenarios/write_messages.py (claude-sonnet-5-5 writer and critic,
  run with `uv run --with anthropic`) into scenarios/pilot_scenarios.jsonl.
- 2026-11-06: Sample accepted, #7 reworded. Pilot run: 30/30 completed on claude-haiku-4-5,
  scenarios/pilot-results.jsonl written, about 3 minutes.
- 2026-11-06: scenarios/pilot_review.jsonl written: 12 reviewed, 6 confirmed failures, 3 scenarios to fix.
- 2026-11-06: 250 final plans in scenarios/support_plans.jsonl (175 coverage, 75 challenge), built from a
  clean seeded copy of the database because the pilot had changed local orders.
- 2026-11-06: scenarios/support_scenarios.jsonl written and validated --final. Reviewed 15 in
  scenarios/support_review.jsonl (14 accept, 1 revise); fixed the 6 staff product searches.
  support-0024 is the revised scenario for the video.
- 2026-11-06: scenarios/monitoring_scenarios.jsonl written, 50 scenarios picked at random with seed 7.
- 2026-11-06: Final run: 250/250 completed on claude-haiku-4-5 (175 coverage, 75 challenge), about 28 minutes.
- 2026-11-06: reports/smoke-output.txt saved. traces/support_traces.json exported: 350 traces, 250 of 250
  scenarios. Checked support-0041, support-0186, support-0247: all fields present.

## Next

- Commit the deliverables. The video is mine to do; Langfuse must be running for it.

## Deliverables (Files to commit)

- [x] scenarios/pilot_scenarios.jsonl
- [x] scenarios/pilot-results.jsonl
- [x] scenarios/pilot_review.jsonl
- [x] scenarios/support_scenarios.jsonl
- [x] scenarios/support_review.jsonl
- [x] scenarios/monitoring_scenarios.jsonl
- [x] scenarios/final-results.jsonl
- [x] reports/smoke-output.txt
- [x] traces/support_traces.json

The video and the assessments are mine to do.
