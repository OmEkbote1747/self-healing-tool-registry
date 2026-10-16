"""Deterministic synthetic seed data for the ADAPTIX demo schema (single source of truth).

Everything here is invented: fictional people, example.com addresses, made-up products. The same module
generates database/init_demo.sql (python -m database.build_init_sql) AND provides the expected business
results that ADAPTIX verifies after a recovery, so verification never trusts the database being repaired.
No randomness: identical output on every run.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

FIRST = ["Aarav", "Bianca", "Chidi", "Dana", "Elif", "Farhan", "Greta", "Hiro", "Isla", "Jonah",
         "Kavya", "Liam", "Mira", "Nikolai", "Odette", "Pranav", "Quinn", "Rosa", "Soren", "Tanvi"]
LAST = ["Kapoor", "Moretti", "Okafor", "Levin", "Yilmaz", "Rahman", "Lindqvist", "Tanaka", "Fraser", "Whitlock",
        "Iyer", "Brennan", "Castell", "Volkov", "Marchetti", "Deshmukh", "Harlow", "Navarro", "Eklund", "Joshi"]
CITIES = ["Pune", "Mumbai", "Bengaluru", "Hyderabad", "Chennai", "Delhi", "Kolkata", "Ahmedabad", "Jaipur", "Kochi"]
CUSTOMER_STATUS = ["active", "active", "active", "active", "inactive", "active", "active", "suspended", "active",
                   "active"]

PRODUCTS = [  # name, category, price, stock
    ("Aurora Desk Lamp", "Home", "1299.00", 120), ("Nimbus Wireless Mouse", "Electronics", "899.00", 200),
    ("Terra Ceramic Mug", "Kitchen", "349.00", 180), ("Vertex Notebook A5", "Stationery", "199.00", 160),
    ("Pulse Fitness Band", "Electronics", "2499.00", 75), ("Harbor Cotton Tee", "Apparel", "599.00", 140),
    ("Summit Water Bottle", "Outdoors", "749.00", 110), ("Cobalt USB-C Hub", "Electronics", "1899.00", 60),
    ("Linen Table Runner", "Home", "899.00", 50), ("Granite Chef Knife", "Kitchen", "1599.00", 45),
    ("Breeze Desk Fan", "Home", "2199.00", 40), ("Quill Gel Pens (Pack of 10)", "Stationery", "249.00", 220),
    ("Trailblazer Backpack", "Outdoors", "2799.00", 55), ("Orbit Bluetooth Speaker", "Electronics", "3499.00", 35),
    ("Meadow Throw Pillow", "Home", "699.00", 90)]
ORDER_STATUS = ["delivered", "delivered", "shipped", "paid", "delivered", "pending", "delivered", "cancelled",
                "shipped", "delivered"]
PAYMENT = ["card", "upi", "netbanking", "wallet", "cod"]
EVENT_TYPES = ["restock", "sale", "adjustment", "return", "restock", "sale"]


def customers() -> list[dict]:
    out = []
    for i in range(20):
        out.append({"customer_id": f"C{i + 1:03d}", "full_name": f"{FIRST[i]} {LAST[i]}",
                    "email": f"{FIRST[i].lower()}.{LAST[i].lower()}@example.com", "city": CITIES[i % len(CITIES)],
                    "created_at": datetime(2025, 1 + i % 12, 3 + i, 9, 30, tzinfo=timezone.utc),
                    "customer_status": CUSTOMER_STATUS[i % len(CUSTOMER_STATUS)]})
    return out


def products() -> list[dict]:
    return [{"product_id": f"P{i + 1:03d}", "product_name": n, "category": c, "unit_price": Decimal(p),
             "stock_quantity": s, "is_active": i != 13} for i, (n, c, p, s) in enumerate(PRODUCTS)]


def orders_and_items() -> tuple[list[dict], list[dict]]:
    prods = products()
    orders, items = [], []
    item_id = 1
    for i in range(40):
        oid = f"O{1001 + i}"
        cust = f"C{(i * 7) % 20 + 1:03d}"
        lines = []
        for k in range(1 + i % 3):
            p = prods[(i * 3 + k * 5) % 15]
            qty = (i + k) % 4 + 1
            line = (p["unit_price"] * qty).quantize(Decimal("0.01"))
            lines.append({"item_id": item_id, "order_id": oid, "product_id": p["product_id"], "quantity": qty,
                          "unit_price": p["unit_price"], "line_total": line})
            item_id += 1
        items += lines
        orders.append({"order_id": oid, "customer_id": cust, "order_date": date(2026, 1, 3) + timedelta(days=i * 6),
                       "order_status": ORDER_STATUS[i % len(ORDER_STATUS)],
                       "total_amount": sum((x["line_total"] for x in lines), Decimal("0.00")),
                       "payment_method": PAYMENT[i % len(PAYMENT)]})
    return orders, items


def inventory_events() -> list[dict]:
    out = []
    for i in range(30):
        t = EVENT_TYPES[i % len(EVENT_TYPES)]
        qty = {"restock": 25 + i, "sale": -(1 + i % 4), "adjustment": -2 if i % 2 else 3, "return": 1 + i % 2}[t]
        out.append({"event_id": i + 1, "product_id": f"P{(i * 4) % 15 + 1:03d}", "event_type": t,
                    "quantity_change": qty, "event_time": datetime(2026, 1, 5, 10, tzinfo=timezone.utc)
                    + timedelta(days=i * 8, hours=i % 7),
                    "notes": {"restock": "Supplier delivery", "sale": "Store sale", "adjustment": "Stock count",
                              "return": "Customer return"}[t]})
    return out


# ---------------------------------------------------------------- expected business results (independent)
def expected_customer_orders(customer_id: str) -> list[dict]:
    orders, _ = orders_and_items()
    rows = [o for o in orders if o["customer_id"] == customer_id]
    rows.sort(key=lambda o: (o["order_date"], o["order_id"]))
    return [{"order_id": o["order_id"], "order_date": o["order_date"].isoformat(), "order_status": o["order_status"],
             "total_amount": str(o["total_amount"])} for o in rows]


def expected_order(order_id: str) -> dict:
    orders, items = orders_and_items()
    o = next(x for x in orders if x["order_id"] == order_id)
    names = {p["product_id"]: p["product_name"] for p in products()}
    its = [{"item_id": x["item_id"], "product_id": x["product_id"], "product_name": names[x["product_id"]],
            "quantity": x["quantity"], "unit_price": str(x["unit_price"]), "line_total": str(x["line_total"])}
           for x in items if x["order_id"] == order_id]
    return {"order_id": o["order_id"], "customer_id": o["customer_id"], "order_status": o["order_status"],
            "payment_method": o["payment_method"], "total_amount": str(o["total_amount"]), "items": its}


def expected_order_total(order_id: str) -> dict:
    o = expected_order(order_id)
    return {"stored_total": o["total_amount"], "computed_total": str(sum(Decimal(i["line_total"]) for i in o["items"])),
            "item_count": len(o["items"])}


def order_lines(order_id: str) -> list[tuple[str, int]]:
    _, items = orders_and_items()
    return [(x["product_id"], x["quantity"]) for x in items if x["order_id"] == order_id]
