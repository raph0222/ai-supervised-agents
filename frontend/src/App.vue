<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from './api'
import { navigate, path } from './router'
import ChatView from './components/ChatView.vue'
import AdminView from './components/AdminView.vue'

const health = ref(null)
const isAdmin = computed(() => path.value.startsWith('/admin'))

onMounted(async () => {
  try {
    health.value = await api.health()
  } catch {
    health.value = { status: 'unreachable' }
  }
})

function go(event, to) {
  event.preventDefault()
  navigate(to)
}
</script>

<template>
  <div class="app" :class="{ 'fixed-height': !isAdmin }">
    <header class="topbar">
      <div class="brand">Northbridge Components <small>support</small></div>
      <nav class="nav">
        <a href="/" :class="{ active: !isAdmin }" @click="go($event, '/')">Chat</a>
        <a href="/admin" :class="{ active: isAdmin }" @click="go($event, '/admin')">Admin</a>
        <span v-if="health" class="pill" :class="health.vertex_configured ? 'good' : 'warn'">
          {{ health.vertex_configured ? 'Vertex live' : 'Vertex not configured' }}
        </span>
      </nav>
    </header>

    <!-- An unconfigured deployment says so plainly instead of failing
         somewhere deep in a stack trace. -->
    <div v-if="health && !health.vertex_configured" class="banner">
      Chat needs Vertex AI. Set <code>{{ (health.missing_vertex_vars || []).join(', ') }}</code>
      in <code>.env</code> and restart. Everything else — orders, policies, logs,
      approvals — works without it.
    </div>

    <AdminView v-if="isAdmin" />
    <ChatView v-else />
  </div>
</template>
