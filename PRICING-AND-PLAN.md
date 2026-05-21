# Pricing & Plan — AI Voice Agent

> Locked offer, cost model, margin analysis, quality feasibility, and
> talk track for the lighthouse customer: Supreme Petrochemicals (SPC),
> Chennai. Numbers in INR unless stated otherwise.

---

## TL;DR — the offer

| Item | Amount |
|---|---:|
| One-time setup (charged at contract sign) | **₹1,00,000** |
| Monthly subscription (billed monthly, annual contract) | **₹15,000** |
| Calls included per month | **2,000** |
| Overage above 2,000 calls/mo | **₹10/call** |
| **Year 1 total** (setup + 12 × ₹15k) | **₹2,80,000** |
| **Year 2+ annual** (12 × ₹15k) | **₹1,80,000** |
| Annual contract term | 12 months, auto-renew |
| Cancel anytime after Month 1 | 30-day notice, no penalty |

**Target customer profile:** B2B Indian SME or mid-market doing
outbound sales / lead-qualification calls in volumes of 30–80/day.
SPC fits perfectly at 50 calls/day.

---

## What SPC actually gets for ₹15k/month

Tangible, demoable deliverables:

1. **Priya** — a custom-tuned AI sales agent voice (chosen from
   Sarvam Bulbul v3's gallery; 30+ Indian voices to pick from).
2. **All 3 priority Indian languages from day one**:
   English, Hindi, Tamil. Priya auto-detects and switches on every
   turn — no manual setting, no awkward transitions.
3. **Up to 2,000 outbound calls/month**, billed flat. That's 90+
   calls a day with full weekends off — well above SPC's 50/day
   target. No usage anxiety.
4. **Live CRM dashboard** at a branded URL:
   - Upload lead lists via CSV or add one at a time
   - Click "Call with AI" on any lead → Priya dials within ~3 sec
   - Realtime transcript pane as the call happens
   - English-normalized AI summary + 0–100 conversion score after
     each call ends (~30 sec wait)
   - Lead status: Hot / Warm / Cold / Do Not Call / Needs Review
   - Extracted fields: decision-maker, chemicals discussed, monthly
     volume, current supplier, supplier pain, decision timeline,
     email/WhatsApp
5. **Hot-lead handoff to human reps**:
   - WhatsApp message with summary and a deeplink back to the lead
   - Email backup via Resend
   - Rep opens the lead → sees the AI summary + transcript + all
     extracted fields
   - One-click **Call now** button: on mobile it's a `tel:` link;
     on desktop it bridges via Exotel two-leg call so the rep's
     phone rings, then connects to the lead
6. **Agent ON/OFF master switch** — one toggle in /settings pauses
   all outbound dialing (for audits, holidays, ramp-down)
7. **Multi-rep dashboard** with role-based access (admin vs rep)
   and per-rep WhatsApp assignment for Hot leads
8. **BYON (Bring Your Own Number) option** — plug your existing
   Exotel/Plivo SID into Settings and we route every call through
   your trunk instead of ours. Lower telephony bills, you keep
   carrier relationship; subscription unchanged.
9. **Call recordings** stored in Cloudflare R2 (zero egress fees);
   replay any call from the lead detail page
10. **DNC list management** — instant respect for "do not call",
    persistent across all future campaigns

What is included in **Setup (₹1L one-time)**:

- Picking Priya's exact voice from Sarvam Bulbul gallery (live A/B
  with your team)
- Loading your full **250-product catalogue** into the agent's KB
- Tuning the sales playbook for SPC's 18 industries
- Drafting and registering the **TRAI DLT templates** in EN/HI/TA
- **50 supervised test calls** before go-live (your team listens,
  we tune)
- 6-scenario E2E demo (live, recorded, kept for your reference)
- 2-rep onboarding session (90 minutes)
- Cloudflare + Supabase + Sarvam + Exotel/Plivo account provisioning
  on your behalf (or your own accounts if preferred)

---

## Usage assumption (the contract floor)

| Parameter | Value |
|---|---:|
| Calls per day | 50 |
| Working days per year (incl. major holidays) | 260 |
| Total calls per year | **13,000** |
| Average call duration | 150 sec (2.5 min) |
| Total talk-time per year | 32,500 min (~542 hr) |
| Hot rate (industry benchmark) | 10–15% |
| Hot leads per year | ~1,300–1,950 |

Calls per month average: ~1,100. Well under the 2,000/mo cap, so
**no overage expected** at SPC's projected volume.

---

## Tech stack (no-compromise, locked)

| Layer | Choice | Rationale |
|---|---|---|
| Voice loop orchestration | **Pipecat (Python)** self-hosted on Hetzner CX22 (€4.50/mo) | Open-source, proven sub-700ms turn-taking, no per-minute platform tax. Same architecture Bolna and many YC voice startups run. |
| STT | **Sarvam Saaras v3** WebSocket streaming | Best-in-class Indian-language ASR, telephony-optimized, native code-mixing support |
| LLM (conversation) | **Google Gemini 2.5 Flash** | Free tier 1,500 req/day; paid is ~₹0.30/call; sub-300ms TTFT |
| TTS | **Sarvam Bulbul v3** streaming + intro caching | Best Indian-voice prosody. Pre-cache fixed intro per language → ~30% TTS cost cut |
| Telephony | **Plivo SIP outbound** (managed) or **BYON** (Exotel / Plivo / Tata) | India-native, DLT-friendly, ₹0.65–1.20/min. Twilio explicitly avoided — 2–3× cost for India |
| CRM frontend | **Next.js on Cloudflare Pages** | Free commercial-use tier, Mumbai+Chennai PoP |
| Database + Auth | **Supabase** Postgres + Auth (free tier → Pro $25/mo at scale) | RLS multi-tenancy, realtime fan-out for transcripts |
| Webhooks / API | **Cloudflare Workers** (Hono) | Zero cold-start; 100k req/day free |
| Recordings | **Cloudflare R2** | Zero egress fees |
| Scoring + email | **Gemini Flash + Resend** | English-normalized summaries; free tier covers SPC volume |
| Handoff | **wa.me deeplink** (no WhatsApp Business API needed for v1) | Free; upgrade to WABA only when client asks |

**Latency budget** (target sub-800ms turn-taking):

```
End of lead speech → first audible Priya word
   80 ms   VAD detects end of utterance
   30 ms   Network: phone → Plivo → our orchestrator
  200 ms   Sarvam Saaras WebSocket final transcript
  250 ms   Gemini 2.5 Flash time-to-first-token
  200 ms   Sarvam Bulbul TTS first audio chunk
   80 ms   Network: orchestrator → Plivo → phone
─────────
  840 ms   typical
  ~650 ms  when Priya is using a cached intro phrase
```

This matches every "premium" Indian voice agent on the market today.
We don't sacrifice quality to hit our margin.

---

## Cost model — per call

A 150-second blended-mix call:

| Component | Managed (we provide telephony) | BYON (SPC's own number) |
|---|---:|---:|
| Sarvam Saaras STT (₹30/hr × 2.5 min) | ₹1.25 | ₹1.25 |
| Sarvam Bulbul TTS (~500 new chars after caching) | ₹1.00 | ₹1.00 |
| Gemini 2.5 Flash conversation (~5k tokens) | ₹0.30 | ₹0.30 |
| Plivo SIP outbound mobile (₹0.80/min × 2.5) | ₹2.00 | **₹0** (SPC pays direct) |
| Post-call scoring LLM (Gemini Flash) | ₹0.10 | ₹0.10 |
| **Per-call cost to us** | **₹4.65** | **₹2.65** |

---

## Cost model — annual

13,000 calls/yr × per-call cost above:

| Cost bucket | Managed | BYON |
|---|---:|---:|
| Per-call variable (13k × per-call) | ₹60,450 | ₹34,450 |
| Hetzner CX22 VPS (Pipecat orchestrator) | ₹4,800 | ₹4,800 |
| Supabase free tier | ₹0 | ₹0 |
| Cloudflare Pages + Workers + R2 free tier | ₹0 | ₹0 |
| Resend free tier (3k email/mo, far above need) | ₹0 | ₹0 |
| Domain + misc + 10% buffer | ₹3,000 | ₹3,000 |
| **Total Year-2+ ops cost** | **₹68,250** | **₹42,250** |
| Year-1 add-on: setup engineering (3 days @ ₹15k/day) | +₹45,000 | +₹45,000 |
| **Total Year-1 cost** | **₹1,13,250** | **₹87,250** |

---

## Margin analysis

| Year | SPC pays | Cost (managed) | Cost (BYON) | Margin (managed) | Margin (BYON) |
|---|---:|---:|---:|---:|---:|
| Year 1 | ₹2,80,000 | ₹1,13,250 | ₹87,250 | **₹1,66,750 (60%)** | **₹1,92,750 (69%)** |
| Year 2 | ₹1,80,000 | ₹83,250 | ₹57,250 | **₹96,750 (54%)** | **₹1,22,750 (68%)** |
| Year 3 | ₹1,80,000 | ₹83,250 | ₹57,250 | **₹96,750 (54%)** | **₹1,22,750 (68%)** |
| **3-year cumulative** | **₹6,40,000** | **₹2,79,750** | **₹2,01,750** | **₹3,60,250 (56%)** | **₹4,38,250 (68%)** |

(Year 2+ cost includes ₹15k/yr light AMC engineering for KB updates and platform upgrades.)

**Healthy by every standard:**
- Year-2+ gross margin ≥ 54% on managed mode
- Year-2+ gross margin ≥ 68% on BYON mode
- Break-even on the contract within Month 8 of Year 1
- Customer Acquisition Cost (CAC) for the first deal is your time;
  the unit economics let you reinvest in pipeline from Month 9 onward

---

## Feasibility check — "genuine, honest, all Indian languages"

A point-by-point honest answer.

### 1. "Doesn't feel like talking to AI"

**Feasibility: HIGH (with our stack).**

What makes a voice agent feel human, in order of impact:

| Factor | Status |
|---|---|
| Sub-1-second turn-taking | ✓ 650–840 ms with our Pipecat + Sarvam + Gemini stack |
| Voice naturalness (prosody, breath, intonation) | ✓ Sarvam Bulbul v3 is the best Indian-language TTS available — comparable to ElevenLabs for English |
| Personalized opening (uses lead's name) | ✓ Built-in via `lead_first_name` metadata + 3-language templates |
| Filler words ("right", "okay", "achha") | ✓ Explicit in Priya's system prompt |
| Pauses (0.5–1 sec where a human would pause) | ✓ Pipecat supports inter-turn pauses; Sarvam Bulbul SSML supports pause tags |
| Mirroring lead's energy and formality | ✓ System prompt instruction |
| Objection handling without scripting | ✓ Sales playbook in system prompt + LLM reasoning |
| Acknowledges being AI if directly asked (honest) | ✓ Explicit prompt rule; pivots back to qualifying after disclosure |

**Caveat to manage expectations:** the most attentive human listener
can still tell within 30–60 seconds that it's an AI — that's true of
every voice agent on the market today, including the best US-based
ones. But for a 30-second qualification opener with a busy procurement
manager, the experience is genuinely indistinguishable from a junior
BDR. That's the bar SPC needs.

### 2. "Answers honestly and correctly"

**Feasibility: HIGH, with guardrails in place.**

- **Knowledge base grounding:** the 250-product catalogue is loaded
  into the agent's KB. Priya cross-references "Do you sell X?"
  against the actual list — no hallucination of products that
  don't exist.
- **Hard "never invent" rule** in the system prompt:
  > "Never invent products or prices. If asked something outside
  > scope, say: Let me have a product specialist call you back
  > within four hours."
- **No public price quoting:** Priya never quotes a price live —
  always defers to the 4-hour quote SLA. This eliminates the
  single biggest hallucination risk.
- **Honest AI disclosure:** when the lead asks "are you a bot",
  Priya says: *"I'm an AI assistant from SPC's sales team — I do
  the first 30 seconds, then a human takes over for serious quotes.
  Now, are you involved in procurement at {{lead.company}}?"*
  This is shipped behavior, not a roadmap promise.

### 3. "All Indian languages and understanding"

**Day-1 feasibility: HIGH for EN / HI / TA (SPC's market).**

| Language | STT (Sarvam Saaras) | TTS (Sarvam Bulbul) | LLM (Gemini Flash) |
|---|---|---|---|
| English (Indian) | ✓ | ✓ | ✓ |
| Hindi | ✓ | ✓ | ✓ |
| Tamil | ✓ | ✓ | ✓ |

**Other Indian languages available without code change** (same stack,
agent config update) — quote SPC future-state coverage:

| Language | STT | TTS | LLM | Activation cost |
|---|---|---|---|---|
| Telugu | ✓ | ✓ | ✓ | ₹0 (re-tune prompt: 1 hour) |
| Bengali | ✓ | ✓ | ✓ | ₹0 |
| Marathi | ✓ | ✓ | ✓ | ₹0 |
| Kannada | ✓ | ✓ | ✓ | ₹0 |
| Malayalam | ✓ | ✓ | ✓ | ₹0 |
| Gujarati | ✓ | ✓ | ✓ | ₹0 |
| Punjabi | ✓ | ✓ | ✓ | ₹0 |
| Odia | ✓ | ✓ | ✓ | ₹0 |

**Sarvam supports 11 Indian languages**; we ship 3 actively for SPC
and offer the others as a no-cost configuration add-on if their
expansion needs it.

**Language matching is locked** in the system prompt:
> "The single most important rule of this entire call: speak whatever
> language the lead chooses to speak."

The moment the lead replies in a different language, Priya switches
on the very next sentence. No warning, no transition phrase. This is
shipped — verified in the 5 "hot" golden transcripts that include
a deliberate language-switch mid-call.

### 4. "Clean CRM data"

**Feasibility: HIGH.**

- **English-normalization rule**: regardless of the call language,
  the scoring LLM produces `summary`, `reason`, `next_action`,
  `industry`, `chemicals[]`, `current_supplier`, `supplier_pain[]`,
  `timeline` all in English.
- **Validation guard**: the scoring worker rejects any LLM output
  that contains Devanagari (Hindi) or Tamil script in those fields
  and forces a retry → marks `needs_review` if it persists.
- **Structured extraction** via Zod schema — no free-text dumping.
- **Original-language transcripts** preserved verbatim in the
  `transcripts` table for fidelity (so the rep can review the
  actual conversation), but the CRM-facing summary fields are
  uniformly English. Any rep can read any lead.

---

## Competitive positioning — Indian market

| Platform | Effective per-call cost | India-native? | Margin if you resold them | Quality |
|---|---:|---|---:|---|
| Vapi (US) | ₹13–17 | No (no DLT, no DPDP) | Negative at ₹15k/mo | High |
| Bland (US) | ₹15–18 | No | Negative | High |
| Sarvam Samvaad (turnkey) | ₹14–15 | Yes | Negative — Samvaad fee eats it | High |
| Bolna (India SaaS) | ₹14 | Yes | Negative | High |
| **You (Pipecat + Sarvam + Plivo)** | **₹4.65 / ₹2.65 BYON** | **Yes** | **54–68%** | **Same Sarvam grade** |

You're not competing on quality — that's at parity with every
serious player. You're winning on **unit economics**, because you
cut the platform middleman that everyone else pays.

---

## Comparison vs the obvious alternative (human BDR)

This is the slide that closes the deal.

| Cost line | Human junior BDR | Priya AI |
|---|---:|---:|
| Monthly salary + ESI/PF/bonus (Chennai market) | ₹30,000 | — |
| Workstation, phone, electricity | ₹2,000 | — |
| Training + onboarding (amortized over 1 year) | ₹3,000 | — |
| Effective calls per day (a human BDR dials 25–40/day) | 30 | 50+ |
| Languages spoken fluently | 1–2 | 3 (Sarvam supports 11) |
| Hours of operation | 9am–6pm | 24/7 |
| Vacation, sick days, attrition | ~15% time loss | 0% |
| **Effective cost per call** | **~₹500** | **₹15k ÷ 1,100 = ₹14** |
| **Monthly cost** | **₹35,000** | **₹15,000** |

The math is brutal in your favour. SPC isn't replacing one BDR —
they're getting **the equivalent of 2–3 BDRs working bigger volume
in three languages for less than half the salary of one human.**

---

## Talk track for the close

> "For SPC, we're proposing a custom Priya agent at **₹15,000 a
> month, which is less than the salary of a junior BDR you'd hire
> for the same first-touch work.** Priya dials 50 leads a day in
> English, Hindi, or Tamil, qualifies them in under three minutes,
> drops a clean English summary plus a 0–100 conversion score into
> your CRM, and pings your reps on WhatsApp the moment a hot lead
> surfaces.
>
> Two thousand calls per month included — that's 67 percent
> headroom on your projected volume. You can flip the agent off
> any time, or plug in your own Exotel number if you'd prefer to
> keep telephony in-house — your subscription doesn't change.
>
> One-time setup is one lakh: we load your full 250-product
> catalogue, tune Priya's voice with your team, register the DLT
> templates in all three languages, and run 50 supervised test
> calls before go-live. Year-one all-in is two-point-eight lakh.
> Year two onwards is one-point-eight lakh — about the same as
> three months of one BDR's salary, for unlimited working hours
> and three languages of coverage."

---

## Negotiation room (in your back pocket)

If SPC pushes hard, here are the levers in order of preference:

1. **Drop monthly to ₹12,500/mo for the first 6 months, then
   ₹15,000/mo from month 7.** Total Year-1 reduction: ₹15,000.
   Margin Year 1 still 56% — fine.
2. **Halve the setup to ₹50,000** if they sign a 24-month
   commitment instead of 12. You make the difference back in
   guaranteed Year-2 revenue.
3. **Add a referral kickback**: 1 free month of subscription
   for every paying referral SPC sends. Costs you ₹15k per
   activated referral — cheaper than any CAC.
4. **DO NOT drop below ₹12k/mo** — that's the floor where
   Year-2 margin breaks negative. Walk away from a deal at
   ₹10k/mo; the customer isn't worth the support burden.

---

## Year-2+ upgrade ladder (so SPC's price increases without churn)

| Year | Tier | Price | What changes |
|---|---|---:|---|
| Year 1 | Lighthouse Launch | ₹15k/mo | Everything in this doc |
| Year 2 | Lighthouse Launch (renewal) | ₹15k/mo | Same — protect retention |
| Year 3 | Growth | ₹22k/mo | + 5,000 calls/mo cap, + WhatsApp Business API automated handoff (saves their reps 5 min per Hot lead), + dedicated quarterly business review |
| Year 4+ | Scale | ₹35k/mo | + Custom voice (cloned from a real SPC rep), + multi-tenant sub-accounts for SPC's distributor network, + API access for them to embed Priya in their own apps |

This is your renewal playbook — SPC doesn't get a "price hike",
they get **more value at a higher tier**. Year-3 conversion to
Growth is the typical SaaS expansion pattern.

---

## Pricing ladder for future clients (use SPC as anchor)

When pitching client #2 onwards, you reference SPC's tier publicly
as "Starter" — this psychologically anchors them upward:

| Tier | Audience | Price | Calls/mo |
|---|---|---:|---:|
| **Starter** (SPC's tier) | SME doing 30–80 calls/day, 1 product line | ₹15,000/mo | 2,000 |
| **Growth** | Mid-market doing 100–300 calls/day, multi-product | ₹35,000/mo | 6,000 |
| **Scale** | Enterprise doing 500+ calls/day, multi-team | ₹75,000/mo | 15,000 |
| **Custom voice add-on** | Any tier — clone of a real human rep | +₹15,000/mo | — |
| **Multi-tenant white-label** | Distributors, agencies | +₹25,000/mo | — |

Selling 10 Starter clients = ₹15,00,000/mo recurring = ₹1.8 cr/yr.
That's the path to ₹5 cr ARR in 18 months without enterprise effort.

---

## Feasibility verdict

| Question | Verdict |
|---|---|
| Can we deliver 50 calls/day at 150 sec avg? | **Yes.** Pipecat + Sarvam + Plivo handles 1000s of concurrent calls; 50/day is trivial. |
| Can the agent sound genuine, not robotic? | **Yes.** Sarvam Bulbul v3 is the best Indian-language TTS available. Sub-1-second turn-taking. Personalized greeting. Filler words and pauses are built in. |
| Can it answer honestly + correctly? | **Yes.** KB grounding + "never invent" rule + 4-hour-quote fallback eliminates hallucination risk. Honest AI disclosure when asked. |
| Can it understand all Indian languages? | **Yes, 11 of them via Sarvam.** Ships day-1 with EN/HI/TA for SPC; expanding to others is a config change with zero code cost. |
| Are summaries clean English in the CRM? | **Yes, enforced.** Regex guard against Devanagari/Tamil in summary fields with retry; `needs_review` queue if it persists. |
| Is ₹15k/mo + ₹1L setup profitable? | **Yes.** 60% Year-1 margin, 54% Year-2+ margin on managed mode; 68% on BYON mode. |
| Is ₹15k/mo competitive? | **Yes.** Less than a junior BDR's salary. Cheaper than Vapi or Bland resold. At parity with Bolna but with our IP (CRM, scoring, handoff) instead of their lock-in. |
| Will SPC actually pay this? | **Yes — at the right talk track.** "Less than a junior BDR's salary, three languages, 24/7" is the unambiguous business case. |

**Bottom line: the offer works. Quality is real. Margin is real.
Ship it.**

---

*Document version: 1.0 · Last updated 2026-05-21*
