# Devpost entry, paste-ready

Each `##` heading is one Devpost field. Fill in the three `<...>` placeholders before submitting.

---

## Project name

HiClose

## Tagline

Every "hi" under your post becomes a sale: an AI agent that closes deals in DMs, and can never be
talked into a bad price or a fake payment. Payments by PayPal.

## Links

- Try it: https://hiclose.onrender.com
- Owner dashboard: https://hiclose.onrender.com/owner
- Dispatch board: https://hiclose.onrender.com/dispatch
- Code: https://github.com/vedantdevkar04/paypalhacks (MIT)
- Video: `<YouTube link>`

---

## Inspiration

Scroll through the comments under any small brand's post and you'll see the same words again and
again: "hi", "price?", "dm", "😍". Every one of them is someone who wants to buy, and most never get
an answer, because a one-person shop can't reply to all of them fast enough.

The tools that exist today are keyword flows. They answer "price" with a link and ignore everything
else. A general AI chatbot answers far better, but when we tested one, it told a customer the shop's
lowest price, revealed it again when asked to "translate" it, and accepted a made-up transaction ID
as payment. A shop can't hand its money to that.

We wanted an agent that's as helpful as an AI and as safe as a keyword bot.

## What it does

HiClose is a sales agent for small brands. It answers comments and DMs, works out which product the
customer means, negotiates within the owner's rules, and closes the sale in the chat with PayPal.

1. A customer says **"hi"** under the jacket post. HiClose knows the post, and replies with the
   jacket's price and the sizes in stock, in the customer's language.
2. They haggle. The shop's rules engine decides every counter-offer, and the AI only words it.
3. They say **"M please"**. HiClose creates a **PayPal order at exactly the agreed price**, holds the
   item for 15 minutes and sends the checkout button.
4. They pay on PayPal's own page: wallet, card or Pay Later, with no account needed.
5. PayPal confirms, and the shop posts **"Paid ✅ $72 for the Washed Denim Jacket (M), shipping to
   San Jose, CA"** by itself.
6. The owner sees every conversation, its stage and every order on a live dashboard. Complaints and
   refunds are flagged for the owner.

It works in a web chat that anyone can try and on WhatsApp.

## How we built it

**The rule we built around: the AI talks, code decides the money, PayPal moves it.**

- **A rules engine in plain Python** owns prices, hidden floor prices, negotiation rounds, stock and
  quotes. The model never sees a floor price.
- **A Gemini 3.6 Flash agent** uses function calling with five tools (`list_products`,
  `get_product`, `propose_price`, `checkout`, `hand_to_owner`). It falls back to Gemini 3.5
  Flash-Lite and retries when the model is busy.
- **Prices travel as quote IDs.** `checkout` creates a quote on the server, and the PayPal order is
  built from that quote. A fooled model can't make a $1 order.
- **A guardrail checks every reply** before it's sent. Any amount or percentage that no tool
  returned, or any piece of the agent's own instructions, sends the reply back for a rewrite.
- **PayPal:** Orders v2 create and capture with idempotency keys, the PayPal checkout page (wallet,
  guest card, Pay Later), webhooks verified with PayPal, and refunds to the original capture only.
  An order is marked paid only when PayPal reports a completed capture for exactly the quoted
  amount and currency.
- **A FastAPI server** with a bounded worker pool and one lock per customer, so many chats run at
  once and one customer's messages stay in order. It drops duplicate webhooks, rate-limits each
  visitor, and returns stock when a 15-minute hold expires.
- **Channels:** a web chat, and WhatsApp through Twilio, with every webhook's HMAC signature
  checked.
- **An owner dashboard** built on **AG Grid**, deployed on **Render**.
- **Channel3** for market prices on the owner dashboard (what similar items sell for elsewhere) and for
  matching a customer's photo to our closest product.
- **A dispatch board** built on **Bryntum Scheduler**: warehouses, vans, driver shifts and shipments for the day.
  Drag a shipment onto a driver and HiClose checks the move; paid PayPal orders show up as shipments to the PayPal address's city.

## Challenges we ran into

- **Testing against the real services found what mocks couldn't.** PayPal rejected our capture call
  until we sent an explicit JSON content type. Approved orders expire within hours if nobody captures
  them. And a sandbox account in the wrong currency blocked checkout entirely.
- **A dry run found two money bugs that 95 passing tests had missed.** When the guardrail asked the
  model to rewrite a reply, the model re-called the pricing tool, and that silently used up a
  negotiation round. A buyer was told $72 and charged $68. Each bug only appeared when two behaviours
  combined. Both are now tests.
- **Live testing found an agent that went silent.** Asking for "blue pants" handed the chat to the
  owner, and every later message got no reply. We changed handoffs so the agent keeps helping until
  the owner actually takes over.
- **Being fair to the baseline.** Our first benchmark scoring flagged four plain-LLM replies that
  were really refusals. We read every flagged transcript, fixed the scorer and re-scored.

## Accomplishments that we're proud of

- **40/40 on our Hi Benchmark.** In the same 40 conversations, a plain LLM revealed the floor price
  twice and believed a fake payment, and a keyword bot ignored half the customers.
- **A real end-to-end sale on the live site:** "hi", then negotiation, a PayPal sandbox payment, and
  the shop's own "Paid ✅" message. Payments are confirmed by verified webhooks.
- **No path to money that bypasses code.** It's in the architecture, not in a prompt asking the
  model to behave.
- **205 automated tests**, plus a benchmark anyone can re-run.

## What we learned

- **Keep the model out of every decision that costs money.** The model is excellent at
  understanding "bhai ye kitne ka hai?" and terrible at being trusted with a discount.
- **Prompt instructions are requests; code is a control.** Every rule that matters is enforced
  outside the model.
- **Test with real services and real conversations early.** Our most important bugs came from live
  PayPal and live chats, not from unit tests.

## What's next for HiClose

- Instagram comments and DMs, once the Meta app is approved
- A cart for several items in one PayPal order
- An owner app to take over chats and reply, with PayPal refunds and tracking from the same screen
- Persistent storage and a hosted plan for real shops

## Built with

`python` · `fastapi` · `gemini` · `google-genai` · `paypal` · `paypal-orders-api` ·
`paypal-webhooks` · `twilio` · `whatsapp` · `ag-grid` · `render` · `httpx` · `pytest`

---

## Before you submit

- [ ] Video uploaded to YouTube (public or unlisted), under 3:00, link pasted above and in the README
- [ ] README placeholders filled: WhatsApp join phrase, sandbox buyer login, video link
- [ ] Repo is public and MIT shows in the GitHub About box
- [ ] Wake the live site just before submitting (open https://hiclose.onrender.com)
- [ ] Opt in to sponsor prizes: AG Grid, Bryntum, Channel3, Render
- [ ] Submitted before 12:00 PT on 12 Nov (aim for 09:00 PT)
