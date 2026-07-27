<script setup>
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { api, subscribe } from '../api'
import ApprovalQueue from './ApprovalQueue.vue'
import MetricsPanel from './MetricsPanel.vue'
import LogTable from './LogTable.vue'
import KnowledgePanel from './KnowledgePanel.vue'
import PolicyRules from './PolicyRules.vue'

const tab = ref('approvals')
const pendingCount = ref(0)
let source = null

const tabs = [
  ['approvals', 'Approvals'],
  ['metrics', 'Metrics'],
  ['api', 'Simulated API calls'],
  ['llm', 'LLM calls'],
  ['audit', 'Audit log'],
  ['email', 'Email outbox'],
  ['orders', 'Orders'],
  ['knowledge', 'Knowledge base'],
  ['policy', 'Policy rules']
]

async function refreshCount() {
  try {
    const data = await api.pending('PENDING')
    pendingCount.value = data.actions.length
  } catch {
    pendingCount.value = 0
  }
}

onMounted(() => {
  refreshCount()
  // `*` is the firehose: any conversation's escalation updates this badge live.
  source = subscribe('*', {
    awaiting_approval: refreshCount,
    approval_resolved: refreshCount
  })
})

onBeforeUnmount(() => source && source.close())
</script>

<template>
  <div class="admin">
    <div class="row">
      <h2 style="margin: 0; font-size: 18px">Admin</h2>
      <span v-if="pendingCount" class="pill warn">{{ pendingCount }} awaiting approval</span>
    </div>

    <div class="tabs">
      <button v-for="[key, label] in tabs" :key="key"
              :class="{ active: tab === key }" @click="tab = key">
        {{ label }}
      </button>
    </div>

    <ApprovalQueue v-if="tab === 'approvals'" @resolved="refreshCount" />
    <MetricsPanel v-else-if="tab === 'metrics'" />
    <KnowledgePanel v-else-if="tab === 'knowledge'" />
    <PolicyRules v-else-if="tab === 'policy'" />

    <LogTable v-else-if="tab === 'api'" :loader="api.apiLogs" collection="logs"
              :columns="[
                { key: 'created_at', label: 'When', type: 'time' },
                { key: 'system', label: 'System' },
                { key: 'operation', label: 'Operation' },
                { key: 'ok', label: 'OK', type: 'bool' },
                { key: 'error_code', label: 'Error' },
                { key: 'latency_ms', label: 'ms' },
                { key: 'request', label: 'Request', type: 'json' },
                { key: 'response', label: 'Response', type: 'json' }
              ]" />

    <LogTable v-else-if="tab === 'llm'" :loader="api.llmCalls" collection="calls"
              :columns="[
                { key: 'created_at', label: 'When', type: 'time' },
                { key: 'agent', label: 'Agent' },
                { key: 'model', label: 'Model' },
                { key: 'input_tokens', label: 'In' },
                { key: 'output_tokens', label: 'Out' },
                { key: 'cost_usd', label: 'USD' },
                { key: 'latency_ms', label: 'ms' },
                { key: 'time_to_first_token_ms', label: 'TTFT' },
                { key: 'ok', label: 'OK', type: 'bool' },
                { key: 'error', label: 'Error' }
              ]" />

    <LogTable v-else-if="tab === 'audit'" :loader="api.auditLogs" collection="logs"
              :columns="[
                { key: 'created_at', label: 'When', type: 'time' },
                { key: 'event', label: 'Event' },
                { key: 'actor', label: 'Actor' },
                { key: 'conversation_id', label: 'Conversation' },
                { key: 'subject_id', label: 'Subject' },
                { key: 'detail', label: 'Detail', type: 'json' }
              ]" />

    <LogTable v-else-if="tab === 'email'" :loader="api.emails" collection="emails"
              :columns="[
                { key: 'created_at', label: 'When', type: 'time' },
                { key: 'to', label: 'To' },
                { key: 'template', label: 'Template' },
                { key: 'subject', label: 'Subject' },
                { key: 'order_id', label: 'Order' },
                { key: 'body', label: 'Body' },
                { key: 'attachments', label: 'Attachments', type: 'json' }
              ]" />

    <LogTable v-else-if="tab === 'orders'" :loader="api.orders" collection="orders"
              :columns="[
                { key: 'id', label: 'Order' },
                { key: 'status', label: 'Fulfilment' },
                { key: 'return_state', label: 'Return' },
                { key: 'refunded_cents', label: 'Refunded', type: 'money' },
                { key: 'total_cents', label: 'Total', type: 'money' },
                { key: 'delivered_at', label: 'Delivered', type: 'time' },
                { key: 'tracking_status', label: 'Tracking' },
                { key: 'fraud_flagged', label: 'Fraud', type: 'bool' },
                { key: 'line_items', label: 'Items', type: 'json' }
              ]" />
  </div>
</template>
