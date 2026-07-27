---
policy_id: POL-EXC-001
title: Exchanges Policy
category: exchanges
version: 2.1
effective_date: 2026-01-15
authority: binding
applies_to: [EXCHANGE]
---

# Exchanges Policy

## Eligibility

Exchanges follow the **same {{return_window_days}}-day window and the same condition requirements** as returns
(POL-RET-001). Final-sale items cannot be exchanged.

An exchange is free of charge — no return shipping fee for any loyalty tier, and no outbound
shipping charge on the replacement.

**One exchange per order line.** If a customer wants to exchange an item that was itself
received as an exchange, treat it as a return and refund instead.

## What can be exchanged for what

- **Different colour or finish, same product and specification**: always allowed, no price
  difference
- **Different form factor or capacity within the same product line** (Mid Tower to Full Tower,
  240mm to 360mm): allowed, no price difference
- **A different product entirely**: not an exchange. Process a return and refund, then let the
  customer place a new order. Do not attempt to swap products of different value.

## Inventory

Before promising an exchange, **check inventory for the exact target variant**. Availability
changes constantly and a promised exchange that cannot be fulfilled is a serious service
failure.

If the target variant is **out of stock**:

1. Check whether a restock date exists. If it is within {{exchange_restock_window_days}} days,
   offer to **backorder** — the customer keeps the original item until the replacement ships.
2. If there is no restock date or it is more than {{exchange_restock_window_days}} days out,
   offer a **standard return and
   refund** instead, and waive the return shipping fee regardless of loyalty tier.
3. Never place an exchange order against zero inventory.

## Compatibility guidance

Most "wrong item" exchanges are really fitment problems, and the fix is usually one step up in
the same product line rather than a different product:

- **It does not fit the case.** The Atlas Mid Tower clears 330mm cards and 165mm coolers, the
  Full Tower 400mm and 185mm. A customer whose card or cooler fouls the fans wants the Full
  Tower, not a different case.
- **It does not fit the case, the other way round.** An ATX motherboard does not mount in a
  Mini-ITX chassis. Confirm the case before exchanging a board for a different form factor.
- **It does not post.** DDR5 kits do not work in DDR4 boards and vice versa — that is a return,
  not an exchange, unless the customer wants the matching generation of the same kit.
- **Mixed memory.** Never recommend adding a second kit alongside an existing one. Exchange for
  a single matched kit of the capacity the customer actually wants.

Recommend at most **one step** at a time. If the customer has already exchanged once for
fitment and it is still wrong, the product line likely does not suit their build; offer a refund.

## Process

1. Verify the order, the delivery date, and the item's eligibility
2. Confirm the target variant with the customer — product, specification, colour
3. Check inventory for that exact variant
4. Create the exchange, which reserves the replacement and generates a return label
5. Email the label and the exchange confirmation
6. Log a ticket in the CRM

The replacement ships as soon as the original is scanned by the carrier — the customer does not
wait for warehouse receipt.

## Price differences

Exchanges are **even swaps only**. If the target variant has a different price than the item
purchased — for example the original was bought on sale — the exchange is still processed at no
additional charge and no partial refund. Do not collect a payment and do not refund a
difference on an exchange.
