<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { getCatalog, getFilters, type CatalogItem, type FilterMeta } from '../api'

const route = useRoute()
const items = ref<CatalogItem[]>([])
const count = ref(0)
const loading = ref(false)
const error = ref('')
const showSidebar = ref(true)
const meta = reactive<FilterMeta>({})

const filters = reactive({
  category: '全部',
  color: '全部',
  style: '全部',
})

const group = computed(() => (typeof route.query.group === 'string' ? route.query.group : '全部'))
const q = computed(() => (typeof route.query.q === 'string' ? route.query.q : ''))

const categoryOptions = computed(() => ['全部', ...(meta['品类'] || [])])
const colorOptions = computed(() => ['全部', ...(meta['颜色主色调'] || [])])
const styleOptions = computed(() => ['全部', ...(meta['风格'] || [])])

async function load() {
  loading.value = true
  error.value = ''
  try {
    const data = await getCatalog({
      group: group.value,
      q: q.value,
      category: filters.category,
      color: filters.color,
      style: filters.style,
      exclude_samples: true,
    })
    items.value = data.items
    count.value = data.count
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

function onToggle(e: Event) {
  const detail = (e as CustomEvent).detail
  showSidebar.value = Boolean(detail)
}

onMounted(async () => {
  Object.assign(meta, await getFilters())
  window.addEventListener('toggle-library-filters', onToggle)
  await load()
})

onUnmounted(() => {
  window.removeEventListener('toggle-library-filters', onToggle)
})

watch([group, q, filters], load, { deep: true })
</script>

<template>
  <section class="library-layout" :class="{ 'sidebar-collapsed': !showSidebar }">
    <aside v-if="showSidebar" class="sidebar">
      <div class="sidebar-head">
        <span>筛选</span>
        <button type="button" aria-label="关闭筛选" @click="showSidebar = false">×</button>
      </div>

      <div class="filter-block">
        <h4>品类</h4>
        <button
          v-for="opt in categoryOptions"
          :key="`c-${opt}`"
          type="button"
          :class="{ active: filters.category === opt }"
          @click="filters.category = opt"
        >
          {{ opt }}
        </button>
      </div>

      <div class="filter-block">
        <h4>颜色</h4>
        <button
          v-for="opt in colorOptions"
          :key="`color-${opt}`"
          type="button"
          :class="{ active: filters.color === opt }"
          @click="filters.color = opt"
        >
          {{ opt }}
        </button>
      </div>

      <div class="filter-block">
        <h4>风格</h4>
        <button
          v-for="opt in styleOptions"
          :key="`s-${opt}`"
          type="button"
          :class="{ active: filters.style === opt }"
          @click="filters.style = opt"
        >
          {{ opt }}
        </button>
      </div>
    </aside>

    <div>
      <p class="meta" style="margin: 0 0 14px">
        {{ loading ? '加载中…' : `${count} 款` }}
      </p>
      <div v-if="error" class="error">{{ error }}</div>
      <div v-if="!items.length && !loading" class="empty">没有匹配的款式</div>
      <div v-else class="grid">
        <article v-for="item in items" :key="item.id" class="card">
          <img :src="item.image_url" :alt="item.tags.一句话描述 || item.tags.品类" loading="lazy" />
          <div class="card-body">
            <h3>{{ item.tags.品类 }}</h3>
            <p class="sub">{{ item.tags.颜色主色调 }} · {{ item.tags.风格 }}</p>
          </div>
        </article>
      </div>
    </div>
  </section>
</template>
