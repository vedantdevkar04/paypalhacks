"""The HiClose server. Run it with:

    uvicorn hiclose.api.app:create_default_app --factory --port 8000
"""

import asyncio
import html
import os
from datetime import datetime
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
from hiclose.logistics import DispatchBoard, DispatchError
from hiclose.market import Channel3Client, PhotoMatcher, market_report
from hiclose.paypal import PayPalError, PayPalWebhooks
from hiclose.rules import CatalogError

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
    market_client: Channel3Client | None = None,
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

    @app.get("/tryon/{token}")
    def tryon_image(token: str):
        studio = service.shop.tryon
        photo = studio.get(token) if studio else None
        if photo is None:
            raise HTTPException(404, "this preview has expired")
        return Response(photo.data, media_type=photo.mime, headers={"Cache-Control": "private, max-age=3600"})

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
        studio = service.shop.tryon
        tryon = {"enabled": studio is not None, **(studio.status() if studio else {})}
        return {"ok": True, "agent_enabled": service.enabled, "tryon": tryon}

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
        shop = service.shop
        return {"brand": shop.brand, "posts": posts, "tryon": shop.tryon is not None,
                "photo_match": shop.photo_match is not None,
                "photos": shop.tryon is not None or shop.photo_match is not None}

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

    # ---- dispatch board (Bryntum page at /dispatch) -------------------------------------------

    def board() -> DispatchBoard:
        if service.dispatch is None:
            raise HTTPException(503, "dispatch board not configured")
        return service.dispatch

    def stock_of(product_id: str) -> int:
        catalog = service.shop.catalog
        try:
            return sum(catalog.in_stock(product_id, size) for size in catalog.get(product_id).sizes)
        except CatalogError:
            return 0

    @app.get("/dispatch")
    def dispatch_page():
        return FileResponse(STATIC / "dispatch.html")

    @app.get("/api/dispatch")
    def dispatch_view():
        return board().view(stock_of)

    @app.post("/api/dispatch/assign")
    def dispatch_assign(payload: dict):
        shipment_id, resource = str(payload.get("shipment_id", "")), str(payload.get("driver_id", ""))
        try:
            if resource == "unassigned":
                board().unassign(shipment_id)
                return {"ok": True, "board": board().view(stock_of)}
            start = datetime.fromisoformat(str(payload.get("start", "")))
            placed = board().assign(shipment_id, resource, start)
        except (DispatchError, ValueError) as error:
            raise HTTPException(400, str(error))
        return {"ok": placed.ok, "reason": placed.reason, "board": board().view(stock_of)}

    @app.post("/api/dispatch/auto")
    def dispatch_auto():
        result = board().auto_assign()
        return {**result, "board": board().view(stock_of)}

    # ---- market prices from Channel3 (owner dashboard) -----------------------------------------

    @app.get("/api/market")
    def market():
        if market_client is None:
            return {"enabled": False, "rows": []}
        return {"enabled": True, "rows": market_report(market_client, service.shop.catalog)}

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
    from hiclose.agent.tryon import GeminiImageModel, TryOnStudio
    from hiclose.paypal import OrderBook, PayPalCheckout, PayPalClient
    from hiclose.rules import Catalog, QuoteBook

    load_dotenv(ROOT / ".env")
    client = PayPalClient.from_env()
    brand = os.environ.get("SHOP_BRAND", "Indigo Lane")
    payments = PayPalCheckout(client, OrderBook(), brand)
    # Off by default: image generation needs a paid Gemini plan (the free tier's limit is 0).
    image_model = GeminiImageModel.from_env() if os.environ.get("TRYON_ENABLED", "0") == "1" else None
    studio = TryOnStudio(image_model, STATIC / "products") if image_model else None
    shop = Shop(brand, Catalog.from_json(ROOT / "fixtures" / "catalog.json"), QuoteBook(), payments, studio)
    channel3 = Channel3Client.from_env()
    if channel3 is not None:
        shop.photo_match = PhotoMatcher(channel3, shop.catalog)
    agent = SalesAgent(GeminiModel.from_env(), shop, ConversationStore())
    web = WebChannel()
    whatsapp = WhatsAppChannel.from_env()
    channels = {"web": web, **({"whatsapp": whatsapp} if whatsapp else {})}
    dispatch = DispatchBoard.from_json(ROOT / "fixtures" / "logistics.json")
    service = HiCloseService(agent, shop, payments, channels, dispatch=dispatch)
    webhook_id = os.environ.get("PAYPAL_WEBHOOK_ID", "")
    webhooks = PayPalWebhooks(client, payments, webhook_id) if webhook_id else None
    return create_app(service, web, webhooks, whatsapp=whatsapp, market_client=channel3)
