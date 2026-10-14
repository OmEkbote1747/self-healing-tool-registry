"""Synthetic schema-drift test cases.

125 cases = 5 categories x 25. Every case is a tool (v1 schema + the args the
agent holds) plus a drifted v2 schema, and the outcome a correct system must
reach: "repair" (task should succeed) or "stop" (system must refuse and report).
"""
from __future__ import annotations

from dataclasses import dataclass, field

# approved configuration the healer may read header values from
APPROVED_CONFIG = {"x-api-key": "demo-key-123", "x-client-version": "2.0"}


@dataclass
class Case:
    id: str
    category: str
    tool: str
    path: str
    v1: dict[str, str]
    args: dict
    v2: dict[str, str]
    v2_headers: list[str] = field(default_factory=list)
    expected: str = "repair"            # "repair" | "stop"
    expected_body: dict | None = None   # what a correct repair sends
    note: str = ""


# tool name, path, v1 schema, the agent's args
TOOLS = [
    ("update_user", "/users/update", {"user_id": "str", "email": "str"},
     {"user_id": "123", "email": "asha@example.com"}),
    ("create_order", "/orders", {"order_id": "str", "sku": "str", "quantity": "str"},
     {"order_id": "A-77", "sku": "SKU-9", "quantity": "3"}),
    ("send_message", "/messages", {"channel_id": "str", "text": "str"},
     {"channel_id": "C42", "text": "Build is green"}),
    ("book_room", "/rooms/book", {"room_id": "str", "guests": "str", "date": "str"},
     {"room_id": "R-12", "guests": "4", "date": "2026-10-20"}),
    ("refund_payment", "/payments/refund", {"payment_id": "str", "amount": "str"},
     {"payment_id": "pay_881", "amount": "19.99"}),
]

RENAMES = {  # field -> 4 realistic new names
    "user_id": ["userId", "uid", "user_identifier", "account_user_id"],
    "order_id": ["orderId", "order_ref", "order_number", "orderID"],
    "channel_id": ["channelId", "channel", "chan_id", "channel_ref"],
    "room_id": ["roomId", "room", "room_number", "room_ref"],
    "payment_id": ["paymentId", "payment_ref", "txn_payment_id", "payment"],
}
NEST = {  # field -> (new dotted path, new type)
    "user_id": ("target.id", "int"), "order_id": ("order.id", "str"),
    "channel_id": ("destination.channel_id", "str"), "room_id": ("room.id", "str"),
    "payment_id": ("payment.id", "str"),
}
TYPED = {  # field -> new types its value converts to cleanly
    "user_id": ["int", "float"], "quantity": ["int", "float"], "guests": ["int", "float"], "amount": ["float"],
}
NEW_REQUIRED = ["reason", "currency", "priority", "approved_by", "region"]


def _conv(v, t):
    return {"int": int, "float": float, "str": str, "bool": bool}[t](v)


def _set(d: dict, path: str, value):
    parts = path.split(".")
    for p in parts[:-1]:
        d = d.setdefault(p, {})
    d[parts[-1]] = value


def _body(schema: dict[str, str], values: dict[str, object]) -> dict:
    out: dict = {}
    for path in schema:
        _set(out, path, values[path])
    return out


def build_cases() -> list[Case]:
    cases: list[Case] = []
    n = 0

    def add(**kw):
        nonlocal n
        n += 1
        cases.append(Case(id=f"C{n:03d}", **kw))

    # 1. field rename: 20 clean renames + 5 ambiguous (two ids renamed to opaque names -> must stop)
    for tool, path, v1, args in TOOLS:
        key = next(k for k in v1 if k in RENAMES)
        for new in RENAMES[key]:
            v2 = {(new if k == key else k): t for k, t in v1.items()}
            vals = {(new if k == key else k): args[k] for k in v1}
            add(category="Field rename", tool=tool, path=path, v1=v1, args=args, v2=v2,
                expected_body=_body(v2, vals), note=f"{key} -> {new}")
        a, b = list(v1)[:2]
        v2 = {"ref_a": "str", "ref_b": "str", **{k: t for k, t in v1.items() if k not in (a, b)}}
        add(category="Field rename", tool=tool, path=path, v1=v1, args=args, v2=v2, expected="stop",
            note=f"{a}, {b} -> ref_a, ref_b (ambiguous)")

    # 2. type change: 20 clean conversions + 5 unconvertible values (must stop)
    typed_tools = [t for t in TOOLS if any(k in TYPED for k in t[2])]
    i = 0
    while sum(c.category == "Type change" for c in cases) < 20:
        tool, path, v1, args = typed_tools[i % len(typed_tools)]
        key = next(k for k in v1 if k in TYPED)
        new_t = TYPED[key][(i // len(typed_tools)) % len(TYPED[key])]
        v2 = {**v1, key: new_t}
        vals = {k: (_conv(args[k], v2[k]) if k == key else args[k]) for k in v1}
        add(category="Type change", tool=tool, path=path, v1=v1, args=args, v2=v2,
            expected_body=_body(v2, vals), note=f'{key}: "{args[key]}" -> {new_t}')
        i += 1
    for tool, path, v1, args in TOOLS:
        key = list(v1)[0]
        bad = {**args, key: args[key] + "-x"}  # e.g. "123-x" cannot become an int
        add(category="Type change", tool=tool, path=path, v1=v1, args=bad, v2={**v1, key: "int"}, expected="stop",
            note=f'{key}: "{bad[key]}" -> int (not convertible)')

    # 3. nested field change: 5 per tool, the id moves into an object (plus extra variants)
    for tool, path, v1, args in TOOLS:
        key = next(k for k in v1 if k in NEST)
        base_new, base_t = NEST[key]
        variants = [(base_new, "str"), (base_new, base_t if args[key].isdigit() else "str"),
                    ("data." + key, "str"), ("payload.ids." + key, "str"), ("meta." + key, "str")]
        for new, t in variants:
            v2 = {(new if k == key else k): (t if k == key else tt) for k, tt in v1.items()}
            vals = {(new if k == key else k): (_conv(args[k], t) if k == key else args[k]) for k in v1}
            add(category="Nested field change", tool=tool, path=path, v1=v1, args=args, v2=v2,
                expected_body=_body(v2, vals), note=f"{key} -> {new} ({t})")

    # 4. new required header: 20 from approved config, 5 with no approved value (must stop)
    for tool, path, v1, args in TOOLS:
        for h in ["x-api-key", "x-client-version", "x-api-key", "x-client-version"]:
            add(category="New required header", tool=tool, path=path, v1=v1, args=args, v2=dict(v1), v2_headers=[h],
                expected_body=_body(v1, args), note=f"needs {h} (in approved config)")
        add(category="New required header", tool=tool, path=path, v1=v1, args=args, v2=dict(v1),
            v2_headers=["x-tenant-id"], expected="stop", note="needs x-tenant-id (not in config)")

    # 5. missing value: a new required field nobody can supply -> must stop, never invent
    for tool, path, v1, args in TOOLS:
        for extra in NEW_REQUIRED:
            add(category="Missing value", tool=tool, path=path, v1=v1, args=args, v2={**v1, extra: "str"},
                expected="stop", note=f"new required field '{extra}'")

    assert len(cases) == 125, len(cases)
    return cases


CATEGORIES = ["Field rename", "Type change", "Nested field change", "New required header", "Missing value"]

# the worked example from the slides
DEMO_CASE = Case(id="DEMO", category="Worked example", tool="update_user", path="/users/update",
                 v1={"user_id": "str"}, args={"user_id": "123"},
                 v2={"target.id": "int"}, v2_headers=["x-api-key"],
                 expected_body={"target": {"id": 123}},
                 note='{"user_id": "123"} -> {"target": {"id": 123}} + X-Api-Key')


def build_stress_cases() -> list[Case]:
    """Harder cases outside the 125, kept separate to show where the system breaks."""
    E = {"user_id": "123", "email": "asha@example.com"}
    raw = [
        ("Different meaning, one candidate", {"user_id": "str", "email": "str"}, E,
         {"user_id": "str", "contact.phone": "str"}, [], "stop", None, "email removed, contact.phone required"),
        ("Different meaning, similar name", {"channel_id": "str", "text": "str"}, {"channel_id": "C42", "text": "hi"},
         {"thread_id": "str", "text": "str"}, [], "stop", None, "channel_id removed, thread_id required"),
        ("Synonym with no shared words", {"user_id": "str", "email": "str"}, E,
         {"customer_ref": "str", "email": "str"}, [], "repair", {"customer_ref": "123", "email": "asha@example.com"},
         "user_id -> customer_ref"),
        ("Synonym with no shared words", {"sku": "str", "quantity": "str"}, {"sku": "SKU-9", "quantity": "3"},
         {"sku": "str", "count": "int"}, [], "repair", {"sku": "SKU-9", "count": 3}, "quantity -> count (int)"),
        ("Unit change hidden in schema", {"payment_id": "str", "amount": "str"}, {"payment_id": "p1", "amount": "20"},
         {"payment_id": "str", "amount_cents": "int"}, [], "stop", None, "amount (rupees) -> amount_cents"),
        ("Lossy conversion", {"payment_id": "str", "amount": "str"}, {"payment_id": "p1", "amount": "19.99"},
         {"payment_id": "str", "amount": "int"}, [], "stop", None, '"19.99" -> int would lose data'),
        ("Two renames at once", {"user_id": "str", "email": "str"}, E,
         {"userId": "str", "emailAddress": "str"}, [], "repair", {"userId": "123", "emailAddress": "asha@example.com"},
         "user_id -> userId, email -> emailAddress"),
        ("Bool conversion", {"user_id": "str", "active": "str"}, {"user_id": "123", "active": "true"},
         {"user_id": "str", "active": "bool"}, [], "repair", {"user_id": "123", "active": True}, '"true" -> bool'),
        ("Rename + nest + type + header", {"user_id": "str"}, {"user_id": "123"},
         {"target.id": "int"}, ["x-api-key"], "repair", {"target": {"id": 123}}, "the worked example"),
        ("Header not in approved config", {"user_id": "str"}, {"user_id": "123"},
         {"user_id": "str"}, ["authorization"], "stop", None, "needs a login token we must not guess"),
    ]
    return [Case(id=f"S{i + 1:02d}", category=cat, tool="tool", path="/endpoint", v1=v1, args=args, v2=v2,
                 v2_headers=h, expected=exp, expected_body=eb, note=note)
            for i, (cat, v1, args, v2, h, exp, eb, note) in enumerate(raw)]
