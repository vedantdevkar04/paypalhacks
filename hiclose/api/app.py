"""The HiClose server. Run it with:

    uvicorn hiclose.api.app:create_default_app --factory --port 8000
"""

import asyncio
import html
import os
from pathlib import Path

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from hiclose.agent.money import fmt
from hiclose.api.ratelimit import RateLimiter
from hiclose.api.service import HiCloseService
from hiclose.channels import OutgoingMessage, WebChannel
from hiclose.channels.whatsapp import WhatsAppChannel
from hiclose.paypal import PayPalError, PayPalWebhooks

ROOT = Path(__file__).resolve().parents[2]
STATIC = Path(__file__).parent / "static"
HOLD_TEXT = "One moment, I'm checking that for you. Send any message if my answer doesn't show up."
log = logging.getLogger("hiclose")


def _public_url(request: Request) -> str:
    base = os.environ.get("PUBLIC_BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL")
    return base.rstrip("/") + request.url.path if base else str(request.url)


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title>"
        "<body style='font-family:system-ui;display:grid;place-items:center;min-height:90vh;background:#f6f5f2;margin:0'>"
        f"<main style='text-align:center;padding:24px'>{body}<p><a href='/'>Back to the chat</a></p></main>"
    )


def create_app(
    service: HiCloseService,
    web: WebChannel,
    webhooks: PayPalWebhooks | None = None,
    limiter: RateLimiter | None = None,
    whatsapp: WhatsAppChannel | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app):
        async def sweep():
            while True:
                await asyncio.sleep(60)
                try:
                    service.release_expired()
                except Exception:
                    log.exception("hold sweep failed")

        task = asyncio.create_task(sweep())
        yield
        task.cancel()

    app = FastAPI(title="HiClose", lifespan=lifespan)
    limiter = limiter or RateLimiter(int(os.environ.get("CHAT_LIMIT_PER_MINUTE", "20")), 60)
    app.state.tasks = set()

    # Twilio waits 15 s for the webhook response; answer a little before that.
    reply_seconds = float(os.environ.get("WHATSAPP_REPLY_SECONDS", "13"))
    # Replies that missed the webhook and that the API refused, delivered on the customer's next message.
    app.state.whatsapp_pending = {}

    async def send_late(task, message):
        """The agent missed Twilio's webhook deadline. Try the Messages API; if Twilio refuses
        free-form sends (21654), keep the reply for the customer's next webhook."""
        try:
            reply = await task
        except Exception:
            log.exception("failed to handle %s message %s", message.channel, message.message_id)
            return
        if reply is None or whatsapp is None:
            return
        try:
            await whatsapp.send(reply)
        except Exception:
            log.warning("late whatsapp reply for %s queued for the customer's next message", message.message_id)
            app.state.whatsapp_pending.setdefault(message.customer_id, []).append(reply)

    app.mount("/products", StaticFiles(directory=STATIC / "products"), name="products")

    @app.get("/")
    def chat_page():
        return FileResponse(STATIC / "index.html")

    @app.get("/owner")
    def owner_page():
        return FileResponse(STATIC / "owner.html")

    @app.get("/api/owner")
    def owner_data():
        return {**service.owner_view(), "stats": stats(), "agent_enabled": service.enabled}

    @app.get("/health")
    def health():
        return {"ok": True, "agent_enabled": service.enabled}

    @app.get("/api/shop")
    def shop():
        catalog = service.shop.catalog
        posts = []
        for product in catalog.products():
            view = catalog.public(product.product_id)
            for post_id in product.post_ids:
                posts.append({
                    "post_id": post_id,
                    "product_id": product.product_id,
                    "name": product.name,
                    "description": product.description,
                    "price": fmt(product.list_cents, product.currency),
                    "image_url": f"/products/{product.image}" if product.image else None,
                    "sizes_in_stock": view["sizes_in_stock"],
                })
        return {"brand": service.shop.brand, "posts": posts}

    @app.post("/api/chat")
    async def chat(payload: dict, request: Request):
        visitor = request.client.host if request.client else "unknown"
        if not limiter.allow(visitor):
            raise HTTPException(429, "Slow down a little: too many messages in a minute")
        messages = web.parse(payload)
        if not messages:
            raise HTTPException(400, "customer_id and text are required")
        for message in messages:
            await service.handle(message)
        return {"ok": True}

    @app.get("/api/chat/{customer}/messages")
    def messages(customer: str, after: int = 0):
        return {"messages": web.transcript(f"web:{customer}", after)}

    @app.get("/checkout/return")
    async def checkout_return(token: str):
        try:
            record = await service.payment_returned(token)
        except PayPalError as error:
            return _page("Payment problem", f"<h1>We couldn't confirm that payment</h1><p>{html.escape(str(error))}</p>")
        if record.status == "PAID":
            price = fmt(record.quote.price_cents, record.quote.currency)
            return _page("Paid", f"<h1>Paid ✅</h1><p>{price} received. Order {html.escape(record.order_id)}.</p>")
        return _page("Not paid", f"<h1>Payment not completed</h1><p>Status: {html.escape(record.status)}</p>")

    @app.get("/checkout/cancel")
    async def checkout_cancel(token: str = ""):
        if token:
            await service.checkout_cancelled(token)
        return _page("Cancelled", "<h1>Checkout cancelled</h1><p>Nothing was charged.</p>")

    @app.post("/webhooks/paypal")
    async def paypal_webhook(request: Request):
        if webhooks is None:
            raise HTTPException(503, "PayPal webhook not configured")
        event = await request.json()
        result = await asyncio.to_thread(webhooks.handle, dict(request.headers), event)
        if not result.accepted:
            raise HTTPException(400, result.reason)
        await service.notify_payment(result.order)
        return {"ok": True, "result": result.reason}

    @app.post("/webhooks/whatsapp")
    async def whatsapp_webhook(request: Request):
        if whatsapp is None:
            raise HTTPException(503, "WhatsApp not configured")
        params = whatsapp.form(await request.body())
        if not whatsapp.verify(_public_url(request), params, request.headers.get("X-Twilio-Signature", "")):
            raise HTTPException(403, "signature not verified")
        # Reply inside the webhook response (TwiML). Twilio refuses free-form API sends from
        # this sender (error 21654, ContentSid required), but accepts a TwiML reply.
        replies = []
        for message in whatsapp.parse(params):
            if not limiter.allow(message.customer_id):
                continue
            replies.extend(app.state.whatsapp_pending.pop(message.customer_id, []))
            task = asyncio.create_task(service.handle(message, deliver=False))
            try:
                reply = await asyncio.wait_for(asyncio.shield(task), reply_seconds)
            except asyncio.TimeoutError:
                log.warning("whatsapp reply for %s missed the webhook deadline", message.message_id)
                late = asyncio.create_task(send_late(task, message))
                app.state.tasks.add(late)
                late.add_done_callback(app.state.tasks.discard)
                replies.append(OutgoingMessage("whatsapp", message.customer_id, HOLD_TEXT))
                continue
            except Exception:
                log.exception("failed to handle %s message %s", message.channel, message.message_id)
                continue
            if reply is not None:
                replies.append(reply)
        return Response(whatsapp.twiml(replies), media_type="application/xml")

    @app.get("/api/stats")
    def stats():
        s = service.stats
        return {
            "messages": s.messages, "replies": s.replies, "checkouts": s.checkouts, "paid": s.paid,
            "revenue": {cur: fmt(cents, cur) for cur, cents in s.revenue_cents.items()},
            "handed_off": s.handed_off, "guard_rewrites": s.guard_rewrites,
        }

    return app


def create_default_app() -> FastAPI:
    from dotenv import load_dotenv

    from hiclose.agent import ConversationStore, GeminiModel, SalesAgent, Shop
    from hiclose.paypal import OrderBook, PayPalCheckout, PayPalClient
    from hiclose.rules import Catalog, QuoteBook

    load_dotenv(ROOT / ".env")
    client = PayPalClient.from_env()
    brand = os.environ.get("SHOP_BRAND", "Indigo Lane")
    payments = PayPalCheckout(client, OrderBook(), brand)
    shop = Shop(brand, Catalog.from_json(ROOT / "fixtures" / "catalog.json"), QuoteBook(), payments)
    agent = SalesAgent(GeminiModel.from_env(), shop, ConversationStore())
    web = WebChannel()
    whatsapp = WhatsAppChannel.from_env()
    channels = {"web": web, **({"whatsapp": whatsapp} if whatsapp else {})}
    service = HiCloseService(agent, shop, payments, channels)
    webhook_id = os.environ.get("PAYPAL_WEBHOOK_ID", "")
    webhooks = PayPalWebhooks(client, payments, webhook_id) if webhook_id else None
    return create_app(service, web, webhooks, whatsapp=whatsapp)
