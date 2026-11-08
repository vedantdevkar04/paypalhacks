"""Try a real PayPal sandbox order, outside the agent.

    python -m hiclose.paypal.try_checkout serve      makes an order, waits on localhost:8000, captures on return
    python -m hiclose.paypal.try_checkout create     makes a $72 jacket order and prints the pay link
    python -m hiclose.paypal.try_checkout capture    captures it, after you approve as the sandbox buyer
"""

import json
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from hiclose.paypal import OrderBook, OrderRecord, PayPalCheckout, PayPalClient
from hiclose.rules import Quote, QuoteBook

ROOT = Path(__file__).resolve().parents[2]
SAVED = ROOT / ".cache" / "sandbox_order.json"


def main() -> None:
    load_dotenv(ROOT / ".env")
    orders = OrderBook()
    checkout = PayPalCheckout(PayPalClient.from_env(), orders, "Indigo Lane")
    if sys.argv[1:2] == ["serve"]:
        serve(checkout)
    elif sys.argv[1:2] == ["create"]:
        quote = QuoteBook().create("jacket-denim", 7200, "USD", size="M")
        result = checkout.create_checkout(quote)
        SAVED.parent.mkdir(exist_ok=True)
        SAVED.write_text(json.dumps({"order_id": result.order_id, "approve_url": result.approve_url,
                                     "quote": {**asdict(quote), "expires_at": quote.expires_at.isoformat()}}))
        print(f"order {result.order_id} created for $72.00\nopen and pay as the sandbox buyer:\n{result.approve_url}")
    elif sys.argv[1:2] == ["capture"]:
        saved = json.loads(SAVED.read_text())
        q = saved["quote"]
        quote = Quote(**{**q, "expires_at": datetime.fromisoformat(q["expires_at"])})
        orders.add(OrderRecord(saved["order_id"], quote, saved["approve_url"]))
        record = checkout.confirm(saved["order_id"])
        print(f"order {record.order_id}: {record.status} | capture {record.capture_id} | ships to {record.ship_to}")
    else:
        print(__doc__)


def serve(checkout: PayPalCheckout) -> None:
    import uvicorn
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse

    quote = QuoteBook().create("jacket-denim", 7200, "USD", size="M")
    result = checkout.create_checkout(quote)
    print(f"order {result.order_id} created for $72.00\nopen and pay as the sandbox buyer:\n{result.approve_url}", flush=True)
    app = FastAPI()

    @app.get("/checkout/return", response_class=HTMLResponse)
    def returned(token: str):
        record = checkout.confirm(token)
        print(f"RETURNED order {record.order_id}: {record.status} | capture {record.capture_id} | ships to {record.ship_to}", flush=True)
        mark = "Paid ✅" if record.status == "PAID" else f"Not paid: {record.status}"
        return f"<h1>{mark}</h1><p>Order {record.order_id}, capture {record.capture_id}. You can close this tab.</p>"

    @app.get("/checkout/cancel", response_class=HTMLResponse)
    def cancelled(token: str = ""):
        print(f"CANCELLED order {token}", flush=True)
        return "<h1>Checkout cancelled</h1>"

    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")


if __name__ == "__main__":
    main()
