<script setup>
import { onMounted, onBeforeUnmount, ref } from 'vue'
import { api, stamp } from '../api'

// Observability without Grafana: the numbers come straight out of Postgres and
// are shown next to the targets, which are goals rather than gates in a
// single-user demo — so this reports, it does not alarm.
const metrics = ref(null)
const activity = ref([])
let timer = null

const tiles = [
  ['automation_rate_pct', 'Automation rate', '%'],
  ['escalation_rate_pct', 'Escalation rate', '%'],
  ['avg_response_ms', 'Avg response', 'ms'],
  ['p95_response_ms', 'P95 response', 'ms'],
  ['csat', 'CSAT', ''],
  ['success_rate_pct', 'Tool success', '%'],
  ['avg_tools_per_turn', 'Tools / turn', ''],
  ['cost_usd', 'LLM spend', 'usd']
]

async function load() {
  try {
    metrics.value = await api.metrics()
    activity.value = (await api.activity()).events
  } catch {
    // the panel is read-only; a transient failure just leaves the last values
  }
}

function verdict(key) {
  const target = metrics.value?.targets?.[key]
  if (!target) return null
  const value = metrics.value[key]
  if (value === null || value === undefined) return null
  const met = target.direction === 'min' ? value >= target.target : value <= target.target
  return { met, text: `${target.direction === 'min' ? '≥' : '≤'} ${target.target}` }
}

function format(key, unit) {
  const value = metrics.value?.[key]
  if (value === null || value === undefined) return '—'
  if (unit === 'usd') return `$${Number(value).toFixed(4)}`
  return `${value}${unit === '%' ? '%' : unit === 'ms' ? 'ms' : ''}`
}

onMounted(() => {
  load()
  timer = setInterval(load, 5000)
})
onBeforeUnmount(() => clearInterval(timer))
</script>

<template>
  <div v-if="metrics" class="section">
    <div class="grid">
      <div v-for="[key, label, unit] in tiles" :key="key" class="card stat">
        <div class="value">{{ format(key, unit) }}</div>
        <div class="label">{{ label }}</div>
        <div v-if="verdict(key)" class="target">
          <span class="pill" :class="verdict(key).met ? 'good' : 'warn'">
            target {{ verdict(key).text }}
          </span>
        </div>
      </div>
    </div>

    <div class="grid" style="grid-template-columns: repeat(auto-fill, minmax(240px, 1fr))">
      <div class="card">
        <h3>Volume</h3>
        <div class="mono">
          conversations {{ metrics.conversations }}<br />
          assistant turns {{ metrics.assistant_turns }}<br />
          escalations {{ metrics.escalations }} ({{ metrics.pending_approvals }} pending)<br />
          policy blocks {{ metrics.policy_blocks }}<br />
          prompt injections {{ metrics.prompt_injections_detected }}
        </div>
      </div>
      <div class="card">
        <h3>Model</h3>
        <div class="mono">
          calls {{ metrics.llm_calls }} ({{ metrics.llm_failures }} failed)<br />
          tokens in {{ metrics.input_tokens }} / out {{ metrics.output_tokens }}<br />
          avg call {{ metrics.avg_llm_call_ms }}ms<br />
          time to first token {{ metrics.avg_time_to_first_token_ms }}ms
        </div>
      </div>
      <div class="card">
        <h3>Tools</h3>
        <div class="mono">
          simulated calls {{ metrics.tool_calls }}<br />
          failures {{ metrics.tool_failures }}<br />
          avg latency {{ metrics.avg_tool_call_ms }}ms
        </div>
      </div>
      <div class="card">
        <h3>Retrieval</h3>
        <div class="mono">
          chunks {{ metrics.knowledge_chunks }}<br />
          embedded {{ metrics.knowledge_embedded }}<br />
          mode {{ metrics.retrieval_mode }}
        </div>
      </div>
    </div>

    <div class="card">
      <h3>Recent decisions</h3>
      <div class="scroll" style="max-height: 40vh; border: none">
        <table>
          <thead>
            <tr><th>When</th><th>Event</th><th>Actor</th><th>Subject</th><th>Detail</th></tr>
          </thead>
          <tbody>
            <tr v-for="event in activity" :key="event.id">
              <td class="mono">{{ stamp(event.created_at) }}</td>
              <td>{{ event.event }}</td>
              <td class="muted">{{ event.actor }}</td>
              <td class="mono">{{ event.subject_id || '—' }}</td>
              <td class="mono">{{ JSON.stringify(event.detail).slice(0, 140) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
  <div v-else class="empty">Loading metrics…</div>
</template>
