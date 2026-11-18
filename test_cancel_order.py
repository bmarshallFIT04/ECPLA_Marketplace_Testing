"""Checks the Cancel Order feature: buyers cancelling pending orders, and sellers' cancellation policies.

Run from the project folder:  python test_cancel_order.py
It uses a temporary database, so your real orders are not touched.
"""
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta

import database

# Point the app at a throwaway database BEFORE the app is imported
_tmp = tempfile.mkdtemp()
database.DB_PATH = os.path.join(_tmp, "test.db")
database.init_db()

import app as shop  # noqa: E402

shop.app.secret_key = "test-only"
client = shop.app.test_client()
results = []

SELLER, OTHER_SELLER, BUYER, OTHER_BUYER = 1, 2, 9, 10
ADDRESS = {"full_name": "Test Buyer", "address": "1 Main St"}


def check(description, passed):
    results.append(passed)
    print(f"  {'PASS' if passed else 'FAIL'}  {description}")


def login(user_id, role):
    with client.session_transaction() as session:
        session["user_id"] = user_id
        session["role"] = role


def page(url):
    return client.get(url).get_data(as_text=True)


def stock(pid):
    return database.get_product(pid)["stock"]


def new_order(pid, quantity=2, hours_ago=0):
    """Places an order as BUYER, optionally backdating it, and returns its ID."""
    database.add_order(BUYER, pid, quantity, ADDRESS)
    con = sqlite3.connect(database.DB_PATH)
    order_id = con.execute("SELECT MAX(order_id) FROM orders").fetchone()[0]
    if hours_ago:
        placed = (datetime.now() - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")
        con.execute("UPDATE orders SET date = ? WHERE order_id = ?", (placed, order_id))
        con.commit()
    con.close()
    return order_id


def cancel(order_id):
    return client.post(f"/orders/{order_id}/cancel")


def set_policy(**form):
    return client.post("/seller/cancellation-policy", data=form)


database.add_product(SELLER, "Teapot", "ceramic", 20.0, 10)
teapot = database.list_products_by_seller(SELLER)[0][0]

print("\nBUYER CANCELS A PENDING ORDER")
order = new_order(teapot, quantity=3)
check("Placing the order takes 3 out of stock", stock(teapot) == 7)
login(BUYER, "buyer")
detail = page(f"/orders/{order}")
check("Order page shows a Cancel Order button", "Cancel Order</button>" in detail)
check("By default buyers can cancel until it ships", "until it ships" in detail)
response = cancel(order)
check("Cancelling redirects back to the order page", response.status_code == 302)
check("Order status is now Cancelled", database.get_order_details(order)["status"] == "Cancelled")
check("Cancelled quantity goes back into stock", stock(teapot) == 10)
check("Shipment history shows the cancellation", database.get_order_updates(order)[0]["status"] == "Cancelled")
detail = page(f"/orders/{order}")
check("Order page confirms the cancellation", f"Order #{order} has been cancelled." in detail)
check("Cancel button is gone after cancelling", "Cancel Order</button>" not in detail)
check("Cancelling twice is refused", cancel(order).status_code == 400 and stock(teapot) == 10)

print("\nWHO CAN CANCEL")
order = new_order(teapot)
login(OTHER_BUYER, "buyer")
check("Another buyer cannot cancel it (404)", cancel(order).status_code == 404)
login(SELLER, "seller")
check("The seller cannot cancel it for the buyer (403)", cancel(order).status_code == 403)
check("Seller's order page has no Cancel button", "Cancel Order</button>" not in page(f"/orders/{order}"))
with client.session_transaction() as session:
    session.clear()
check("A logged-out visitor is sent to log in", cancel(order).status_code == 302)
check("...and the order is still pending", database.get_order_details(order)["status"] == "Confirmed")

print("\nSHIPPED ORDERS CANNOT BE CANCELLED")
login(SELLER, "seller")
client.post(f"/orders/{order}/status", data={"status": "Shipped"})
login(BUYER, "buyer")
detail = page(f"/orders/{order}")
check("Shipped order shows why it can't be cancelled", "already shipped" in detail)
check("...and has no Cancel button", "Cancel Order</button>" not in detail)
check("Cancelling a shipped order is refused", cancel(order).status_code == 400)
check("...and it stays Shipped", database.get_order_details(order)["status"] == "Shipped")

print("\nSELLER DISABLES CANCELLATIONS")
order = new_order(teapot)
login(SELLER, "seller")
check("Seller dashboard shows the policy form", "Order Cancellation Policy" in page("/seller"))
set_policy(cancel_mode="disabled")
check("Policy saved as disabled", database.get_cancellation_policy(SELLER)["enabled"] is False)
check("Dashboard has 'Never' selected", 'value="disabled"\n                       checked' in page("/seller"))
login(BUYER, "buyer")
check("Buyer is told the seller doesn't accept cancellations",
      "does not accept cancellations" in page(f"/orders/{order}"))
check("Cancelling is refused", cancel(order).status_code == 400)

print("\nSELLER SETS A TIME WINDOW")
login(SELLER, "seller")
set_policy(cancel_mode="window", window_hours="24")
check("Policy saved as a 24-hour window", database.get_cancellation_policy(SELLER) == {"enabled": True, "window_hours": 24})
recent = new_order(teapot, hours_ago=2)
old = new_order(teapot, hours_ago=25)
login(BUYER, "buyer")
check("Order inside the window shows the deadline", "You can cancel this order until" in page(f"/orders/{recent}"))
check("Order outside the window says the window passed", "window for this order has passed" in page(f"/orders/{old}"))
check("Cancelling outside the window is refused", cancel(old).status_code == 400)
check("Cancelling inside the window works", cancel(recent).status_code == 302
      and database.get_order_details(recent)["status"] == "Cancelled")

print("\nPOLICY VALIDATION")
login(SELLER, "seller")
for bad in ("0", "-5", "abc", str(database.MAX_CANCEL_WINDOW_HOURS + 1)):
    set_policy(cancel_mode="window", window_hours=bad)
    check(f"Window of '{bad}' hours is rejected", database.get_cancellation_policy(SELLER)["window_hours"] == 24)
set_policy(cancel_mode="nonsense")
check("An unknown option is rejected", database.get_cancellation_policy(SELLER)["window_hours"] == 24)
set_policy(cancel_mode="until_shipped")
check("Seller can go back to 'until shipped'",
      database.get_cancellation_policy(SELLER) == {"enabled": True, "window_hours": None})
login(BUYER, "buyer")
check("A buyer account cannot change policies", set_policy(cancel_mode="disabled").status_code == 403)
check("Each seller has their own policy (others still default)",
      database.get_cancellation_policy(OTHER_SELLER) == {"enabled": True, "window_hours": None})

print("\nCANCELLED ORDERS AFTERWARDS")
login(SELLER, "seller")
cancelled = database.get_orders(BUYER)
cancelled = [o for o in cancelled if database.get_order_details(o)["status"] == "Cancelled"][0]
response = client.post(f"/orders/{cancelled}/status", data={"status": "Shipped"})
check("Seller cannot post updates on a cancelled order", response.status_code == 400)
check("Seller's order page says it was cancelled", "cancelled by the buyer" in page(f"/orders/{cancelled}"))
sales = database.get_seller_sales(SELLER)
live = [database.get_order_details(o) for o in database.get_orders(BUYER)]
live = [o for o in live if o["status"] != "Cancelled"]
check("Seller revenue leaves out cancelled orders", sales["revenue"] == sum(o["total"] for o in live))
check("Seller order count leaves out cancelled orders", sales["order_count"] == len(live))

passed = sum(results)
print(f"\n{passed} of {len(results)} checks passed.")
print("Everything works." if passed == len(results) else "Something failed: read the FAIL lines above.")
