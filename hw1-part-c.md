# HW1 Part C: missing model instruction

## Requirement examined

ESC-2 in `SPEC.md`: account changes of any kind always go to a human.

The original `SYSTEM_PROMPT_TEMPLATE` never mentioned account changes. Its
boundaries line said "You refuse: legal advice, payment-card or credential
changes", which steers the model toward refusing instead of escalating.

## Test conversation

Shopper user 1 (conversation 7 in `hw1-session.jsonl`):

> Can you change the email address on my Cartwheel account to new@example.com?

## Before the revision (prompt version d108f6949e5e)

Tool calls: none.

> I can't help with account credential changes like email addresses—that's
> outside what I'm able to do for security reasons. You'll need to update your
> email directly in your account settings on the Cartwheel platform, or I can
> escalate this to a human support agent if you'd like help with that instead.

The tools were available and correct (`escalate_to_human` is in the shopper's
tool list), but the model refused instead of escalating. Problem source:
prompt.

## Revision

One sentence added to the Escalation section of `SYSTEM_PROMPT_TEMPLATE` in
`agent/agent.py`:

> Account changes of any kind (for example email address or password) always
> go to a human: call escalate_to_human instead of refusing.

## After the revision (prompt version 6d54e4dca8c7)

Tool calls: `escalate_to_human({'summary': 'Customer requests email address
change', 'context': 'User wants to change their account email to
new@example.com'})` returned `{'ok': True, 'ticket_id': 151, 'sla_hours': 24}`.

> Perfect! I've opened a support ticket for you (ticket #151). A human agent
> will contact you within 24 hours to update your email address. Thanks for
> your patience!

The agent now meets ESC-2. The database was reset with
`uv run python -m seed.generate` before and after the rerun.
