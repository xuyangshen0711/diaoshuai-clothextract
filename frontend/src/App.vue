<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { getHealth } from './api'

const openaiReady = ref(false)
const catalogCount = ref(0)

onMounted(async () => {
  try {
    const health = await getHealth()
    openaiReady.value = health.openai_ready
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
        <h1>服装内部图片库</h1>
        <p>
          Vue 3 前端 · FastAPI · GPT-4o-mini 打标
          · {{ catalogCount }} 条
          · {{ openaiReady ? 'API 已就绪' : 'API 未配置' }}
        </p>
      </div>
      <nav class="nav">
        <RouterLink to="/">图片库</RouterLink>
        <RouterLink to="/search">检索</RouterLink>
        <RouterLink to="/import">导入</RouterLink>
        <RouterLink to="/edit">人工修正</RouterLink>
      </nav>
    </header>
    <RouterView />
  </div>
</template>
