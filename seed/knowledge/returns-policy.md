---
policy_id: POL-RET-001
title: Returns Policy
category: returns
version: 3.2
effective_date: 2026-01-15
authority: binding
applies_to: [RETURN, EXCHANGE]
---

# Returns Policy

## Return window

Northbridge Components accepts returns within **{{return_window_days}} calendar days of the delivery
date**. The window is measured from the date the carrier marked the parcel delivered, not the
order date and not the ship date.

Orders delivered more than {{return_window_days}} days ago are **outside the return window** and are not eligible
for a standard return. Do not create a return for these. The customer may still be eligible
under the Warranty Policy (POL-WAR-001) if the issue is a manufacturing defect, or under the
Damaged & Missing Items Policy (POL-DMG-001) if the item arrived damaged.

Loyalty tier does not extend the return window. Gold and Platinum members receive free return
shipping, not more time.

## Condition requirements

To be accepted, a returned item must be:

- **Unused and undamaged**, with no signs of installation wear, modification or overclocking
- In its **original packaging**, with the anti-static bag, brackets, cables and manuals included
- With the **serial and warranty labels intact** and matching the ones we shipped
- Free of thermal compound residue on the heat spreader or cold plate

Components must be test-fitted on an anti-static surface. Boards and processors with bent socket
pins, and cards with damaged PCIe fingers, are rejected at inspection.

## Non-returnable items

The following are final and cannot be returned under any circumstance:

- Items marked **FINAL SALE** on the product page and the order confirmation
- Gift cards
- Software licence keys, once the key has been revealed or activated
- Custom-built systems and custom-sleeved cable sets

If a customer requests a return on a final-sale item, explain that it is non-returnable and
offer **store credit at 50% of the item value** as a goodwill gesture. This offer does not
require approval and may be extended once per customer per calendar year.

## High-value items

Items in the **High-Value** product class (graphics cards and processors above $500) follow the
same {{return_window_days}}-day window but require **bench testing and manual inspection before any refund is
issued**. Every high-value return requires human approval, regardless of the refund amount. See
the Refunds Policy.

The inspection exists because a returned card or processor has to be confirmed as the one we
shipped, working and unmodified — serial match, no missing cooler, no reflashed firmware.

High-value returns are shipped to the inspection facility, not the standard returns warehouse.
The return label generated for a high-value item routes there automatically.

## Return shipping

- Standard members: a **$7.95 return shipping fee** is deducted from the refund
- Gold and Platinum members: **free return shipping**
- Defective, damaged or incorrectly shipped items: **free return shipping for everyone**, and
  the original outbound shipping cost is refunded as well

## Process

1. Verify the order exists and belongs to the customer
2. Confirm the delivery date is within {{return_window_days}} days
3. Confirm the item is not final sale and is not otherwise excluded
4. Ask the customer to confirm the item is unused and in its original packaging
5. Create the return and generate a prepaid label
6. Email the label to the customer
7. Log a ticket in the CRM

The customer has **14 days from label issue** to ship the item back. Unused labels expire and
the return is cancelled automatically.

## Refund timing

Refunds are issued once the returned item is received and passes inspection at the warehouse,
typically 3–5 business days after delivery to the warehouse. See the Refunds Policy for how the
money is returned and how long it takes to appear.
