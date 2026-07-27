<script setup>
import { onMounted, ref, watch } from 'vue'
import { money, stamp } from '../api'

// One table for every read-only inspector. The tabs differ in columns, not in
// behaviour, and six near-identical components would have to be kept in sync.
const props = defineProps({
  loader: { type: Function, required: true },
  collection: { type: String, required: true },
  columns: { type: Array, required: true }
})

const rows = ref([])
const error = ref('')
const query = ref('')

async function load() {
  try {
    const data = await props.loader()
    rows.value = data[props.collection] || []
    error.value = ''
  } catch (err) {
    error.value = err.message
  }
}

function display(row, column) {
  const value = row[column.key]
  if (value === null || value === undefined || value === '') return '—'
  if (column.type === 'time') return stamp(value)
  if (column.type === 'money') return money(value)
  if (column.type === 'bool') return value ? 'yes' : 'no'
  if (column.type === 'json') return JSON.stringify(value)
  return String(value)
}

function matches(row) {
  if (!query.value) return true
  return JSON.stringify(row).toLowerCase().includes(query.value.toLowerCase())
}

onMounted(load)
watch(() => props.loader, load)
</script>

<template>
  <div class="section">
    <div class="row">
      <input v-model="query" placeholder="Filter…" style="max-width: 320px" />
      <button class="ghost" @click="load">Refresh</button>
      <span class="muted">{{ rows.filter(matches).length }} rows</span>
      <span v-if="error" class="pill bad">{{ error }}</span>
    </div>

    <div class="scroll wrap">
      <table>
        <thead>
          <tr><th v-for="column in columns" :key="column.key">{{ column.label }}</th></tr>
        </thead>
        <tbody>
          <tr v-for="(row, i) in rows.filter(matches)" :key="row.id ?? i">
            <td v-for="column in columns" :key="column.key"
                :class="{ mono: column.type === 'json' || column.type === 'time' }">
              <span :class="column.key === 'ok' ? (row.ok ? 'trace ok' : 'trace failed') : ''">
                {{ display(row, column) }}
              </span>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-if="!rows.filter(matches).length" class="empty">Nothing recorded yet.</div>
    </div>
  </div>
</template>
