# HW4 review summary

## Reviewed sample

The sample is **100 distinct traces** (one trace is one user turn), all `support-*` scenarios from the live Langfuse project `cartwheel-dev`, which holds 350 such traces in 250 sessions; `pilot-*` scenarios are excluded. Each trace was reviewed inside its full conversation (grouped by `cartwheel.session_id`). No trace counts toward more than one batch; the app refuses a second assignment. The batch and selection method of every trace are recorded in `analysis/state/sample_manifest.json`.

| Batch | Traces | Selection |
|---|---|---|
| uniform | 15 | uniform random, seed 7 |
| cluster | 15 | closest-to-centroid representatives, round-robin over k-means clusters (k = 8) on structural features |
| dimension | 30 | stratified by `user_role`: 10 shopper, 10 merchant, 10 support (random within role, seed 7) |
| depth | 25 | code-filter searches per candidate mode, mixing likely positives and close negatives; the signal for each trace is in the manifest |
| final_uniform | 15 | uniform random from the unbatched pool, seed 11 |

The role mix is 56 shopper, 22 merchant and 22 support; 20 traces are follow-up turns.

Open coding produced 46 traces with a failure note and 54 marked "no failure observed": uniform 8/7, cluster 8/7, dimension 11/19, depth 14/11, final_uniform 5/10.

## Final taxonomy

The counts below are **sample fractions, not prevalence estimates**. The cluster, dimension and depth batches deliberately change the composition of the sample, and the depth batch is enriched for failures. Fail = failure present. Every one of the 100 traces has a judgment for every mode, giving 500 labels in `analysis/state/labels/<mode>.jsonl`; all 500 were also written to Langfuse as numeric scores.

| Mode | Binary definition (short) | SPEC source | Evaluator | Positives / close negatives | Fail (sample fraction) | Final 15 |
|---|---|---|---|---|---|---|
| `unreliable_tool_data_presented_as_fact` | Fail if the reply presents invalid, inconsistent, empty or malformed tool output as a reliable fact (or explains it away) instead of flagging or escalating | RESP-3, ESC-4 | LLM judge, code-assisted | 11 / 3 | 11 (0.11) | 3 |
| `store_return_window_not_verified` | Fail if a return deadline, window or eligibility reason is stated without checking the store's own policy, even when the date is correct | SPEC gap (store override verification) | code check | 20 scenarios (21 traces) / 3 | 21 (0.21) | 3 |
| `write_outcome_overstated` | Fail if the reply presents a write as complete, approved or certain beyond the latest tool result, or promises the outcome of a write never performed | RESP-2, ESC-1 | code check + LLM judge | 9 / 8 | 9 (0.09) | 0 |
| `policy_claim_uncited` | Fail if a policy-derived claim is made without its policy id; facts restated from a tool result are excluded | RESP-1 | LLM judge (code can detect ids) | 14 / 6 | 14 (0.14) | 2 |
| `invented_process_or_exception` | Fail if the reply offers an exception, procedure, team, document requirement, remedy or timeline that no tool result or policy contains | RESP-3 (+ RESP-1) | LLM judge with policy corpus | 20 (incl. 4 secondary) / 4 | 20 (0.20) | 2 |

Overall, 46 traces fail at least one mode. Each mode's boundary to its nearest neighbour, its product change and the supporting annotations are recorded in `analysis/state/patterns.json`; earlier versions are in `patterns_history.jsonl`.

## New modes in the final 15

The final 15 uniformly sampled traces produced **one previously unseen consequential failure type**: support-0217. The agent correctly said the return window had closed, then offered a charge dispute as a fallback for a change-of-mind return, which applies `cw-disputes` to a case it doesn't cover. It is recorded as a candidate in `patterns.json` (`new_type_candidates`) but not promoted to a mode, because one trace does not support a definition. The other four failures in the batch fit existing modes. One new type in 15 traces suggests the taxonomy is reasonably stable for this agent and scenario set.

## Taxonomy revision

**support-0137 (cluster batch).** The first open code said "no failure observed": the deadline (July 29) was correct and cited. support-0027 had been coded as a failure for the same step: it gave a deadline without checking whether the store has its own return window. In axial round 1, the student made the two consistent: 0137 was revised to a failure under `store_return_window_not_verified`, and the original open code was kept in the annotation history. The mode boundary now says "Fail even when the date happens to be correct".

A later decision followed from this. The student chose to keep the mode as a **process rule**, evaluated by a code check (no store-policy lookup before stating a window), rather than a harm rule (wrong window given). As a consequence, support-0189 turns 2 and 3 and support-0144 turn 3, open-coded as "no failure", are labelled Fail. Most store-window labels for stores without an override are marked low confidence.

## Rejected groups

- **`ambiguous_match_treated_as_unique`**, dropped. It had one positive (support-0022). The depth search over all unbatched traces found 9 duplicate-title hits; the 5 reviewed (0021, 0060, 0108, 0169, 0184) all listed the matches, asked which one, or used the product id. That leaves fewer than 3 positives, so the record is kept with status `rejected`.
- **`scope_escalation_unclear`**, dissolved. It existed only because SCOPE-2 (refuse card changes) and ESC-2 (account changes go to a human) conflicted. The SPEC revision below resolves the conflict, and support-0225 becomes correct behaviour.

## Rejected search suggestion

Searching for `policy_claim_uncited` flagged **support-0004** because its reply says the refund arrives "within 5 to 10 business days" without a policy id. On review, that sentence restates the `issue_refund` tool result note, not a policy document. The search hit was rejected and the definition revised: facts restated from a tool result are outside the mode. In total, 12 suggestions were rejected: 3 batch-2 open codes, 8 depth search hits and 1 criteria-drift hit.

## SPEC revision

ESC-2 now reads: "Payment-card changes count as account changes: the agent refuses to receive or handle card details (SCOPE-2) and offers escalation to a human (ESC-2)." The motivating annotation is **support-0225**, which was revised to "no failure observed" under the new wording. `store_return_window_not_verified` still cites a SPEC gap: no requirement yet obliges the agent to verify a store override.

## Open codes vs final labels

- **support-0022, support-0060 and support-0217** have failure notes but fail no active mode: respectively the dropped ambiguous-match mode, an unsupported "in stock" claim, and the new candidate type.
- **support-0189 t2/t3 and support-0144 t3** were open-coded "no failure" but are labelled Fail under the process rule.

## Other notes

- Part C (Raindrop Workshop) was skipped.
- The interface design and its comparison with the reference UI are in `interface_comparison.md`.

## Transparency

The open codes were drafted by a coding agent and stored as **agent suggestions**, never directly as annotations:

- The student accepted the batch-1 and batch-2 suggestions in the review app.
- At the student's explicit request, the agent applied the following decisions itself:
  - three batch-2 accepts changed to rejections (0159, 0156, 0173);
  - all decisions for the depth and final batches;
  - the criteria-drift decisions;
  - all 500 labels.
- The state files record each of these as "agent, applied at student's request (2026-11-10)", with source `agent` and a confidence level.
- Each decision reason, label evidence and Langfuse score comment carries this note.

## Preparing for Homework 5

Every mode has fewer than 30 Fail labels: unreliable_tool_data 11, store_window 21, write_outcome 9, policy_uncited 14, invented_process 20. Each mode has at least 79 Pass labels. All five modes therefore need synthetic scenarios targeting the failure before a judge can be split and validated; `write_outcome_overstated` and `unreliable_tool_data_presented_as_fact` need the most.
