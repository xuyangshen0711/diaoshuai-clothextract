import './compat'
import { createApp } from 'vue'
import App from './App.vue'
import { router } from './router'
import './theme.css'
import './style.css'

declare global {
  interface Window {
    pageLoadFailed?: (code: string) => void
    pageLoadReady?: () => void
  }
}
const app = createApp(App)
let startupFailed = false
app.config.errorHandler = (error) => {
  startupFailed = true
  console.error(error)
  window.pageLoadFailed?.('PAGE_RUNTIME')
}
router.onError(error => {
  console.error(error)
  window.pageLoadFailed?.('PAGE_ROUTE')
})
app.use(router)
router.isReady().then(() => {
  app.mount('#app')
  if (!startupFailed) window.pageLoadReady?.()
}).catch(error => {
  console.error(error)
  window.pageLoadFailed?.('PAGE_START')
})
