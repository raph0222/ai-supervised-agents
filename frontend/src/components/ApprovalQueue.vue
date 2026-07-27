<script setup>
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { api, money, stamp, subscribe } from '../api'

// The escalation package, rendered. A reviewer should be able to decide
// from this screen alone: what was proposed, why it stopped, who the customer
// is, what the agent was thinking, and which policy applies.
const emit = defineEmits(['resolved'])

const actions = ref([])
const selected = ref(null)
const detail = ref(null)
const note = ref('')
const busy = ref(false)
const error = ref('')
const filter = ref('PENDING')
let source = null

async function load() {
  try {
    const data = await api.pending(filter.value || undefined)
    actions.value = data.actions
    if (selected.value && !actions.value.some((a) => a.id === selected.value)) {
      selected.value = null
      detail.value = null
    }
  } catch (err) {
    error.value = err.message
  }
}

async function select(id) {
  selected.value = id
  detail.value = null
  detail.value = await api.pendingDetail(id)
}

async function decide(approved) {
  if (!selected.value || busy.value) return
  busy.value = true
  error.value = ''
  try {
    await (approved ? api.approve(selected.value, note.value) : api.reject(selected.value, note.value))
    note.value = ''
    await load()
    if (selected.value) await select(selected.value)
    emit('resolved')
  } catch (err) {
    error.value = err.message
  } finally {
    busy.value = false
  }
}

onMounted(() => {
  load()
  source = subscribe('*', { awaiting_approval: load, approval_resolved: load })
})
onBeforeUnmount(() => source && source.close())
</script>

<template>
  <div class="section">
    <div class="row">
      <select v-model="filter" style="width: auto" @change="load">
        <option value="PENDING">Pending</option>
        <option value="APPROVED">Approved</option>
        <option value="REJECTED">Rejected</option>
        <option value="EXECUTED">Executed</option>
        <option value="">All</option>
      </select>
      <button class="ghost" @click="load">Refresh</button>
      <span v-if="error" class="pill bad">{{ error }}</span>
    </div>

    <div class="queue">
      <div class="scroll">
        <div v-if="!actions.length" class="empty">
          Nothing here. Escalations appear the moment a workflow parks —
          try a return on ORD-1001 or ORD-1006 in the chat.
        </div>
        <div v-for="action in actions" :key="action.id"
             class="action-row" :class="{ selected: selected === action.id }"
             @click="select(action.id)">
          <div class="title">
            <strong>{{ action.tool_name }}</strong>
            <span class="pill" :class="action.priority === 'HIGH' ? 'bad' : ''">{{ action.priority }}</span>
          </div>
          <div class="muted" style="font-size: 12.5px; margin-top: 4px">
            {{ action.summary || action.reason }}
          </div>
          <div class="row" style="margin-top: 6px; gap: 6px">
            <span class="pill">{{ action.status }}</span>
            <span v-if="action.order_id" class="pill">{{ action.order_id }}</span>
            <span v-if="action.amount_cents" class="pill">{{ money(action.amount_cents) }}</span>
            <span class="muted" style="font-size: 11.5px">{{ stamp(action.created_at) }}</span>
          </div>
        </div>
      </div>

      <div v-if="detail" class="card">
        <h3>Escalation package</h3>
        <div class="section">
          <p style="margin: 0">{{ detail.escalation_package?.summary }}</p>

          <div class="row">
            <span class="pill">{{ detail.escalation_package?.intent }}</span>
            <span class="pill">confidence {{ detail.escalation_package?.confidence }}</span>
            <span class="pill">{{ detail.escalation_package?.sentiment }}</span>
            <span class="pill warn">{{ detail.escalation_package?.reason }}</span>
          </div>

          <h4>Suggested action</h4>
          <pre class="json">{{ JSON.stringify(detail.escalation_package?.suggested_action, null, 2) }}</pre>

          <h4>Why it stopped</h4>
          <div v-if="!detail.policy_reasons?.length" class="muted">
            No policy verdict — escalated on a routing trigger.
          </div>
          <div v-for="(reason, i) in detail.policy_reasons || []" :key="i" class="trace blocked">
            {{ reason.rule }} ({{ reason.policy_id }}) — {{ reason.detail }}
          </div>

          <h4>Agent reasoning</h4>
          <p class="muted" style="margin: 0">
            {{ detail.escalation_package?.agent_reasoning || '—' }}
          </p>

          <h4>Plan</h4>
          <div v-for="(step, i) in detail.escalation_package?.plan || []" :key="i" class="trace">
            {{ step.n || i + 1 }}. {{ step.action }}
            <span v-if="step.tool" class="muted">[{{ step.tool }}]</span>
          </div>

          <h4>Customer</h4>
          <pre class="json">{{ JSON.stringify(detail.escalation_package?.customer_profile, null, 2) }}</pre>

          <h4>Conversation</h4>
          <div class="scroll" style="max-height: 200px; padding: 8px 10px">
            <div v-for="(turn, i) in detail.escalation_package?.conversation || []" :key="i"
                 style="margin-bottom: 6px">
              <span class="pill">{{ turn.role }}</span> {{ turn.content }}
            </div>
          </div>

          <h4>Relevant policy</h4>
          <details v-for="(policy, i) in detail.escalation_package?.relevant_policy || []" :key="i"
                   class="details">
            <summary>{{ policy.policy_id }} — {{ policy.heading }}</summary>
            <div class="body"><pre class="json">{{ policy.content }}</pre></div>
          </details>

          <template v-if="detail.execution_result">
            <h4>Execution result</h4>
            <pre class="json">{{ JSON.stringify(detail.execution_result, null, 2) }}</pre>
          </template>

          <template v-if="detail.status === 'PENDING'">
            <h4>Decision</h4>
            <input v-model="note" placeholder="Note (optional, recorded in the audit log)" />
            <div class="row">
              <button class="primary" :disabled="busy" @click="decide(true)">
                Approve and execute
              </button>
              <button class="danger" :disabled="busy" @click="decide(false)">Reject</button>
            </div>
            <p class="muted" style="margin: 0; font-size: 12px">
              Approving resumes the checkpointed workflow and executes exactly
              the call shown above — once. Rejecting resumes it down the refusal
              branch and executes nothing.
            </p>
          </template>
          <div v-else class="pill" :class="detail.status === 'EXECUTED' ? 'good' : ''">
            {{ detail.status }}{{ detail.executed_at ? ` at ${stamp(detail.executed_at)}` : '' }}
          </div>
        </div>
      </div>

      <div v-else class="card empty">Select an action to see its escalation package.</div>
    </div>
  </div>
</template>
