<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { getHealth } from './api'

const catalogCount = ref(0)

onMounted(async () => {
  try {
    const health = await getHealth()
    catalogCount.value = health.catalog_count
  } catch {
    /* backend may still be starting */
  }
})
</script>

<template>
  <div class="app-shell">
    <header class="topbar">
      <div class="brand">
        <h1>服装图片库</h1>
      </div>
      <nav class="nav">
        <RouterLink to="/">图库</RouterLink>
        <RouterLink to="/search">搜索</RouterLink>
        <RouterLink to="/import">导入</RouterLink>
        <RouterLink to="/edit">修正</RouterLink>
      </nav>
      <div class="top-meta">{{ catalogCount }} 款</div>
    </header>
    <RouterView />
  </div>
</template>
