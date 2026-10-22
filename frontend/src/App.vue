<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { activeJobCount, jobNotice, startJobPolling } from './composables/importJobs'
import { useRoute, useRouter } from 'vue-router'

const route = useRoute()
const router = useRouter()

const groups = ['全部', '上衣', '下装', '外套']
const group = computed(() => typeof route.query.group === 'string' ? route.query.group : '全部')
const query = ref('')
const showFilters = ref(true)

let stopPolling: (() => void) | undefined
onMounted(() => { stopPolling = startJobPolling() })
onUnmounted(() => stopPolling?.())

const onLibrary = computed(() => route.name === 'library')

function selectGroup(value: string) {
  const target = { name: 'library', query: { ...(onLibrary.value ? route.query : {}), group: value === '全部' ? undefined : value } }
  return onLibrary.value ? router.replace(target) : router.push(target)
}
watch(() => route.query.q, value => { query.value = typeof value === 'string' ? value : '' }, { immediate: true })

function submitSearch() {
  if (query.value.trim()) router.push({ name: 'search', query: { mode: 'text', q: query.value.trim() } })
}

function goHome() {
  query.value = ''
  router.push({ name: 'library' })
}

function toggleFilters() {
  showFilters.value = !showFilters.value
  window.dispatchEvent(new CustomEvent('toggle-library-filters', { detail: showFilters.value }))
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

      <nav class="group-nav">
        <button
          v-for="item in groups"
          :key="item"
          type="button"
          :class="{ active: group === item }"
          @click="selectGroup(item)"
        >
          {{ item }}
        </button>
      </nav>

      <div class="header-actions">
        <form class="search-pill" @submit.prevent="submitSearch">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
            <circle cx="11" cy="11" r="7" />
            <path d="M20 20l-3.5-3.5" />
          </svg>
          <input v-model="query" aria-label="搜索款式或灵感" placeholder="搜索款式或灵感…" />
        </form>
        <button class="icon-btn" type="button" @click="toggleFilters">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
            <path d="M4 7h16M7 12h10M10 17h4" />
          </svg>
          筛选
        </button>
        <div class="util-links">
          <RouterLink to="/search?mode=text">灵感找款</RouterLink>
          <RouterLink to="/search?mode=image">以图搜</RouterLink>
          <RouterLink to="/import">导入任务<span v-if="activeJobCount" class="nav-count">{{ activeJobCount }}</span></RouterLink>
          <RouterLink to="/review">人工审核</RouterLink>
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
