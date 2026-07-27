<script setup>
// What the agent actually did, folded away by default. The customer does not
// need it; anyone evaluating whether the system is trustworthy needs exactly
// this — the plan, the tool calls, the policy verdicts, the retrieval.
const props = defineProps({ meta: { type: Object, default: () => ({}) } })

const hasTrace = () =>
  (props.meta.tool_results || []).length ||
  (props.meta.verdicts || []).length ||
  (props.meta.plan?.steps || []).length ||
  (props.meta.retrieved || []).length

function statusClass(status) {
  if (status === 'EXECUTED') return 'ok'
  if (status === 'BLOCKED') return 'blocked'
  return 'failed'
}
</script>

<template>
  <details v-if="hasTrace()" class="details">
    <summary>trace — plan, tools, policy</summary>
    <div class="body">
      <template v-if="meta.plan?.steps?.length">
        <div class="muted">plan</div>
        <div v-for="(step, i) in meta.plan.steps" :key="i" class="trace">
          {{ step.n || i + 1 }}. {{ step.action }}
          <span v-if="step.tool" class="muted">[{{ step.tool }}]</span>
        </div>
      </template>

      <template v-if="meta.tool_results?.length">
        <div class="muted">tool calls</div>
        <div v-for="(result, i) in meta.tool_results" :key="i" class="trace" :class="statusClass(result.status)">
          {{ result.status }} · {{ result.tool }}
          <span class="muted">{{ JSON.stringify(result.args) }}</span>
          <span v-if="result.result?.message"> — {{ result.result.message }}</span>
        </div>
      </template>

      <template v-if="meta.verdicts?.length">
        <div class="muted">policy verdicts</div>
        <div v-for="(verdict, i) in meta.verdicts" :key="i" class="trace">
          {{ verdict.decision }}
          <span v-for="(reason, j) in verdict.reasons || []" :key="j">
            <template v-if="reason.decision !== 'ALLOW'">
              · {{ reason.rule }} ({{ reason.policy_id }})
            </template>
          </span>
        </div>
      </template>

      <template v-if="meta.retrieved?.length">
        <div class="muted">retrieved</div>
        <div class="trace">
          <span v-for="(hit, i) in meta.retrieved" :key="i">
            {{ hit.policy_id }}<span class="muted">/{{ hit.heading }} ({{ hit.score }})</span>
            <span v-if="i < meta.retrieved.length - 1"> · </span>
          </span>
        </div>
        <div class="muted" style="font-size: 11px">
          retrieval mode: {{ meta.retrieved[0]?.mode }}
        </div>
      </template>
    </div>
  </details>
</template>
