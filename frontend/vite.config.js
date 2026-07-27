import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// This server is the app's entrypoint; the backend serves the API only.

// On the host the backend is on localhost:8000; inside compose it is the `api`
// service, so the target is an env var rather than a constant.
const apiTarget = process.env.VITE_API_PROXY_TARGET || 'http://localhost:8000'

const usePolling = process.env.VITE_USE_POLLING === 'true'

export default defineConfig({
  plugins: [vue()],
  server: {
    host: true,
    port: 5173,
    watch: usePolling ? { usePolling: true, interval: 300 } : undefined,
    proxy: {
      '/api': {
        target: apiTarget,
        changeOrigin: true
      },
      '/health': apiTarget
    }
  }
})
