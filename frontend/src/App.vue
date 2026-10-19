<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

const route = useRoute()
const router = useRouter()

const groups = ['全部', '上衣', '下装', '外套']
const group = ref('全部')
const query = ref('')
const showFilters = ref(true)

const onLibrary = computed(() => route.name === 'library')

watch(group, (value) => {
  if (!onLibrary.value) {
    router.push({ name: 'library', query: { ...route.query, group: value } })
    return
  }
  router.replace({ query: { ...route.query, group: value === '全部' ? undefined : value } })
})

watch(
  () => route.query.group,
  (value) => {
    group.value = typeof value === 'string' && value ? value : '全部'
  },
  { immediate: true },
)

function submitSearch() {
  router.push({
    name: 'library',
    query: {
      ...route.query,
      q: query.value.trim() || undefined,
      group: group.value === '全部' ? undefined : group.value,
    },
  })
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
      </div>

      <nav class="group-nav">
        <button
          v-for="item in groups"
          :key="item"
          type="button"
          :class="{ active: group === item }"
          @click="group = item"
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
          <input v-model="query" placeholder="搜索品类或颜色..." />
        </form>
        <button class="icon-btn" type="button" @click="toggleFilters">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
            <path d="M4 7h16M7 12h10M10 17h4" />
          </svg>
          筛选
        </button>
        <div class="util-links">
          <RouterLink to="/search">以图搜</RouterLink>
          <RouterLink to="/import">导入</RouterLink>
          <RouterLink to="/edit">修正</RouterLink>
        </div>
      </div>
    </header>

    <RouterView />
  </div>
</template>
