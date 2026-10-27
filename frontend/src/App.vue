<script setup lang="ts">
import { computed, onMounted, onUnmounted } from 'vue'
import { activeJobCount, jobNotice, startJobPolling } from './composables/importJobs'
import { useRoute, useRouter } from 'vue-router'

const route = useRoute()
const router = useRouter()
const onLibrary = computed(() => route.name === 'library')

let stopPolling: (() => void) | undefined
onMounted(() => { stopPolling = startJobPolling() })
onUnmounted(() => stopPolling?.())

function goHome() {
  router.push({ name: 'library' })
}
</script>

<template>
  <div class="app-shell">
    <header class="topbar">
      <div class="brand">
        <h1>服装图片库</h1>
        <button v-if="!onLibrary" class="icon-btn home-button" type="button" @click="goHome">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true">
            <path d="M3 10l9-7 9 7M5 9v12h5v-7h4v7h5V9" />
          </svg>
          返回主页
        </button>
      </div>

      <div class="header-actions">
        <div class="util-links">
          <RouterLink to="/search?mode=text">灵感找款</RouterLink>
          <RouterLink to="/search?mode=image">以图搜</RouterLink>
          <RouterLink to="/import">导入任务<span v-if="activeJobCount" class="nav-count">{{ activeJobCount }}</span></RouterLink>
          <RouterLink to="/review">人工审核</RouterLink>
          <RouterLink to="/categories">品类设置</RouterLink>
        </div>
      </div>
    </header>

    <RouterView />
    <aside v-if="jobNotice" class="job-toast" role="status">
      <span>{{ jobNotice }}</span><RouterLink to="/import">查看任务</RouterLink>
      <button type="button" aria-label="关闭任务提示" @click="jobNotice = ''">×</button>
    </aside>
  </div>
</template>
