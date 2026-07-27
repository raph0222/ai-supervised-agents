<script setup>
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { api, money, shortTime, subscribe } from '../api'
import AccountPanel from './AccountPanel.vue'
import MessageTrace from './MessageTrace.vue'

const conversationId = ref(null)
const messages = ref([])
const draft = ref('')
const busy = ref(false)
const error = ref('')
const awaitingApproval = ref(false)
const listEl = ref(null)
const composerEl = ref(null)
const panel = ref(null)
let source = null

// The seeded scenario matrix (seed/README.md) is the demo script. Three
// examples — return, order status, refund — beat seven: they cover the branches
// worth seeing without turning the composer into a menu. The rest of the matrix
// stays reachable by typing, and the account panel still proposes per-order.
const suggestions = [
  'I want to return the case from ORD-1001',
  'Where is my order ORD-1004?',
  'I need a refund for ORD-1003'
]

onMounted(async () => {
  try {
    const data = await api.currentConversation()
    conversationId.value = data.conversation_id
    messages.value = data.messages
    connect()
    scroll()
  } catch (err) {
    error.value = err.message
  }
})

onBeforeUnmount(() => source && source.close())

function connect() {
  source && source.close()
  source = subscribe(conversationId.value, {
    message: (event) => {
      // Our own POST already appended the pair; only take what we do not have.
      if (messages.value.some((m) => m.id === event.id)) return
      // The optimistic copy `send` pushed carries a local id, so the id check
      // above cannot match it and the message rendered twice for the length of
      // the turn. Adopt the persisted row into that placeholder instead.
      const pending = messages.value.find(
        (m) => String(m.id).startsWith('local-') &&
               m.role === event.role &&
               m.content === event.content
      )
      if (pending) {
        pending.id = event.id
        pending.agent = event.agent
        pending.meta = event.meta || {}
        pending.created_at = event.created_at
        return
      }
      messages.value.push({
        id: event.id,
        role: event.role,
        content: event.content,
        agent: event.agent,
        meta: event.meta || {},
        created_at: event.created_at
      })
      scroll()
    },
    awaiting_approval: () => {
      awaitingApproval.value = true
    },
    approval_resolved: () => {
      awaitingApproval.value = false
      // An approval executes the action, so the balances in the panel moved.
      panel.value?.refresh()
    }
  })
}

// The panel proposes, the composer disposes: one click loads the message so it
// can still be edited before a turn is spent on it.
function ask(text) {
  draft.value = text
  nextTick(() => composerEl.value?.focus())
}

async function send(text) {
  const content = (text ?? draft.value).trim()
  if (!content || busy.value) return
  draft.value = ''
  error.value = ''
  busy.value = true

  messages.value.push({ id: `local-${Date.now()}`, role: 'user', content, meta: {} })
  scroll()

  try {
    const result = await api.send(content, conversationId.value)
    conversationId.value = result.conversation_id
    awaitingApproval.value = result.status === 'AWAITING_APPROVAL'
    // The stream delivers the assistant turn; refresh once so ids reconcile
    // whether or not the event arrived first.
    const data = await api.conversation(conversationId.value)
    messages.value = data.messages
  } catch (err) {
    error.value = err.message
  } finally {
    busy.value = false
    scroll()
    // A turn may have issued a refund or opened a return; show it.
    panel.value?.refresh()
  }
}

async function reset() {
  const data = await api.newConversation()
  conversationId.value = data.conversation_id
  messages.value = []
  awaitingApproval.value = false
  connect()
}

function onKeydown(event) {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault()
    send()
  }
}

function scroll() {
  nextTick(() => {
    if (listEl.value) listEl.value.scrollTop = listEl.value.scrollHeight
  })
}

// What the tool layer actually did, said by us and not by the model. The reply
// above is generated prose; these lines are derived from an EXECUTED tool
// result, so they cannot promise money that never moved — and when an approval
// is what finally issued the refund, this is where the customer learns it.
const OUTCOMES = {
  'Stripe.RefundPayment': (r) =>
    `Refund of ${money(r.amount_cents)} issued. It goes back to your original ` +
    'payment method — allow 5–10 business days for it to appear.',
  'Stripe.CreateAdjustment': (r) =>
    `Partial refund of ${money(r.amount_cents)} issued. Allow 5–10 business ` +
    'days for it to reach your original payment method.',
  'Shopify.CreateReturn': (r) =>
    `Return ${r.return_id} created` +
    (r.return_shipping_fee_cents
      ? `, ${money(r.return_shipping_fee_cents)} return shipping.`
      : ', with return shipping on us.'),
  'Shopify.CreateExchange': (r) => `Exchange ${r.exchange_id} created.`,
  'Shopify.CancelOrder': (r) => `Order ${r.order_id} cancelled.`
}

function outcomes(message) {
  return (message.meta?.tool_results || [])
    .filter((t) => t.status === 'EXECUTED' && OUTCOMES[t.tool])
    .map((t) => OUTCOMES[t.tool](t.result || {}))
}
</script>

<template>
  <div class="chat-layout">
    <div class="chat">
      <!-- Entering the page always resumes the current conversation, so the way
           back to a blank one has to be visible rather than buried in the
           suggestion row. -->
      <div class="chat-head">
        <span class="head-label">Conversation</span>
        <button class="ghost new-convo" :disabled="busy" @click="reset">
          + New conversation
        </button>
      </div>

      <div ref="listEl" class="messages">
        <div v-if="!messages.length" class="empty">
          Ask about an order, a return, a refund or a policy. You are always the
          same seeded customer
        </div>

        <div v-for="message in messages" :key="message.id"
             class="msg" :class="message.role === 'user' ? 'user' : message.agent === 'system' ? 'system' : 'assistant'">
          <div class="bubble">{{ message.content }}</div>
          <div v-for="(line, i) in outcomes(message)" :key="i" class="outcome">{{ line }}</div>
          <div class="meta">
            <span v-if="message.agent && message.agent !== 'system'" class="pill">{{ message.agent }}</span>
            <span v-if="message.meta?.intent" class="pill">{{ message.meta.intent }}</span>
            <span v-if="message.meta?.confidence != null" class="pill">
              conf {{ Number(message.meta.confidence).toFixed(2) }}
            </span>
            <span v-for="p in message.meta?.cited_policies || []" :key="p" class="pill good">{{ p }}</span>
            <span>{{ shortTime(message.created_at) }}</span>
          </div>
          <MessageTrace v-if="message.role === 'assistant'" :meta="message.meta || {}" />
        </div>

        <div v-if="busy" class="thinking">
          <span class="dot" /><span class="dot" /><span class="dot" />
          routing, planning, retrieving policy…
        </div>

        <div v-if="awaitingApproval" class="banner" style="border-radius: 10px; border: 1px solid var(--border)">
          This turn is parked awaiting approval. Open
          <a href="/admin">/admin</a>, approve or reject it, and the outcome lands
          here without a reload.
        </div>

        <div v-if="error" class="banner" style="border-radius: 10px">{{ error }}</div>
      </div>

      <div class="suggestions">
        <button v-for="s in suggestions" :key="s" class="ghost" :disabled="busy" @click="send(s)">
          {{ s }}
        </button>
      </div>

      <div class="composer">
        <textarea ref="composerEl" v-model="draft" rows="1"
                  placeholder="Type a message…  (Enter to send)"
                  :disabled="busy" @keydown="onKeydown" />
        <button class="primary" :disabled="busy || !draft.trim()" @click="send()">Send</button>
      </div>
    </div>

    <AccountPanel ref="panel" @ask="ask" />
  </div>
</template>
