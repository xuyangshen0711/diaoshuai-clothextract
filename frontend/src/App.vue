<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ensureSession, logoutSite, type SiteSession } from './api'
import { activeJobCount, jobNotice, startJobPolling } from './composables/importJobs'

const route = useRoute()
const router = useRouter()
const textSearch = computed(() => route.name === 'search' && route.query.mode !== 'image')
const imageSearch = computed(() => route.name === 'search' && route.query.mode === 'image')
const isLogin = computed(() => route.name === 'login')
const session = ref<SiteSession | null>(null)
const ready = ref(false)
const showShell = computed(() => ready.value && (!session.value?.required || session.value.authenticated))

let stopPolling: (() => void) | undefined
async function syncPolling() {
  try {
    if (route.name === 'login') {
      stopPolling?.()
      stopPolling = undefined
      return
    }
    session.value = await ensureSession()
    if (session.value.required && !session.value.authenticated) {
      stopPolling?.()
      stopPolling = undefined
      return
    }
    if (!stopPolling) stopPolling = startJobPolling()
  } finally {
    ready.value = true
  }
}
watch(() => route.fullPath, () => { void syncPolling() }, { immediate: true })
onUnmounted(() => stopPolling?.())

async function logout() {
  await logoutSite()
  session.value = { required: true, authenticated: false }
  stopPolling?.()
  stopPolling = undefined
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
        <RouterLink to="/review" exact-active-class="is-current" active-class="">审核</RouterLink>
        <button v-if="session?.required" class="text-button nav-logout" type="button" @click="logout">退出</button>
      </nav>
    </header>

    <RouterView v-if="showShell || (ready && isLogin)" />
    <aside v-if="showShell && jobNotice && !isLogin" class="job-toast" role="status">
      <span>{{ jobNotice }}</span><RouterLink to="/import">查看任务</RouterLink>
      <button type="button" aria-label="关闭任务提示" @click="jobNotice = ''">×</button>
    </aside>
  </div>
</template>
