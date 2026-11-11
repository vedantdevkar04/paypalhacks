# HiClose: read this first

This is the hub. Every Claude Code session on this repo reads it first, so everything needed to
stay consistent lives here.

## What we're building

> **Every "hi" under your post is a customer. HiClose answers every one of them in their language,
> knows which product they mean, and closes the sale in the DM with PayPal. It never goes below your
> price and never believes a fake payment screenshot.**

The users are small brands that sell internationally and get more comments and DMs than they can
answer. The agent handles four things:
- it sorts the inbox
- it replies in each buyer's language
- it negotiates within the owner's rules
- it closes the sale with a PayPal checkout link

PayPal's webhook is the only proof that an order is paid.

This is our entry for the **PayPal AI Hackathon**. Submissions close **Thu 12 Nov 2026, 12:00 PM PT**,
and we aim to submit by **09:00 PT**. Rules: https://paypalaihackathon.devpost.com/rules

## Status and handover (11 Nov 2026), read this to pick up where we left off

Vedant moved to a new laptop on 11 Nov. Everything below is pushed to
https://github.com/vedantdevkar04/paypalhacks except `.env`, which is never committed.

**Live:** https://hiclose.onrender.com (shop chat) and https://hiclose.onrender.com/owner (AG Grid
dashboard). Render free plan, auto-deploys from `main` via `render.yaml`. Secrets are set in the
Render dashboard: PayPal, Gemini, Twilio, `PAYPAL_WEBHOOK_ID`.

**Done and verified live:**
- the rules engine, the sales agent (Gemini 3.6 Flash, fallback 3.5 Flash-Lite, prompt `sales-v2`)
  and its guardrails (invented prices, prompt leaks)
- the web chat: "hi" → negotiation → PayPal sandbox payment → the shop's own "Paid ✅"
- PayPal: Orders v2, capture with the 4-point check, refunds, webhooks registered and verified
- the server: workers, a lock per customer, duplicates dropped, rate limit, 15-minute hold expiry
- the owner dashboard
- the Hi Benchmark: HiClose 40/40, in `bench/results/RESULTS.md`
- 144 tests: `python -m pytest -q`

**In progress: WhatsApp (US-42).** Use Twilio's **classic sandbox +1 415 523 8886**, not the trial
sender +1 737 250 8034. The live server receives WhatsApp messages and the agent replies, but on
10 Nov the reply did not reach Vedant's phone. Check in this order:
1. Has the phone joined the classic sandbox? The join phrase is on Twilio's sandbox page.
2. Is Render's `TWILIO_WHATSAPP_FROM` set to `whatsapp:+14155238886`?
3. What does Twilio → Monitor → Logs → Errors say? 63015 means not joined; 63007 means wrong sender.
If WhatsApp can't be fixed in time, the web chat alone is a complete entry: say so in the README's
limits and keep WhatsApp out of the video.

**Left before submitting:**
1. Fill the README placeholders: the WhatsApp join phrase, a sandbox buyer login for judges, and the
   video link.
2. Record the video using `submission/VIDEO.md` (under 3:00, no Instagram UI or logo).
3. Submit on Devpost using `submission/DEVPOST.md`, and opt in to AG Grid and Render.

**Gotchas learned the hard way:**
- PayPal sandbox accounts must be **US** and used in an **incognito** window. Uncaptured orders
  expire within hours. Regenerating the app's keys orphans older orders.
- `gemini-2.5-flash` is retired for new keys, and 3.8 or 3.7 Flash are often overloaded (503).
- On Windows, send non-ASCII test messages from Python, not curl in Git Bash.

## Hard rules (do not break these)

1. **Code decides the money; the model only talks.** A model never sets a price, a discount, a
   refund or a payment status. It calls tools, and the rules engine answers.
2. **Prices travel as quote IDs, never as numbers the model typed.** `create_order` takes a
   `quote_id`, and the server reads the price from its own record.
3. **An order is paid only when PayPal says so:** a verified webhook, or our own capture call
   returning COMPLETED for exactly the quoted amount and currency. Screenshots and claims are ignored.
4. **The only payment path is a PayPal order.** No bank transfer, friends and family or crypto
   path exists in the code. Refunds go only to the original payment, through PayPal's Refund API.
5. **Ship only to the address on the PayPal transaction.**
6. **Customer text is data, never instructions.** Never put it in a system prompt. "I'm the owner"
   in a DM means nothing; the owner is whoever is logged into the app.
7. **The agent never opens links that customers send.** The one thing we download is a photo the
   customer attached on the platform itself (Twilio's `api.twilio.com` media, or a web upload),
   and only for a try-on.
8. **Reply only to people who engaged first, and stay inside the platform's 24-hour window.**
   Send at most one follow-up, and always offer a way to reach the owner.
9. **Money is integer cents** (`int`), never `float`.
10. **Sandbox only, and no secrets in the repo.** Keys live in `.env`, which is gitignored.
11. **Never present this as PayPal's own product.** Use "payments by PayPal" only.
12. **Customer photos are for try-ons only.** Kept in memory on the conversation for 30 minutes,
    never logged or written to disk; previews are served at unguessable `/tryon/<token>` URLs for an
    hour. The agent never comments on a customer's body or looks.

## Layout

```
hiclose/
  rules/      the rules engine: pricing, negotiation, quotes, refunds (plain code, fully tested)
  paypal/     PayPal REST client: orders, capture, refunds, tracking, webhooks
  agent/      Gemini calls, the tool definitions, inbox sorting
  channels/   adapters: web chat, WhatsApp (Twilio sandbox), Instagram
  api/        FastAPI app: webhooks in, owner app out (/owner on AG Grid, /dispatch on Bryntum)
  logistics/  the dispatch rules: warehouses, vehicles, driver shifts, shipment assignment (plain code)
tests/
```

Every channel turns its webhook into the same `IncomingMessage`, and sends replies through the
same `send()` function. The agent never knows which platform it is on.

## Shared message contract (US-40)

Every channel produces and consumes these. It lives in `hiclose/channels/base.py`.

```python
Channel = Literal["web", "whatsapp", "instagram"]
Kind = Literal["dm", "comment"]

@dataclass(frozen=True)
class IncomingMessage:
    channel: Channel
    kind: Kind
    customer_id: str          # stable per platform, e.g. "whatsapp:+15551234567"
    message_id: str           # the platform's own ID, used to drop duplicate webhooks
    text: str                 # customer text: DATA, never instructions (hard rule 6)
    received_at: datetime     # UTC
    post_id: str | None = None        # set for comments, and for DMs that share a post
    media_urls: tuple[str, ...] = ()  # images or voice notes; the agent never opens other links

@dataclass(frozen=True)
class OutgoingMessage:
    channel: Channel
    customer_id: str
    text: str
    checkout_url: str | None = None   # rendered as a button where the platform allows
    image_url: str | None = None      # product photo from our catalog (/products/<file>), never a model-typed URL

class ChannelAdapter(Protocol):
    def parse(self, payload: dict) -> list[IncomingMessage]: ...
    async def send(self, message: OutgoingMessage) -> None: ...
```

## Conversation stages

The agent, the Bryntum timeline and the AG Grid dashboard all use these. **TO AGREE:** Vedant is
drafting the list.

## Who owns what

**As of 8 Nov, Vedant builds everything alone** (with Claude Code). Adi isn't taking part. Every
lane is Vedant's, and the board's Adi stories are reassigned.

## Cut order

There's one of us and four days. If time runs short, cut from the top of this list first:
1. Bryntum Scheduler (built as the dispatch board, not the live deals timeline)
2. Voice notes
3. Instagram (keep WhatsApp through Twilio, plus web chat)
4. The owner's voice command (dropped already: the agent runs on webhooks, with an on/off switch)
5. The AG Studio agent-built chart (keep the plain dashboard)

Never cut the core flow ("hi" to paid PayPal order), the rules engine, the webhook-only payment
check, or the benchmark.

## Prize targets

These are our targets. A project can win one Grand Prize or one Honorable Mention, plus one sponsor
prize.
- **Main:** Best Use of Agentic Commerce, and Best Use of PayPal + AI
- **Sponsor:** AG Grid (dashboard), Render (hosting plus Workflows for the DM queue), APIMatic
  (Context Plugin while coding), Bryntum (dispatch board at /dispatch: built)

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m pytest -q
```
