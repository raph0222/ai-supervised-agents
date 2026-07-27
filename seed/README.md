# Seed Data

Everything needed to make the app usable on first run.

Fictional brand: **Northbridge Components**, a DTC retailer of PC parts — cases, cooling,
motherboards, memory, graphics cards and processors.

```
seed/
  knowledge/    → chunked + embedded into pgvector (the RAG corpus)
  data/         → loaded into the relational tables and the fake-external-system tables
```

## Conventions

**Dates are relative, never absolute.** Orders use fields like `delivered_days_ago: 5`. The
seeder resolves them against `now()` at seed time. This keeps the "inside / outside the
30-day return window" scenarios valid no matter when the demo is run — hardcoded dates would
silently rot and every order would drift out of policy.

**Money is in minor units** (cents, integers). `12800` = $128.00. Matches how Stripe models
amounts.

**Product class drives the gates.** `HIGH_VALUE` (graphics cards, processors) always requires
human approval regardless of amount; `final_sale` blocks returns and exchanges outright.

**Rigged failures are explicit.** Any object with a `_simulate` key is one of the deterministic
failure cases. The key is metadata for the simulator, not a column, the seeder reads
it and configures the fake integration layer, it does not persist it as data.

## Scenario matrix

Every path the agent can take has at least one seeded order that exercises it. Use this as the
walkthrough when clicking through the app.

| Order | Scenario | Expected outcome |
|---|---|---|
| `ORD-1001` | Return, case, delivered 5d ago, $128 | In window → return created → refund > $50 → **pauses for /admin approval** |
| `ORD-1002` | Return, fan pack, delivered 62d ago | Outside 30-day window → **policy denial**, no tool call |
| `ORD-1003` | Refund, thermal compound, $28 | Under $50 → **auto-approved**, no admin step |
| `ORD-1004` | WISMO, motherboard in transit | Tracking + delivery estimate, no write |
| `ORD-1005` | Return, clearance DDR4 kit | **Final sale, non-returnable**, agent explains and offers store credit |
| `ORD-1006` | Return, graphics card $780 | High-value → **always /admin**, regardless of amount |
| `ORD-1007` | Exchange, memory kit black → white | Target colour out of stock → **fallback** to refund or backorder |
| `ORD-1008` | Refund, AIO cooler $94 | Simulator returns `card_declined` → **retry → escalate to /admin** |
| `ORD-1009` | — | Prior completed return, feeds long-term memory |
| `ORD-1010` | Any | `GetOrder` returns a malformed payload → **fallback path** |
| `ORD-1011` | Missing package | Marked delivered, customer says not received → claim flow |
| `ORD-1012` | Warranty, case 8 months old | Inside the 2-year chassis warranty → warranty claim |

## Re-running

Nothing to run by hand: the API seeds the relational data when the tables are empty and
re-ingests `knowledge/` on every boot. See the root `README.md` for the reset and
knowledge-only commands, and `python scripts/seed.py --help` for the rest.