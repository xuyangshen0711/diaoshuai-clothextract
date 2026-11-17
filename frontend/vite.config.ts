import { fileURLToPath } from 'node:url'
import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, fileURLToPath(new URL('.', import.meta.url)), 'API_PROXY_')
  const apiTarget = process.env.API_PROXY_TARGET || env.API_PROXY_TARGET || 'http://127.0.0.1:8520'
  return {
    plugins: [vue()],
    build: { target: ['chrome79', 'edge79', 'firefox72', 'safari13'] },
    server: {
      host: '0.0.0.0',
      port: 8518,
      proxy: {
        // The analytics origin check uses the browser-facing Host.
        '/api/analytics': { target: apiTarget, changeOrigin: false },
        '/api': apiTarget,
      },
    },
  }
})
