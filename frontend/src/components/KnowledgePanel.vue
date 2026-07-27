<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api'

const stats = ref(null)
const query = ref('How long do I have to return a graphics card?')
const hits = ref([])
const busy = ref(false)

async function load() {
  stats.value = await api.knowledge()
}

async function search() {
  busy.value = true
  try {
    hits.value = (await api.knowledgeSearch(query.value)).hits
  } finally {
    busy.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="section">
    <div v-if="stats" class="grid">
      <div class="card stat">
        <div class="value">{{ stats.chunks }}</div>
        <div class="label">chunks</div>
      </div>
      <div class="card stat">
        <div class="value">{{ stats.embedded }}</div>
        <div class="label">embedded</div>
      </div>
      <div class="card stat">
        <div class="value">{{ stats.missing_embeddings }}</div>
        <div class="label">without vectors</div>
        <div v-if="stats.missing_embeddings" class="target">
          <span class="pill warn">keyword fallback in use</span>
        </div>
      </div>
      <div class="card">
        <h3>By policy</h3>
        <div class="mono">
          <div v-for="(count, policy) in stats.by_policy" :key="policy">
            {{ policy }} — {{ count }}
          </div>
        </div>
      </div>
    </div>

    <div class="card">
      <h3>Try a retrieval</h3>
      <div class="row">
        <input v-model="query" style="max-width: 520px" @keydown.enter="search" />
        <button class="primary" :disabled="busy" @click="search">Search</button>
      </div>
      <div v-for="hit in hits" :key="hit.chunk_id" style="margin-top: 12px">
        <div class="row">
          <span class="pill">{{ hit.policy_id }}</span>
          <span class="pill" :class="hit.authority === 'binding' ? 'good' : ''">{{ hit.authority }}</span>
          <span class="pill">{{ hit.mode }}</span>
          <span class="muted">score {{ hit.score }}</span>
          <strong>{{ hit.heading || hit.title }}</strong>
        </div>
        <pre class="json" style="margin-top: 6px">{{ hit.content }}</pre>
      </div>
    </div>
  </div>
</template>
