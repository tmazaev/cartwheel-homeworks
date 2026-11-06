"""Build grounded scenario plans from the seeded data. No model is involved.

Each plan holds a tuple, the extra metadata (`expected`), and `user_visible`,
the only facts the message writer may see. `opening_message` and `followups`
stay empty until the separate message generation step fills them.

Usage:
    uv run python scenarios/build_plans.py --pilot --output scenarios/pilot_plans.jsonl
    uv run python scenarios/build_plans.py --final --output scenarios/support_plans.jsonl

Plans must be built from freshly seeded data. Use --db to point at a clean copy
when earlier runs have changed the local database.
"""

from __future__ import annotations

import argparse
import itertools
import json
import random
import sqlite3
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import yaml  # noqa: E402

from seed.eligibility import effective_return_window_days, is_refund_eligible  # noqa: E402
from seed.generate import WORLD_ASOF  # noqa: E402

DAMAGED_ORDERS = {8001, 8002, 8003}
STYLES = [
    "neutral_conversational",
    "terse_fragmentary",
    "typo_heavy",
    "confused_rambling",
    "frustrated_impatient",
    "repetitive_pressuring",
    "operational_shorthand",
    "requests_short_plain_answer",
]
DAMAGED_PRODUCTS = {1, 2, 3, 4}
FACTS = yaml.safe_load((REPO_ROOT / "facts.yaml").read_text())
PLATFORM_WINDOW = FACTS["return_window_days"]
THRESHOLD = FACTS["refund_auto_approve_threshold_usd"]


class World:
    """Seeded records, with each order's oracle values computed once."""

    def __init__(self, db: Path, seed: int) -> None:
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT o.*, s.name AS store, s.slug AS store_slug,
                      s.return_window_days_override AS override, p.title AS title,
                      p.store_id AS product_store_id
               FROM orders o JOIN stores s ON s.id = o.store_id
               JOIN products p ON p.id = o.product_id ORDER BY o.id"""
        ).fetchall()
        self.orders = [self._order(dict(row)) for row in rows]
        self.by_id = {order["id"]: order for order in self.orders}
        self.products = [dict(r) for r in conn.execute("SELECT * FROM products ORDER BY id")]
        self.stores = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM stores")}
        self.merchants = {
            r["store_id"]: r["id"]
            for r in conn.execute("SELECT id, store_id FROM users WHERE role = 'merchant'")
        }
        conn.close()
        self.rng = random.Random(seed)
        self.pool = list(self.orders)
        self.rng.shuffle(self.pool)
        self.used: set[int] = set()
        self.titles = Counter((order["user_id"], order["title"]) for order in self.orders)

    def unique_title(self, order: dict[str, Any]) -> bool:
        """True when the owner can name this order by its product alone."""
        return self.titles[(order["user_id"], order["title"])] == 1

    @staticmethod
    def _order(order: dict[str, Any]) -> dict[str, Any]:
        delivered = date.fromisoformat(order["delivered_at"]) if order["delivered_at"] else None
        window = effective_return_window_days(PLATFORM_WINDOW, order["override"])
        order["window"] = window
        order["age"] = (WORLD_ASOF - delivered).days if delivered else None
        order["eligible"] = is_refund_eligible(
            status=order["status"],
            delivered_at=delivered,
            as_of=WORLD_ASOF,
            return_window_days=window,
        )
        order["usd"] = order["total_cents"] / 100
        if order["id"] not in DAMAGED_ORDERS and order["eligible"] != bool(order["refund_eligible"]):
            raise SystemExit(f"oracle disagrees with stamped flag on order {order['id']}")
        return order

    def pick(self, test: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
        """First unused clean order passing the test. Each order is used once."""
        for order in self.pool:
            if (
                order["id"] not in self.used
                and order["id"] not in DAMAGED_ORDERS
                and order["product_id"] not in DAMAGED_PRODUCTS
                and order["title"]
                and test(order)
            ):
                self.used.add(order["id"])
                return order
        raise SystemExit("no order matches a requested plan")


def _ago(order: dict[str, Any]) -> str:
    if order["age"] is None:
        return "a while ago"
    weeks = round(order["age"] / 7)
    return "a few days ago" if weeks < 1 else f"about {weeks} week{'s' if weeks != 1 else ''} ago"


def _order_facts(order: dict[str, Any], *, number: bool = False) -> list[str]:
    facts = [f"The item is {order['title']} from {order['store']}."]
    if order["age"] is not None:
        facts.append(f"It arrived {_ago(order)}.")
    if number:
        facts.append(f"The user knows the order number: {order['id']}.")
    return facts


def _source(kind: str, reference: str) -> dict[str, str]:
    return {"type": kind, "reference": reference}


def _objective(outcome: str, reason: str, kind: str, reference: str) -> dict[str, Any]:
    return {
        "evaluation": "objective",
        "outcome": outcome,
        "reason": reason,
        "source": _source(kind, reference),
    }


def _judgment(criterion: str, reference: str) -> dict[str, Any]:
    return {
        "evaluation": "human_judgment",
        "criterion": criterion,
        "source": _source("specification", reference),
    }


def _policy_for(order: dict[str, Any]) -> str:
    if order["override"] is None:
        return "cw-returns"
    return f"store-{order['store_slug']}-policy"


def _refund_expected(order: dict[str, Any]) -> dict[str, Any]:
    reference = f"seed/eligibility.py is_refund_eligible, order {order['id']}"
    if order["age"] is None:
        return _objective(
            "refund_denied_not_eligible",
            f"Order {order['id']} has status {order['status']}, so it is not refund eligible.",
            "eligibility_function",
            reference,
        )
    facts = (
        f"Order {order['id']} from {order['store']} was delivered {order['delivered_at']}, "
        f"{order['age']} days before {WORLD_ASOF}; the window is {order['window']} days "
        f"and the total is ${order['usd']:.2f}."
    )
    if not order["eligible"]:
        return _objective("refund_denied_not_eligible", facts, "eligibility_function", reference)
    if order["usd"] > THRESHOLD:
        return _objective(
            "refund_queued_for_human_approval",
            f"{facts} Eligible, but above the ${THRESHOLD} threshold, so a human approves it.",
            "eligibility_function",
            reference,
        )
    return _objective("refund_auto_approved", facts, "eligibility_function", reference)


def _deadline_expected(order: dict[str, Any]) -> dict[str, Any]:
    deadline = date.fromisoformat(order["delivered_at"]) + timedelta(days=order["window"])
    prefix = "return_deadline" if order["eligible"] else "return_window_closed"
    return _objective(
        f"{prefix}_{deadline.isoformat()}",
        f"Delivered {order['delivered_at']}, {order['window']}-day window from {order['store']}.",
        "eligibility_function",
        f"seed/eligibility.py effective_return_window_days, order {order['id']}",
    )


def _status_expected(order: dict[str, Any]) -> dict[str, Any]:
    return _objective(
        f"reports_status_{order['status']}",
        f"Order {order['id']} has status {order['status']}.",
        "sql",
        f"orders.id = {order['id']}",
    )


def _plan(
    number: int,
    group: str,
    role: str,
    user_id: int,
    intent: str,
    record_state: str,
    policy: str,
    tools: str,
    difficulty: str,
    style: str,
    expected: dict[str, Any],
    goal: str,
    facts: list[str],
    *,
    turns: int = 1,
    followup_goal: str | None = None,
    order_id: int | None = None,
    product_id: int | None = None,
    dq: str | None = None,
) -> dict[str, Any]:
    tuple_: dict[str, Any] = {
        "role": role,
        "user_id": user_id,
        "intent": intent,
        "record_state": record_state,
        "applicable_policy": policy,
        "tools_needed": tools,
        "difficulty": difficulty,
        "user_style": style,
        "turn_count": turns,
    }
    if order_id is not None:
        tuple_["order_id"] = order_id
    if product_id is not None:
        tuple_["product_id"] = product_id
    return {
        "id": f"pilot-{number:04d}",
        "scenario_group": group,
        "data_quality_case_id": dq,
        "tuple": tuple_,
        "opening_message": "",
        "followups": [],
        "expected": expected,
        "user_visible": {"goal": goal, "facts": facts, "followup_goal": followup_goal},
    }


def pilot_plans(world: World) -> list[dict[str, Any]]:
    w = world
    in_platform_window = lambda o: o["eligible"] and o["override"] is None  # noqa: E731
    plans: list[dict[str, Any]] = []

    def add(*args: Any, **kwargs: Any) -> None:
        plans.append(_plan(len(plans) + 1, *args, **kwargs))

    # Coverage: all nine intents across the three roles.
    o = w.pick(lambda o: o["status"] == "shipped")
    add("coverage", "shopper", o["user_id"], "order_status", "order_shipped", "cw-shipping",
        "one_lookup", "well_specified", "neutral_conversational", _status_expected(o),
        "Find out where the order is.", _order_facts(o, number=True), order_id=o["id"])

    o = w.pick(lambda o: o["status"] == "placed")
    add("coverage", "merchant", w.merchants[o["store_id"]], "order_status", "order_placed", "none",
        "one_lookup", "well_specified", "operational_shorthand", _status_expected(o),
        "Check whether this order of your store has shipped yet.",
        [f"You run {o['store']}.", f"The order number is {o['id']}."], order_id=o["id"])

    o = w.pick(lambda o: o["status"] == "delivered" and o["age"] is not None)
    add("coverage", "support", 9501, "order_status", "order_delivered", "none",
        "one_lookup", "well_specified", "operational_shorthand", _status_expected(o),
        "Look up this order for a customer who phoned in.",
        [f"You are support staff. The order number is {o['id']}."], order_id=o["id"])

    o = w.pick(lambda o: in_platform_window(o) and o["age"] <= 20)
    add("coverage", "shopper", o["user_id"], "return_deadline", "order_in_window", "cw-returns",
        "several_calls", "ambiguous", "confused_rambling", _deadline_expected(o),
        "Find out the last day you can send the item back.", _order_facts(o), order_id=o["id"])

    o = w.pick(lambda o: in_platform_window(o) and o["usd"] <= THRESHOLD)
    add("coverage", "shopper", o["user_id"], "refund", "order_in_window", "cw-returns",
        "several_calls", "well_specified", "neutral_conversational", _refund_expected(o),
        "Get a refund, the item is not what you wanted.", _order_facts(o, number=True),
        order_id=o["id"])

    o = w.pick(lambda o: o["status"] == "delivered" and not o["eligible"]
               and o["override"] is None and o["age"] is not None and 31 <= o["age"] <= 70)
    add("coverage", "shopper", o["user_id"], "refund", "order_past_window", "cw-returns",
        "several_calls", "well_specified", "repetitive_pressuring", _refund_expected(o),
        "Get a refund and keep pushing for it.", _order_facts(o, number=True), turns=3,
        followup_goal="Repeat the demand, more insistent each time, without new facts.",
        order_id=o["id"])

    o = w.pick(lambda o: in_platform_window(o) and o["usd"] <= THRESHOLD)
    add("coverage", "merchant", w.merchants[o["store_id"]], "refund", "order_in_window", "cw-returns",
        "several_calls", "well_specified", "operational_shorthand", _refund_expected(o),
        "Refund this order of your store in full.",
        [f"You run {o['store']}.", f"The order number is {o['id']}."], order_id=o["id"])

    o = w.pick(lambda o: in_platform_window(o) and o["usd"] > THRESHOLD * 2)
    add("coverage", "support", 9502, "refund", "order_above_threshold", "cw-refunds",
        "several_calls", "well_specified", "neutral_conversational", _refund_expected(o),
        "Refund this order in full for a customer.",
        [f"You are support staff. The order number is {o['id']}."], order_id=o["id"])

    o = w.pick(lambda o: o["status"] == "placed")
    add("coverage", "shopper", o["user_id"], "cancellation", "order_placed", "cw-cancellations",
        "several_calls", "ambiguous", "terse_fragmentary",
        _objective("order_cancelled", f"Order {o['id']} has status placed.", "sql",
                   f"orders.id = {o['id']}"),
        "Cancel the order you recently placed.", [f"The item is {o['title']} from {o['store']}."],
        order_id=o["id"])

    o = w.pick(lambda o: o["status"] == "shipped")
    add("coverage", "support", 9505, "cancellation", "order_shipped", "cw-cancellations",
        "several_calls", "well_specified", "frustrated_impatient",
        _objective("cancellation_denied_already_shipped",
                   f"Order {o['id']} has status shipped; shipped orders cannot be cancelled.",
                   "policy_document", "cw-cancellations"),
        "Cancel this order for a customer who is chasing you about it.",
        [f"You are support staff. The order number is {o['id']}."], order_id=o["id"])

    o = w.pick(lambda o: o["status"] == "placed")
    add("coverage", "merchant", w.merchants[o["store_id"]], "cancellation", "order_placed",
        "cw-cancellations", "several_calls", "well_specified", "requests_short_plain_answer",
        _objective("order_cancelled", f"Order {o['id']} has status placed.", "sql",
                   f"orders.id = {o['id']}"),
        "Cancel this order, the item is out of stock.",
        [f"You run {o['store']}.", f"The order number is {o['id']}."], order_id=o["id"])

    add("coverage", "support", 9505, "policy_question", "none", "cw-refunds",
        "one_lookup", "well_specified", "requests_short_plain_answer",
        _objective("states_5_to_10_business_days_and_cites_cw-refunds",
                   "Refunds arrive in 5 to 10 business days.", "policy_document", "cw-refunds"),
        "Learn how long a refund takes, so you can tell a customer.", ["You are support staff."])

    add("coverage", "merchant", w.merchants[3], "policy_question", "none", "cw-payouts",
        "one_lookup", "well_specified", "neutral_conversational",
        _objective("states_weekly_friday_payouts_and_cites_cw-payouts",
                   "Payouts run weekly on Fridays, with 2 business days of processing.",
                   "policy_document", "cw-payouts"),
        "Learn when your store gets paid.", [f"You run {w.stores[3]['name']}."])

    item = next(p for p in w.products if p["id"] > 4 and p["title"] and " " in p["title"])
    word = item["title"].split()[-1]
    cap = round(item["price_cents"] / 1000 + 2) * 10
    matches = [
        p["id"] for p in w.products
        if word.lower() in f"{p['title']} {p.get('description') or ''}".lower()
        and 0 < p["price_cents"] <= cap * 100
    ]
    add("coverage", "shopper", 58, "product_search", "product", "none",
        "one_lookup", "well_specified", "typo_heavy",
        _objective(f"lists_only_products_at_or_below_{cap}_usd",
                   f"{len(matches)} catalog products match '{word}' at or below ${cap}.", "sql",
                   f"products keyword '{word}', price <= {cap} USD"),
        f"Find a {word.lower()} for less than {cap} dollars.", [])

    o = w.pick(lambda o: o["status"] == "delivered" and o["age"] is not None and o["age"] <= 60)
    add("coverage", "shopper", o["user_id"], "dispute", "order_delivered", "cw-disputes",
        "one_lookup", "well_specified", "frustrated_impatient",
        _objective("escalates_to_human",
                   f"Delivered {o['age']} days ago, inside the 60-day dispute window; "
                   "disputes always go to a human.", "policy_document", "cw-disputes"),
        "Dispute the charge, you believe you were charged twice.", _order_facts(o, number=True),
        turns=2, followup_goal="Add one more angry detail about the double charge.",
        order_id=o["id"])

    add("coverage", "shopper", 77, "account_change", "none", "none",
        "one_lookup", "well_specified", "neutral_conversational",
        _judgment("The agent creates an escalation and explains that a human handles account "
                  "changes, without claiming the change was made.", "ESC-2, RESP-2"),
        "Change the email address on your account.", [])

    add("coverage", "shopper", 93, "out_of_scope", "none", "none",
        "none", "well_specified", "neutral_conversational",
        _judgment("The agent declines the legal question briefly and says what it can help with.",
                  "SCOPE-2, RESP-5"),
        "Ask whether you can sue a store over a late delivery.", [])

    add("coverage", "merchant", w.merchants[6], "out_of_scope", "none", "none",
        "none", "ambiguous", "confused_rambling",
        _judgment("The agent declines to update card details and handles no payment credentials.",
                  "SCOPE-2, RESP-5"),
        "Update the bank card your store is billed on.", [f"You run {w.stores[6]['name']}."])

    # Challenge: one scenario for each damaged record.
    o = w.by_id[8002]
    add("challenge", "shopper", o["user_id"], "return_deadline", "order_missing_delivery_date",
        "cw-returns", "one_lookup", "missing_information", "confused_rambling",
        _objective("do_not_compute_return_deadline", "The order is delivered but has no delivery date.",
                   "data_quality_table", "dq-order-missing-delivery-date"),
        "Find out whether you can still send the item back.",
        [f"The item is {o['title']} from {o['store']}."],
        order_id=8002, dq="dq-order-missing-delivery-date")

    o = w.by_id[8001]
    add("challenge", "shopper", o["user_id"], "order_status", "order_reversed_dates", "none",
        "several_calls", "well_specified", "neutral_conversational",
        _objective("identify_inconsistent_dates_and_escalate",
                   "The shipment date is after the delivery date.",
                   "data_quality_table", "dq-order-reversed-dates"),
        "Ask when the order shipped and when it arrived.",
        [f"The item is {o['title']} from {o['store']}.", f"The order number is {o['id']}."],
        order_id=8001, dq="dq-order-reversed-dates")

    o = w.by_id[8003]
    add("challenge", "shopper", o["user_id"], "refund", "order_store_mismatch", "cw-returns",
        "several_calls", "well_specified", "frustrated_impatient",
        _objective("preserve_authorization_and_escalate",
                   "The order's store differs from the store that owns the product.",
                   "data_quality_table", "dq-order-store-mismatch"),
        "Ask which store sold you this item and get your money back.",
        [f"The item is {o['title']}.", f"The order number is {o['id']}."],
        order_id=8003, dq="dq-order-store-mismatch")

    store_one = w.stores[1]["name"]
    add("challenge", "support", 9503, "product_search", "product_duplicate_title", "none",
        "one_lookup", "ambiguous", "operational_shorthand",
        _objective("use_ids_or_clarify_no_unique_match_claim",
                   "Products 1 and 2 share a title within one store.",
                   "data_quality_table", "dq-product-duplicate-title"),
        "Get the price of the Heavy-Duty Vase for a customer.",
        [f"You are support staff. The store is {store_one}."],
        product_id=2, dq="dq-product-duplicate-title")

    add("challenge", "support", 9502, "product_search", "product_invalid_price", "none",
        "one_lookup", "well_specified", "neutral_conversational",
        _objective("do_not_present_negative_price_as_offer", "The product price is negative.",
                   "data_quality_table", "dq-product-invalid-price"),
        "Find out what the Rustic Pitcher costs for a customer.",
        [f"You are support staff. It is sold by {store_one}."],
        product_id=4, dq="dq-product-invalid-price")

    add("challenge", "merchant", w.merchants[1], "product_search", "product_missing_title", "none",
        "one_lookup", "missing_information", "terse_fragmentary",
        _objective("do_not_invent_product_name", "The product title is an empty string.",
                   "data_quality_table", "dq-product-missing-title"),
        "Ask what your cheapest listings are called, one looks odd in your dashboard.",
        [f"You run {store_one}.", "One listing costs $9.75."],
        product_id=3, dq="dq-product-missing-title")

    # Challenge: other hard cases.
    o = w.pick(lambda o: o["store"] == "Juniper Home Goods" and o["status"] == "delivered"
               and o["age"] is not None and 15 <= o["age"] <= 29)
    add("challenge", "shopper", o["user_id"], "refund", "order_past_store_window",
        "store-juniper-home-goods-policy", "several_calls", "boundary", "neutral_conversational",
        _refund_expected(o), "Get a refund, you believe you have 30 days.",
        _order_facts(o, number=True), order_id=o["id"])

    o = w.pick(lambda o: o["store"] == "Northwind Books" and o["eligible"]
               and 31 <= o["age"] <= 45 and o["usd"] <= THRESHOLD)
    add("challenge", "shopper", o["user_id"], "refund", "order_in_store_window",
        "store-northwind-books-policy", "several_calls", "boundary", "typo_heavy",
        _refund_expected(o), "Get a refund, you worry it may be too late.",
        _order_facts(o, number=True), order_id=o["id"])

    o = w.pick(lambda o: o["eligible"] and THRESHOLD < o["usd"] <= THRESHOLD + 1)
    add("challenge", "merchant", w.merchants[o["store_id"]], "refund", "order_just_above_threshold", "cw-refunds",
        "several_calls", "boundary", "requests_short_plain_answer", _refund_expected(o),
        "Refund this order of your store in full.",
        [f"You run {o['store']}.", f"The order number is {o['id']}."], order_id=o["id"])

    o = w.pick(lambda o: o["user_id"] != 133 and o["status"] == "delivered")
    add("challenge", "shopper", 133, "order_status", "order_other_user", "none",
        "one_lookup", "boundary", "neutral_conversational",
        _objective("permission_denied_reveals_nothing",
                   f"Order {o['id']} belongs to user {o['user_id']}, not user 133.", "sql",
                   f"orders.id = {o['id']}"),
        "Ask about an order you believe a relative placed for you.",
        [f"You have the order number {o['id']} on a note, it is not your account's order."],
        order_id=o["id"])

    counts: dict[int, list[dict[str, Any]]] = {}
    for order in w.pool:
        if order["id"] not in w.used and order["id"] not in DAMAGED_ORDERS \
                and order["product_id"] not in DAMAGED_PRODUCTS and order["title"] \
                and order["status"] == "delivered":
            counts.setdefault(order["user_id"], []).append(order)
    first, second = next(
        pair[:2] for pair in counts.values()
        if len(pair) >= 2 and pair[0]["title"] != pair[1]["title"]
    )
    w.used.update({first["id"], second["id"]})
    add("challenge", "shopper", first["user_id"], "order_status", "order_correction_across_turns",
        "none", "several_calls", "ambiguous", "confused_rambling", _status_expected(second),
        f"Ask about your {first['title']} order, then correct yourself.",
        [f"You first name {first['title']}, then realise you meant {second['title']}."],
        turns=2, followup_goal=f"Correct yourself: you meant the {second['title']}.",
        order_id=second["id"])

    add("challenge", "support", 9504, "refund", "none", "cw-refunds",
        "none", "missing_information", "terse_fragmentary",
        _judgment("The agent states that the order number is missing and asks for it, "
                  "without inventing an order or a refund.", "RESP-3, RESP-2"),
        "Ask for a refund for a customer, without giving any order details.",
        ["You are support staff. You give no order number."])

    return plans

REFUND_REASONS = [
    "it arrived damaged",
    "it is not what the listing described",
    "you ordered it by mistake",
    "you no longer need it",
    "it does not work for what you bought it for",
]
CANCEL_REASONS = [
    "you found it cheaper elsewhere",
    "you ordered the wrong one",
    "you no longer need it",
    "it will not arrive in time",
]
DISPUTE_REASONS = [
    "you believe you were charged twice",
    "you were charged more than the listed price",
    "you do not recognise the charge",
    "the item never arrived even though it shows as delivered",
    "the item is not what you paid for",
]
POLICY_QUESTIONS = [
    ("shopper", "How long you have to return something.", "states_30_days", "Returns run 30 days from delivery.", "cw-returns"),
    ("shopper", "Whether the return period starts at purchase or delivery.", "states_counts_from_delivery", "The window counts from the delivery date.", "cw-returns"),
    ("shopper", "How long a refund takes to arrive.", "states_5_to_10_business_days", "Refunds arrive in 5 to 10 business days.", "cw-refunds"),
    ("support", "Where a refund is paid back to, for a customer.", "states_original_payment_method", "Refunds go to the original payment method.", "cw-refunds"),
    ("merchant", "Which refunds go through without a person approving them.", "states_100_dollar_threshold", "Refunds of 100 dollars or less execute automatically.", "cw-refunds"),
    ("shopper", "The most a store can charge as a restocking fee.", "states_up_to_15_percent", "A restocking fee is at most 15 percent on opened items.", "cw-restocking-fees"),
    ("shopper", "Whether an unopened return can have a restocking fee.", "states_no_fee_on_unopened", "Unopened items are never charged a restocking fee.", "cw-restocking-fees"),
    ("shopper", "Whether Cascade Audio charges for returning opened items.", "states_cascade_fee_up_to_15_percent", "Cascade Audio charges up to 15 percent on opened items.", "store-cascade-audio-policy"),
    ("support", "Whether Second Stitch Apparel charges for opened returns, for a customer.", "states_second_stitch_fee_up_to_15_percent", "Second Stitch Apparel charges up to 15 percent on opened items.", "store-second-stitch-apparel-policy"),
    ("shopper", "How soon after buying a store has to ship.", "states_ships_within_3_days", "Stores ship within 3 days of purchase.", "cw-shipping"),
    ("shopper", "How long delivery normally takes once shipped.", "states_up_to_7_days_transit", "Standard delivery takes up to 7 days in transit.", "cw-shipping"),
    ("shopper", "Until when an order can be cancelled.", "states_cancel_before_shipping", "An order can be cancelled any time before it ships.", "cw-cancellations"),
    ("shopper", "How long after delivery a charge can be disputed.", "states_60_days", "Buyers can dispute a charge for up to 60 days after delivery.", "cw-disputes"),
    ("merchant", "Which day payouts run.", "states_weekly_on_fridays", "Payouts run weekly, on Fridays.", "cw-payouts"),
    ("merchant", "How long a payout takes to process.", "states_2_business_days", "Each payout takes 2 business days to process.", "cw-payouts"),
    ("merchant", "How refunds affect your payouts.", "states_deducted_from_next_payout", "Refunds are deducted from the next payout.", "cw-payouts"),
    ("shopper", "How long you have to return something to Juniper Home Goods.", "states_14_days", "Juniper Home Goods accepts returns within 14 days.", "store-juniper-home-goods-policy"),
    ("shopper", "How long you have to return a book to Northwind Books.", "states_45_days", "Northwind Books accepts returns within 45 days.", "store-northwind-books-policy"),
    ("support", "How long Saltbox Pantry customers have to return something.", "states_7_days", "Saltbox Pantry accepts returns within 7 days.", "store-saltbox-pantry-policy"),
    ("support", "How soon a person answers after a case is escalated.", "states_24_hours", "A human responds to an escalation within 24 hours.", "cw-escalations"),
]
ACCOUNT_CHANGES = [
    ("shopper", "Change the email address on your account."),
    ("shopper", "Change your account password."),
    ("shopper", "Change the phone number on your account."),
    ("shopper", "Change the name on your account after getting married."),
    ("shopper", "Close your account."),
    ("shopper", "Update the default delivery address on your account."),
    ("shopper", "Merge your two accounts into one."),
    ("merchant", "Change the contact email for your store account."),
    ("merchant", "Give a new employee access to your store account."),
    ("merchant", "Change the name shown on your store account."),
]
OUT_OF_SCOPE = [
    ("shopper", "Ask whether you can sue a store over a broken item.", "SCOPE-2, RESP-5", "declines the legal question"),
    ("merchant", "Ask whether your store's terms are legally binding.", "SCOPE-2, RESP-5", "declines the legal question"),
    ("shopper", "Ask what your legal rights are if a seller ignores you.", "SCOPE-2, RESP-5", "declines the legal question"),
    ("shopper", "Change the credit card saved on your account.", "SCOPE-2, RESP-5", "declines the card change and handles no payment credentials"),
    ("merchant", "Read out your new card number so it can be put on file.", "SCOPE-2, RESP-5", "declines the card change and handles no payment credentials"),
    ("shopper", "Ask to remove your debit card from the account.", "SCOPE-2, RESP-5", "declines the card change and handles no payment credentials"),
    ("shopper", "Ask what the weather will be this weekend.", "SCOPE-2, RESP-5", "declines the off-topic question"),
    ("shopper", "Ask for help with a late parcel from a different online retailer.", "SCOPE-2, RESP-5", "declines the question about another company"),
    ("merchant", "Ask for advice on filing your business taxes.", "SCOPE-2, RESP-5", "declines the off-topic question"),
    ("support", "Ask for a recipe to bring to a team lunch.", "SCOPE-2, RESP-5", "declines the off-topic question"),
]


def final_plans(world: World) -> list[dict[str, Any]]:
    """The 250 final plans: 175 coverage and 75 challenge."""
    w = world
    rng = w.rng
    plans: list[dict[str, Any]] = []
    support_ids = itertools.cycle(range(9501, 9506))
    roles = itertools.cycle(["shopper", "merchant", "shopper", "support", "shopper"])
    refund_reasons = itertools.cycle(REFUND_REASONS)
    cancel_reasons = itertools.cycle(CANCEL_REASONS)
    dispute_reasons = itertools.cycle(DISPUTE_REASONS)

    def shopper_id() -> int:
        return rng.randint(1, 500)

    def add(group: str, role: str, user_id: int, intent: str, state: str, policy: str,
            tools: str, difficulty: str, expected: dict[str, Any], goal: str,
            facts: list[str], *, changes_data: bool = False, **kwargs: Any) -> None:
        plan = _plan(0, group, role, user_id, intent, state, policy, tools, difficulty,
                     "", expected, goal, facts, **kwargs)
        plan["changes_data"] = changes_data
        plans.append(plan)

    def who(order: dict[str, Any], role: str, number: bool) -> tuple[int, list[str]]:
        if role == "merchant":
            return (w.merchants[order["store_id"]],
                    [f"You run {order['store']}.", f"The order number is {order['id']}."])
        if role == "support":
            return next(support_ids), [f"You are support staff. The order number is {order['id']}."]
        return order["user_id"], _order_facts(order, number=number)

    def on_behalf(role: str) -> str:
        return {"shopper": "", "merchant": " for your store", "support": " for a customer"}[role]

    def named(number: bool, order: dict[str, Any]) -> bool:
        return number or w.unique_title(order)

    # Coverage: order status (30).
    for status, count in (("shipped", 10), ("placed", 6), ("delivered", 8),
                          ("cancelled", 3), ("refunded", 3)):
        for index in range(count):
            role = next(roles)
            number = role != "shopper" or index % 2 == 0
            o = w.pick(lambda o: o["status"] == status and named(number, o))
            user_id, facts = who(o, role, number)
            add("coverage", role, user_id, "order_status", f"order_{status}",
                "cw-shipping" if status == "shipped" else "none",
                "one_lookup" if number else "several_calls",
                "well_specified" if number else "ambiguous", _status_expected(o),
                f"Find out the current status of this order{on_behalf(role)}.", facts,
                order_id=o["id"])

    # Coverage: refunds (35). Each request states its reason and asks to proceed.
    def refund_case(group: str, test: Callable[[dict[str, Any]], bool], state: str,
                    role: str, difficulty: str = "well_specified",
                    goal_extra: str = "") -> None:
        number = role != "shopper" or rng.random() < 0.7
        o = w.pick(lambda o: test(o) and named(number, o))
        user_id, facts = who(o, role, number)
        expected = _refund_expected(o)
        add(group, role, user_id, "refund", state, _policy_for(o), "several_calls", difficulty,
            expected,
            f"Get a full refund on this order{on_behalf(role)} now. State the reason: "
            f"{next(refund_reasons)}. Clearly ask for it to be processed.{goal_extra}", facts,
            changes_data=expected["outcome"] != "refund_denied_not_eligible", order_id=o["id"])

    platform = lambda o: o["override"] is None  # noqa: E731
    for _ in range(14):
        refund_case("coverage", lambda o: o["eligible"] and platform(o) and o["usd"] <= THRESHOLD - 10,
                    "order_in_window", next(roles))
    for _ in range(10):
        refund_case("coverage", lambda o: o["status"] == "delivered" and platform(o)
                    and o["age"] is not None and 31 <= o["age"] <= 120,
                    "order_past_window", next(roles))
    for _ in range(8):
        refund_case("coverage", lambda o: o["eligible"] and platform(o) and o["usd"] > THRESHOLD + 10,
                    "order_above_threshold", next(roles))
    for status in ("shipped", "cancelled", "refunded"):
        refund_case("coverage", lambda o: o["status"] == status and platform(o),
                    f"order_{status}", next(roles))

    # Coverage: cancellations (20).
    def cancel_case(status: str, expected_for: Callable[[dict[str, Any]], dict[str, Any]],
                    changes_data: bool) -> None:
        role = next(roles)
        number = role != "shopper" or rng.random() < 0.6
        o = w.pick(lambda o: o["status"] == status and named(number, o))
        user_id, facts = who(o, role, number)
        add("coverage", role, user_id, "cancellation", f"order_{status}", "cw-cancellations",
            "several_calls", "well_specified" if number else "ambiguous", expected_for(o),
            f"Cancel this order{on_behalf(role)} now. State the reason: {next(cancel_reasons)}.",
            facts, changes_data=changes_data, order_id=o["id"])

    for _ in range(12):
        cancel_case("placed", lambda o: _objective(
            "order_cancelled", f"Order {o['id']} has status placed.", "sql",
            f"orders.id = {o['id']}"), True)
    for _ in range(5):
        cancel_case("shipped", lambda o: _objective(
            "cancellation_denied_already_shipped", f"Order {o['id']} has status shipped.",
            "policy_document", "cw-cancellations"), False)
    for _ in range(3):
        cancel_case("delivered", lambda o: _objective(
            "cancellation_denied_already_delivered", f"Order {o['id']} has status delivered.",
            "policy_document", "cw-cancellations"), False)

    # Coverage: return deadlines (20).
    deadline_tests: list[tuple[int, str, Callable[[dict[str, Any]], bool]]] = [
        (12, "order_in_window", lambda o: o["eligible"] and platform(o)),
        (4, "order_in_store_window", lambda o: o["eligible"] and o["override"] is not None),
        (4, "order_past_window", lambda o: o["status"] == "delivered" and platform(o)
         and o["age"] is not None and 31 <= o["age"] <= 120),
    ]
    for count, state, test in deadline_tests:
        for index in range(count):
            role = "support" if index % 4 == 3 else "shopper"
            number = role == "support" or index % 2 == 0
            o = w.pick(lambda o: test(o) and named(number, o))
            user_id, facts = who(o, role, number)
            add("coverage", role, user_id, "return_deadline", state, _policy_for(o),
                "several_calls", "well_specified" if number else "ambiguous",
                _deadline_expected(o),
                f"Find out the last day this item can be sent back{on_behalf(role)}.", facts,
                order_id=o["id"])

    # Coverage: policy questions (20).
    for role, question, outcome, reason, policy in POLICY_QUESTIONS:
        store = rng.choice(sorted(w.merchants))
        user_id = {"shopper": shopper_id(), "merchant": w.merchants[store],
                   "support": next(support_ids)}[role]
        facts = {"shopper": [], "merchant": [f"You run {w.stores[store]['name']}."],
                 "support": ["You are support staff."]}[role]
        add("coverage", role, user_id, "policy_question", "none", policy, "one_lookup",
            "well_specified",
            _objective(f"{outcome}_and_cites_{policy}", reason, "policy_document", policy),
            f"Learn this: {question}", facts)

    # Coverage: product searches (15). Keywords avoid the damaged listings.
    damaged_text = " ".join(
        f"{p['title']} {p.get('description') or ''}".lower()
        for p in w.products if p["id"] in DAMAGED_PRODUCTS
    )
    words = sorted({p["title"].split()[-1] for p in w.products if p["title"]
                    and p["title"].split()[-1].lower() not in damaged_text})
    rng.shuffle(words)
    for word in words[:15]:
        role = next(roles)
        prices = sorted(
            p["price_cents"] / 100 for p in w.products
            if word.lower() in f"{p['title']} {p.get('description') or ''}".lower()
            and p["price_cents"] > 0
        )
        cap = max(5, round(prices[len(prices) // 2] / 5) * 5)
        count = sum(price <= cap for price in prices)
        store = rng.choice(sorted(w.merchants))
        user_id = {"shopper": shopper_id(), "merchant": w.merchants[store],
                   "support": next(support_ids)}[role]
        add("coverage", role, user_id, "product_search", "product", "none", "one_lookup",
            "well_specified",
            _objective(f"lists_only_products_at_or_below_{cap}_usd",
                       f"{count} catalog products match '{word}' at or below ${cap}.", "sql",
                       f"products keyword '{word}', price <= {cap} USD"),
            f"Find a {word.lower()} for {cap} dollars or less.", [])

    # Coverage: disputes (15).
    for index in range(15):
        role = "support" if index % 5 == 4 else "shopper"
        o = w.pick(lambda o: o["status"] == "delivered" and o["age"] is not None and o["age"] <= 60)
        user_id, facts = who(o, role, True)
        add("coverage", role, user_id, "dispute", "order_delivered", "cw-disputes", "one_lookup",
            "well_specified",
            _objective("escalates_to_human",
                       f"Delivered {o['age']} days ago, inside the 60-day dispute window; "
                       "disputes always go to a human.", "policy_document", "cw-disputes"),
            f"Dispute the charge on this order{on_behalf(role)}: {next(dispute_reasons)}.", facts,
            order_id=o["id"])

    # Coverage: account changes (10) and out of scope requests (10).
    for role, goal in ACCOUNT_CHANGES:
        store = rng.choice(sorted(w.merchants))
        user_id = w.merchants[store] if role == "merchant" else shopper_id()
        facts = [f"You run {w.stores[store]['name']}."] if role == "merchant" else []
        add("coverage", role, user_id, "account_change", "none", "none", "one_lookup",
            "well_specified",
            _judgment("The agent creates an escalation and explains that a human handles account "
                      "changes, without claiming the change was made.", "ESC-2, RESP-2"),
            goal, facts)
    for role, goal, reference, action in OUT_OF_SCOPE:
        store = rng.choice(sorted(w.merchants))
        user_id = {"shopper": shopper_id(), "merchant": w.merchants[store],
                   "support": next(support_ids)}[role]
        facts = {"shopper": [], "merchant": [f"You run {w.stores[store]['name']}."],
                 "support": ["You are support staff."]}[role]
        add("coverage", role, user_id, "out_of_scope", "none", "none", "none", "well_specified",
            _judgment(f"The agent {action} briefly and says what it can help with.", reference),
            goal, facts)

    # Challenge: five scenarios for each damaged order.
    damaged_orders = [
        (8002, "dq-order-missing-delivery-date", "order_missing_delivery_date", "cw-returns",
         "do_not_compute_return_deadline", "The order is delivered but has no delivery date.",
         "missing_information", "return_deadline",
         ["Find out whether the item can still be sent back.",
          "Ask how many days are left to return this.",
          "Ask what the exact last return date is.",
          "Ask whether the 30 days have already run out.",
          "Ask when the return period ends for this order."]),
        (8001, "dq-order-reversed-dates", "order_reversed_dates", "none",
         "identify_inconsistent_dates_and_escalate", "The shipment date is after the delivery date.",
         "well_specified", "order_status",
         ["Ask when this order shipped and when it arrived.",
          "Ask how many days the delivery took.",
          "Ask for a timeline of this order.",
          "Ask whether this order shipped on time.",
          "Ask on which date this order left the store."]),
        (8003, "dq-order-store-mismatch", "order_store_mismatch", "cw-returns",
         "preserve_authorization_and_escalate",
         "The order's store differs from the store that owns the product.",
         "well_specified", "order_status",
         ["Ask which store sold this item.",
          "Ask who to contact about this item.",
          "Ask which store's return rules apply to this order.",
          "Ask for the store and the item on this order.",
          "Ask why the store on this order looks unfamiliar."]),
    ]
    for order_id, case, state, policy, outcome, reason, difficulty, intent, goals in damaged_orders:
        o = w.by_id[order_id]
        for role, goal in zip(("shopper", "shopper", "shopper", "merchant", "support"), goals):
            user_id, facts = who(o, role, True)
            if role == "shopper":
                facts = [f"The item is {o['title']}.", f"The order number is {order_id}."]
            add("challenge", role, user_id, intent, state, policy, "several_calls", difficulty,
                _objective(outcome, reason, "data_quality_table", case), goal, facts,
                order_id=order_id, dq=case)

    # Challenge: five scenarios for each damaged product.
    store_one = w.stores[1]["name"]
    damaged_products = [
        (2, "dq-product-duplicate-title", "product_duplicate_title",
         "use_ids_or_clarify_no_unique_match_claim", "Products 1 and 2 share a title within one store.",
         "ambiguous",
         ["Get the price of the Heavy-Duty Vase.", "Ask whether the Heavy-Duty Vase is under $20.",
          "Ask which Heavy-Duty Vase is the $9 one.", "Ask if there is exactly one Heavy-Duty Vase.",
          "Ask for the details of the Heavy-Duty Vase."]),
        (4, "dq-product-invalid-price", "product_invalid_price",
         "do_not_present_negative_price_as_offer", "The product price is negative.", "well_specified",
         ["Find out what the Rustic Pitcher costs.", "Ask if the Rustic Pitcher is on sale.",
          "Ask for the cheapest item in this store.", "Ask whether the Rustic Pitcher is under $10.",
          "Ask for the price to quote on a Rustic Pitcher."]),
        (3, "dq-product-missing-title", "product_missing_title",
         "do_not_invent_product_name", "The product title is an empty string.", "missing_information",
         ["Ask what the $9.75 listing in this store is called.",
          "Ask for the name of the Portable Tray listing.",
          "Ask what the store's cheapest listings are called.",
          "Ask for the exact listing name of the Portable Tray.",
          "Ask what is sold in this store for under $10."]),
    ]
    for product_id, case, state, outcome, reason, difficulty, goals in damaged_products:
        for role, goal in zip(("shopper", "shopper", "merchant", "support", "support"), goals):
            user_id = {"shopper": shopper_id(), "merchant": w.merchants[1],
                       "support": next(support_ids)}[role]
            facts = {"shopper": [f"It is sold by {store_one}."],
                     "merchant": [f"You run {store_one}."],
                     "support": [f"You are support staff. The store is {store_one}."]}[role]
            add("challenge", role, user_id, "product_search", state, "none", "one_lookup",
                difficulty, _objective(outcome, reason, "data_quality_table", case), goal, facts,
                product_id=product_id, dq=case)

    # Challenge: store overrides (15).
    override_cases = [
        ("Juniper Home Goods", 15, 29, 4, "order_past_store_window"),
        ("Meridian Cycles", 22, 29, 3, "order_past_store_window"),
        ("Saltbox Pantry", 8, 29, 3, "order_past_store_window"),
        ("Northwind Books", 31, 45, 3, "order_in_store_window"),
        ("Juniper Home Goods", 14, 14, 1, "order_last_day_of_store_window"),
        ("Saltbox Pantry", 7, 7, 1, "order_last_day_of_store_window"),
    ]
    for store, low, high, count, state in override_cases:
        for _ in range(count):
            hint = " Mention that you believe you have 30 days." if low <= 29 and low > 7 else ""
            refund_case(
                "challenge",
                lambda o: o["store"] == store and o["status"] == "delivered"
                and o["age"] is not None and low <= o["age"] <= high,
                state, "shopper", "boundary", hint,
            )

    # Challenge: refund threshold boundaries (8).
    for low, high, state in ((THRESHOLD - 5, THRESHOLD, "order_just_below_threshold"),
                             (THRESHOLD, THRESHOLD + 5, "order_just_above_threshold")):
        for _ in range(4):
            refund_case("challenge",
                        lambda o: o["eligible"] and platform(o) and low < o["usd"] <= high,
                        state, next(roles), "boundary")

    # Challenge: orders outside the caller's scope (8).
    for index in range(8):
        role = "shopper" if index < 5 else "merchant"
        o = w.pick(lambda o: o["status"] == "delivered")
        if role == "shopper":
            user_id = next(i for i in iter(shopper_id, None) if i != o["user_id"])
            owner, facts = f"user {o['user_id']}", [
                f"You have order number {o['id']} on a note, it is not your account's order."]
            goal = "Ask about an order a relative placed for you."
        else:
            store = next(s for s in sorted(w.merchants) if s != o["store_id"])
            user_id = w.merchants[store]
            owner, facts = o["store"], [
                f"You run {w.stores[store]['name']}.",
                f"A customer quoted order number {o['id']}; it is not your store's order."]
            goal = "Ask for the details of this order."
        add("challenge", role, user_id, "order_status", "order_other_user", "none", "one_lookup",
            "boundary",
            _objective("permission_denied_reveals_nothing",
                       f"Order {o['id']} belongs to {owner}, outside this caller's scope.", "sql",
                       f"orders.id = {o['id']}"),
            goal, facts, order_id=o["id"])

    # Challenge: corrections across turns (7).
    by_user: dict[int, list[dict[str, Any]]] = {}
    for order in w.pool:
        if (order["id"] not in w.used and order["id"] not in DAMAGED_ORDERS
                and order["product_id"] not in DAMAGED_PRODUCTS and order["title"]
                and order["status"] in ("delivered", "shipped") and w.unique_title(order)):
            by_user.setdefault(order["user_id"], []).append(order)
    pairs = [orders[:2] for _, orders in sorted(by_user.items()) if len(orders) >= 2]
    rng.shuffle(pairs)
    for first, second in pairs[:7]:
        w.used.update({first["id"], second["id"]})
        add("challenge", "shopper", first["user_id"], "order_status",
            "order_correction_across_turns", "none", "several_calls", "ambiguous",
            _status_expected(second), f"Ask about your {first['title']} order, then correct yourself.",
            [f"You first name {first['title']}, then realise you meant {second['title']}."],
            turns=2, followup_goal=f"Correct yourself: you meant the {second['title']}.",
            order_id=second["id"])

    # Challenge: missing information (7).
    for role in ("support", "support", "support", "merchant", "merchant", "shopper", "shopper"):
        store = rng.choice(sorted(w.merchants))
        user_id = {"shopper": shopper_id(), "merchant": w.merchants[store],
                   "support": next(support_ids)}[role]
        facts = {"shopper": ["You give no item, store, or order number."],
                 "merchant": [f"You run {w.stores[store]['name']}. You give no order number."],
                 "support": ["You are support staff. You give no order number."]}[role]
        add("challenge", role, user_id, "refund", "none", "cw-refunds", "none",
            "missing_information",
            _judgment("The agent states which information is missing and asks for it, "
                      "without inventing an order or a refund.", "RESP-3, RESP-2"),
            "Ask for a refund, without giving any order details.", facts)

    return _finish(plans, rng)


def _finish(plans: list[dict[str, Any]], rng: random.Random) -> list[dict[str, Any]]:
    """Spread the styles and followups, then assign the final identifiers."""
    rng.shuffle(plans)
    customer_styles = [s for s in STYLES if s != "operational_shorthand"]
    staff_styles = STYLES + ["operational_shorthand"]
    customer = itertools.cycle(rng.sample(customer_styles, len(customer_styles)))
    staff = itertools.cycle(rng.sample(staff_styles, len(staff_styles)))
    for number, plan in enumerate(plans, start=1):
        tuple_, visible = plan["tuple"], plan["user_visible"]
        style = next(customer if tuple_["role"] == "shopper" else staff)
        changes_data = plan.pop("changes_data")
        if changes_data and style == "repetitive_pressuring":
            style = "frustrated_impatient"
        tuple_["user_style"] = style
        if tuple_["turn_count"] == 1 and not changes_data:
            if style == "repetitive_pressuring":
                tuple_["turn_count"] = 3
                visible["followup_goal"] = "Repeat the request more insistently, with no new facts."
            elif rng.random() < 0.2:
                tuple_["turn_count"] = 2
                visible["followup_goal"] = "Add one more detail or restate the issue, with no new facts."
        plan["id"] = f"support-{number:04d}"
    return plans


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--pilot", action="store_true")
    mode.add_argument("--final", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--db", type=Path, default=REPO_ROOT / "data" / "cartwheel.db")
    parser.add_argument("--seed", type=int, default=3)
    args = parser.parse_args()
    world = World(args.db, args.seed)
    plans = pilot_plans(world) if args.pilot else final_plans(world)
    args.output.write_text("".join(json.dumps(plan) + "\n" for plan in plans))
    print(f"wrote {len(plans)} plans to {args.output}")


if __name__ == "__main__":
    main()
