# HiClose: read this first

This is the hub. Two people build this in parallel with separate Claude Code sessions that can't
see each other, so everything we need to stay consistent lives here.

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
7. **The agent never opens links that customers send.**
8. **Reply only to people who engaged first, and stay inside the platform's 24-hour window.**
   Send at most one follow-up, and always offer a way to reach the owner.
9. **Money is integer cents** (`int`), never `float`.
10. **Sandbox only, and no secrets in the repo.** Keys live in `.env`, which is gitignored.
11. **Never present this as PayPal's own product.** Use "payments by PayPal" only.

## Layout

```
hiclose/
  rules/      the rules engine: pricing, negotiation, quotes, refunds (plain code, fully tested)
  paypal/     PayPal REST client: orders, capture, refunds, tracking, webhooks
  agent/      Gemini calls, the tool definitions, inbox sorting
  channels/   adapters: web chat, WhatsApp (Twilio sandbox), Instagram
  api/        FastAPI app: webhooks in, owner app out
tests/
```

Every channel turns its webhook into the same `IncomingMessage`, and sends replies through the
same `send()` function. The agent never knows which platform it is on.

## Shared message contract (US-40): PROPOSAL, Adi to confirm

Vedant drafted this so the agent can be built against it. **Adi owns it.** Change it if you need
to, and tell Vedant before you do. It lives in `hiclose/channels/base.py`.

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

class ChannelAdapter(Protocol):
    def parse(self, payload: dict) -> list[IncomingMessage]: ...
    async def send(self, message: OutgoingMessage) -> None: ...
```

## Conversation stages

The agent, the Bryntum timeline and the AG Grid dashboard all use these. **TO AGREE:** Vedant is
drafting the list.

## Who owns what

- **Vedant:** `hiclose/rules/`, `hiclose/agent/`, the Hi Benchmark, the Devpost write-up
- **Adi:** `hiclose/paypal/`, `hiclose/channels/`, `hiclose/api/`, the owner app (AG Grid,
  Bryntum), the Render deploy
- **Both:** the demo brand and catalog, the benchmark messages, the video

Stay in your lane, so two AI sessions don't edit the same files. If you swap work, edit this list.

**Swap on 6 Nov:** Vedant built `hiclose/paypal/` (US-20, US-21, plus the capture, refund and
webhook logic) so the demo had real checkout early. **Adi: don't rebuild it.** Extend it from here.
It implements the agent's `CheckoutProvider` contract in `hiclose/agent/tools.py`. Still to do on
your side:
- the FastAPI routes: `/webhooks/paypal`, `/checkout/return`, `/checkout/cancel`
- registering the webhook in the PayPal dashboard once Render gives us a URL
- releasing stock holds when a checkout expires or is cancelled

## Cut order

There are two of us and seven days. If time runs short, cut from the top of this list first:
1. Bryntum Scheduler
2. Voice notes
3. Instagram (keep WhatsApp through Twilio, plus web chat)
4. The owner's voice command (keep typed commands)

Never cut the core flow ("hi" to paid PayPal order), the rules engine, the webhook-only payment
check, or the benchmark.

## Prize targets

These are our targets. A project can win one Grand Prize or one Honorable Mention, plus one sponsor
prize.
- **Main:** Best Use of Agentic Commerce, and Best Use of PayPal + AI
- **Sponsor:** AG Grid (dashboard), Render (hosting plus Workflows for the DM queue), APIMatic
  (Context Plugin while coding), Bryntum (live deals Scheduler)

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m pytest -q
```
