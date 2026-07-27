<script setup>
// The demo driver. Chat is the only customer surface, so without
// this you have to read seed/README.md to find an order id, and you cannot see
// what a gate did to a balance without opening /admin.
//
// Every verdict here comes from the policy engine, not from rules restated in
// JavaScript — a threshold changed in /admin shows up on the next refresh.
import { onMounted, ref } from 'vue'
import { api, money } from '../api'

const emit = defineEmits(['ask'])

const data = ref(null)
const error = ref('')
const openId = ref(null)

async function load() {
  try {
    data.value = await api.me()
    error.value = ''
  } catch (err) {
    error.value = err.message
  }
}

onMounted(load)
defineExpose({ refresh: load })

function toggle(id) {
  openId.value = openId.value === id ? null : id
}

const VERDICT = {
  ALLOW: { label: 'returnable', cls: 'good' },
  REQUIRES_APPROVAL: { label: 'needs approval', cls: 'warn' },
  DENY: { label: 'not returnable', cls: 'bad' }
}

// Three independent axes, deliberately not collapsed into one field. `status`
// says where the goods are, the payment says where the money is, and a return
// is a process running alongside both — a damage refund leaves an order
// DELIVERED and refunded at once, and flattening that would erase the fact that
// the customer still has the item.
const FULFILMENT = {
  PROCESSING: { label: 'processing', cls: '' },
  IN_TRANSIT: { label: 'in transit', cls: 'warn' },
  DELIVERED: { label: 'delivered', cls: 'good' },
  RETURN_IN_PROGRESS: { label: 'return open', cls: 'warn' },
  RETURNED: { label: 'returned', cls: '' },
  CANCELLED: { label: 'cancelled', cls: 'bad' }
}

function fulfilment(order) {
  return FULFILMENT[order.status] ||
    { label: order.status.toLowerCase().replace(/_/g, ' '), cls: '' }
}

// Null until money has actually moved, so the pill only appears when it says
// something. This reads the same `refundable_cents` the policy engine gates on,
// which is why a fully refunded order stops accepting refund requests.
function refundState(order) {
  const paid = order.payment
  if (!paid || !paid.refunded_cents) return null
  if (paid.refundable_cents === 0) return { label: 'refunded', cls: 'good' }
  return { label: `${money(paid.refunded_cents)} refunded`, cls: 'warn' }
}

function returnState(order) {
  const list = order.returns || []
  const latest = list[list.length - 1]
  return latest ? latest.status.toLowerCase().replace(/_/g, ' ') : null
}

// Refunds and returns are order-level here, so every line of an order shares its
// fate — but "which item is shipped, received or refunded" is read on the item,
// so that is where it goes.
function itemState(order) {
  return refundState(order) || fulfilment(order)
}

function verdict(order) {
  return VERDICT[order.return_policy.decision] || { label: '—', cls: '' }
}

function why(order) {
  return order.return_policy.reasons.map((r) => r.detail).join(' ')
}

function summary(order) {
  return order.line_items
    .map((li) => `${li.title}${li.size ? ` (${li.size})` : ''}`)
    .join(', ')
}

// One click should produce a message that actually exercises the branch the
// pill promises, which means naming the order id every time.
function prompts(order) {
  if (order.status === 'IN_TRANSIT' || order.status === 'PROCESSING') {
    return [`Where is my order ${order.id}?`]
  }
  const list = [
    `I want to return ${order.id}`,
    `I need a refund for ${order.id}`
  ]
  if (order.line_items.some((li) => li.size)) {
    list.push(`${order.id} does not fit, can I exchange it?`)
  }
  return list
}
</script>

<template>
  <aside class="account">
    <div v-if="error" class="banner" style="border-radius: var(--radius)">{{ error }}</div>

    <template v-if="data">
      <div class="account-head">
        <div class="row" style="justify-content: space-between">
          <strong>{{ data.customer.name }}</strong>
          <span class="pill">{{ data.customer.loyalty_tier }}</span>
        </div>
        <div class="muted" style="font-size: 12.5px">{{ data.customer.email }}</div>
        <div class="muted" style="font-size: 12px">
          {{ data.customer.order_count }} orders ·
          {{ money(data.customer.lifetime_spend_cents) }} lifetime ·
          {{ data.customer.preferences.platform }} platform /
          {{ data.customer.preferences.case_form_factor }} build
        </div>
      </div>

      <!-- The two numbers that explain most of the verdicts below. -->
      <div class="account-policy">
        Returns within <strong>{{ data.policy.return_window_days }} days</strong> of delivery ·
        refunds under
        <strong>{{ money(data.policy.refund_auto_approve_under_cents) }}</strong>
        auto-approve
      </div>

      <div class="account-orders">
        <div v-for="order in data.orders" :key="order.id" class="order"
             :class="{ open: openId === order.id }">
          <button class="order-head" @click="toggle(order.id)">
            <div class="row" style="justify-content: space-between; width: 100%">
              <span class="mono">{{ order.id }}</span>
              <span>{{ money(order.total_cents) }}</span>
            </div>
            <div class="order-title">{{ summary(order) }}</div>
            <div class="row" style="gap: 6px">
              <span class="pill" :class="fulfilment(order).cls">{{ fulfilment(order).label }}</span>
              <span v-if="refundState(order)" class="pill" :class="refundState(order).cls">
                {{ refundState(order).label }}
              </span>
              <span v-if="returnState(order)" class="pill">return {{ returnState(order) }}</span>
              <span class="pill" :class="verdict(order).cls" :title="why(order)">
                {{ verdict(order).label }}
              </span>
              <span v-if="order.days_since_delivery != null" class="muted"
                    style="font-size: 11.5px">
                delivered {{ order.days_since_delivery }}d ago
              </span>
            </div>
          </button>

          <div v-if="openId === order.id" class="order-body">
            <div v-for="li in order.line_items" :key="li.sku" class="line-item">
              <div class="row" style="justify-content: space-between">
                <span>{{ li.quantity }}× {{ li.title }}</span>
                <span class="mono">{{ money(li.line_total_cents) }}</span>
              </div>
              <div class="row" style="gap: 6px">
                <span class="pill" :class="itemState(order).cls">{{ itemState(order).label }}</span>
                <span class="mono muted" style="font-size: 11px">{{ li.sku }}</span>
                <span v-if="li.size" class="muted" style="font-size: 11.5px">size {{ li.size }}</span>
                <span v-if="li.product_class === 'HIGH_VALUE'" class="pill warn">high value</span>
                <span v-if="li.final_sale" class="pill bad">final sale</span>
              </div>
            </div>

            <div v-if="order.payment" class="muted" style="font-size: 12px">
              paid {{ money(order.payment.amount_cents) }} ·
              refunded {{ money(order.payment.refunded_cents) }} ·
              <strong>{{ money(order.payment.refundable_cents) }} refundable</strong>
            </div>

            <div v-if="order.tracking_number" class="muted" style="font-size: 12px">
              {{ order.carrier }} {{ order.tracking_number }}
              <template v-if="order.tracking_status">— {{ order.tracking_status }}</template>
            </div>

            <div v-for="r in order.returns" :key="r.id" class="muted" style="font-size: 12px">
              {{ r.id }}: {{ r.status }}<template v-if="r.reason"> ({{ r.reason }})</template>
            </div>

            <!-- Why the verdict is what it is, in the engine's own words. -->
            <div v-for="r in order.return_policy.reasons" :key="r.rule"
                 class="reason" :class="r.decision === 'DENY' ? 'bad' : 'warn'">
              <span class="mono">{{ r.policy_id }}</span> {{ r.detail }}
            </div>

            <div class="row" style="gap: 6px">
              <button v-for="p in prompts(order)" :key="p" class="ghost tiny"
                      @click="emit('ask', p)">
                {{ p }}
              </button>
            </div>
          </div>
        </div>
      </div>

      <button class="ghost tiny" style="align-self: flex-start" @click="load">refresh</button>
    </template>
  </aside>
</template>
