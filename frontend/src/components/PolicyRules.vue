<script setup>
import { onMounted, ref } from 'vue'
import { api, stamp } from '../api'

// Rule *parameters* live in the database and are editable here. The
// rules themselves are code and are not editable from anywhere — changing a
// threshold is a config change, changing a gate is a deploy.
const rules = ref([])
const drafts = ref({})
const message = ref('')
const error = ref('')

async function load() {
  const data = await api.policyRules()
  rules.value = data.rules
  drafts.value = Object.fromEntries(data.rules.map((r) => [r.key, r.value_int]))
}

async function save(key) {
  error.value = ''
  message.value = ''
  try {
    const result = await api.updatePolicyRule(key, Number(drafts.value[key]))
    message.value = `${key}: ${result.previous} → ${result.value_int}. The engine reads this on its next evaluation.`
    await load()
  } catch (err) {
    error.value = err.message
  }
}

onMounted(load)
</script>

<template>
  <div class="section">
    <div class="row">
      <span v-if="message" class="pill good">{{ message }}</span>
      <span v-if="error" class="pill bad">{{ error }}</span>
    </div>

    <div class="scroll">
      <table>
        <thead>
          <tr><th>Key</th><th>Policy</th><th>Value</th><th>Description</th><th>Updated</th><th></th></tr>
        </thead>
        <tbody>
          <tr v-for="rule in rules" :key="rule.key">
            <td class="mono">{{ rule.key }}</td>
            <td><span class="pill">{{ rule.policy_id }}</span></td>
            <td style="width: 130px">
              <input v-model="drafts[rule.key]" type="number" />
            </td>
            <td class="muted">{{ rule.description }}</td>
            <td class="mono muted">{{ stamp(rule.updated_at) }}</td>
            <td>
              <button :disabled="Number(drafts[rule.key]) === rule.value_int"
                      @click="save(rule.key)">Save</button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <p class="muted" style="font-size: 12.5px">
      Amounts are in minor units — 5000 is $50.00. Lowering
      <code>refund_auto_approve_under_cents</code> sends more refunds to this
      queue; the gate itself cannot be turned off from here.
    </p>
  </div>
</template>
