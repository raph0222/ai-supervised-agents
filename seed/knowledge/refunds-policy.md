---
policy_id: POL-REF-001
title: Refunds and Approval Thresholds
category: refunds
version: 2.8
effective_date: 2026-03-01
authority: binding
applies_to: [REFUND, RETURN, EXCHANGE]
---

# Refunds and Approval Thresholds

## Approval thresholds

This is the single most important rule set in the system. Refund authority is determined by
amount and product class.

| Condition | Authority |
|---|---|
| Refund **under {{refund_auto_approve_under_cents|money}}** | Auto-approved. The agent may issue it directly. |
| Refund **of {{refund_auto_approve_under_cents|money}} or more** | Requires human approval before the refund is issued. |
| Any **High-Value** class item | Requires human approval, regardless of amount. |
| Any order flagged for **fraud review** | Requires human approval. |
| Customer **risk score above {{risk_score_approval_threshold}}** | Requires human approval. |

The threshold is evaluated on the **total refund exposure for the order** — this request, plus
everything already refunded against it, plus everything already sitting in the approval queue —
not per line item and not per transaction. Two items refunded together count as one refund of
their combined value. Splitting a refund into smaller transactions to stay under the threshold
is prohibited, and does not work: the amounts are added back together before the comparison.

When approval is required, the agent must not call the refund tool. It creates a pending action
with its reasoning and the relevant policy, tells the customer the request is under review, and
stops. The refund executes only once approved.

## Refund method

Refunds are always issued to the **original payment method**. There is no exception. If the
original card is expired or cancelled, the payment processor still routes the refund to the
issuing bank, which forwards it to the customer's replacement card. Never offer to refund to a
different card, to a bank transfer, or to a third party.

Store credit may be offered as an **alternative** when the customer prefers it, or when the
item is non-returnable and a goodwill gesture is warranted. Store credit is issued instantly
and never expires.

## What is refunded

- **Item price**: always
- **Sales tax**: always, proportional to the refunded item
- **Original outbound shipping**: only when the item was defective, damaged, or shipped in
  error. Not refunded for change-of-mind returns.
- **Return shipping fee**: deducted from the refund for Standard members, waived for Gold and
  Platinum, and waived for everyone on defective or damaged items

## Timing

Once approved and issued, refunds take **5–10 business days** to appear on the customer's
statement. This is controlled by the issuing bank and cannot be accelerated. Do not promise a
faster timeline.

Customers frequently ask why the refund has not appeared. If the refund was issued fewer than
10 business days ago, explain the timing and provide the refund reference. Do not re-issue —
duplicate refunds are a serious error.

## Partial refunds

A partial refund may be issued without a physical return when:

- An item arrived with **minor cosmetic damage** the customer is willing to keep — up to 30%
  of item value
- An order was **missing an item** but the customer only wants the missing item's value back
- A **price adjustment** applies: an item went on sale within 7 days of purchase, refund the
  difference

Partial refunds follow the same approval thresholds as full refunds.

## Declined refunds

If the payment processor declines the refund, do not retry more than
**{{max_refund_retries|times}}**. A persistent
decline usually means the original charge was already reversed, disputed, or is still pending
settlement. Escalate to a human with the processor's error code — do not tell the customer the
refund succeeded.

## Prohibited

- Issuing a refund without verifying the order and payment exist
- Issuing a refund on an order that has already been fully refunded
- Refunding an amount greater than the original charge
- Issuing a refund on a chargeback or disputed payment — these are handled by the disputes team
