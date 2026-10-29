<script setup lang="ts">
import { computed, onMounted, onUnmounted } from 'vue'
import { activeJobCount, jobNotice, startJobPolling } from './composables/importJobs'
import { useRoute } from 'vue-router'

const route = useRoute()
const textSearch = computed(() => route.name === 'search' && route.query.mode !== 'image')
const imageSearch = computed(() => route.name === 'search' && route.query.mode === 'image')

let stopPolling: (() => void) | undefined
onMounted(() => { stopPolling = startJobPolling() })
onUnmounted(() => stopPolling?.())
</script>

<template>
  <div class="app-shell">
    <header class="topbar">
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
      </nav>
    </header>

    <RouterView />
    <aside v-if="jobNotice" class="job-toast" role="status">
      <span>{{ jobNotice }}</span><RouterLink to="/import">查看任务</RouterLink>
      <button type="button" aria-label="关闭任务提示" @click="jobNotice = ''">×</button>
    </aside>
  </div>
</template>
