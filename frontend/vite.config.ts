import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    host: '0.0.0.0',
    port: 8518,
    proxy: {
      '/api': process.env.API_PROXY_TARGET || 'http://127.0.0.1:8520',
    },
  },
})
