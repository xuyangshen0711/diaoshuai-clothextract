<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ensureSession, logoutSite, type SiteSession } from './api'
import { activeJobCount, jobNotice, startJobPolling } from './composables/importJobs'
import { serviceSummary, startServiceStatusPolling } from './composables/serviceStatus'

const route = useRoute()
const router = useRouter()
const textSearch = computed(() => route.name === 'search' && route.query.mode !== 'image')
const imageSearch = computed(() => route.name === 'search' && route.query.mode === 'image')
const isLogin = computed(() => route.name === 'login')
const session = ref<SiteSession | null>(null)
const ready = ref(false)
const showShell = computed(() => ready.value && (!session.value?.required || session.value.authenticated))

let stopPolling: (() => void) | undefined
let stopServicePolling: (() => void) | undefined
async function syncPolling() {
  try {
    if (route.name === 'login') {
      stopPolling?.()
      stopPolling = undefined
      stopServicePolling?.()
      stopServicePolling = undefined
      return
    }
    session.value = await ensureSession()
    if (session.value.required && !session.value.authenticated) {
      stopPolling?.()
      stopPolling = undefined
      stopServicePolling?.()
      stopServicePolling = undefined
      return
    }
    if (!stopPolling) stopPolling = startJobPolling()
    if (!stopServicePolling) stopServicePolling = startServiceStatusPolling()
  } finally {
    ready.value = true
  }
}
watch(() => route.fullPath, () => { void syncPolling() }, { immediate: true })
onUnmounted(() => { stopPolling?.(); stopServicePolling?.() })

async function logout() {
  await logoutSite()
  session.value = { required: true, authenticated: false }
  stopPolling?.()
  stopPolling = undefined
  stopServicePolling?.()
  stopServicePolling = undefined
  await router.push({ name: 'login' })
}
</script>

<template>
  <div class="app-shell" :class="{ 'is-login': isLogin || !showShell }">
    <header v-if="showShell && !isLogin" class="topbar">
      <div class="brand-lockup">
        <RouterLink to="/" class="brand-name" exact-active-class="is-current" active-class="">
          <span class="brand-zh">貂帅服装</span>
          <span class="brand-en">DIAOSHUAI KNIT</span>
        </RouterLink>
        <span class="brand-rule" aria-hidden="true"></span>
        <p class="brand-sub">
          <span class="brand-sub-zh">女装针织 · 款式图库</span>
          <span class="brand-sub-en">Women's Knitwear Style Library</span>
        </p>
      </div>
      <nav class="util-links" aria-label="页面">
        <RouterLink to="/" exact-active-class="is-current" active-class="">图库</RouterLink>
        <RouterLink to="/search?mode=text" active-class="" exact-active-class="" :class="{ 'is-current': textSearch }">灵感找款</RouterLink>
        <RouterLink to="/search?mode=image" active-class="" exact-active-class="" :class="{ 'is-current': imageSearch }">以图搜</RouterLink>
        <RouterLink to="/import" exact-active-class="is-current" active-class="">导入<span v-if="activeJobCount" class="nav-count">{{ activeJobCount }}</span></RouterLink>
        <RouterLink to="/import#api-status" class="service-status-link" :class="serviceSummary.tone" active-class="" exact-active-class="" title="查看分类 API 配置与连接检测结果"><span aria-hidden="true"></span>{{ serviceSummary.label }}</RouterLink>
        <button v-if="session?.required" class="text-button nav-logout" type="button" @click="logout">退出</button>
      </nav>
    </header>

    <RouterView v-if="showShell || (ready && isLogin)" />
    <footer class="site-icp-footer" aria-label="网站备案信息">
      <a href="https://beian.miit.gov.cn/" target="_blank" rel="noopener noreferrer">浙ICP备2026103320号</a>
    </footer>
    <aside v-if="showShell && jobNotice && !isLogin" class="job-toast" role="status">
      <span>{{ jobNotice }}</span><RouterLink to="/import">查看任务</RouterLink>
      <button type="button" aria-label="关闭任务提示" @click="jobNotice = ''">×</button>
    </aside>
  </div>
</template>

<style scoped>
.util-links .service-status-link { display: inline-flex; align-items: center; gap: 7px; padding: 6px 10px; border: 1px solid var(--line); border-radius: 4px; color: var(--muted); font-size: 12px; letter-spacing: 0; line-height: 1.6; }
.service-status-link span { width: 6px; height: 6px; border-radius: 50%; background: currentColor; flex: 0 0 6px; }
.util-links .service-status-link.connected { color: #41624a; border-color: #c8d5c7; }
.util-links .service-status-link.pending { color: #80602f; border-color: #d9c8aa; }
.util-links .service-status-link.error { color: #9a403a; border-color: #dfbbb5; }
.util-links .service-status-link:focus-visible { outline: 2px solid var(--gold); outline-offset: 4px; }
</style>
