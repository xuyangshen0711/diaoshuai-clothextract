<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { getCatalog, getFilters, type CatalogItem, type FilterMeta } from '../api'
import { formatTime, importTime, sourceType } from '../utils/source'

const route = useRoute()
const router = useRouter()
const items = ref<CatalogItem[]>([])
const count = ref(0)
const loading = ref(false)
const error = ref('')
const showSidebar = ref(true)
const meta = reactive<FilterMeta>({})

const filters = computed(() => ({
  category: typeof route.query.category === 'string' ? route.query.category : '全部',
  color: typeof route.query.color === 'string' ? route.query.color : '全部',
  style: typeof route.query.style === 'string' ? route.query.style : '全部',
}))
function setFilter(key: string, value: string) {
  router.replace({ query: { ...route.query, [key]: value === '全部' ? undefined : value } })
}

const group = computed(() => (typeof route.query.group === 'string' ? route.query.group : '全部'))
const q = computed(() => (typeof route.query.q === 'string' ? route.query.q : ''))
const reviewStatus = computed(() => ['pending', 'approved'].includes(String(route.query.review_status)) ? String(route.query.review_status) : '')
const statusOptions = [{ value: '', label: '全部款式' }, { value: 'pending', label: '未审核' }, { value: 'approved', label: '已审核' }]

const categoryOptions = computed(() => ['全部', ...(meta['品类'] || [])])
const colorOptions = computed(() => ['全部', ...(meta['颜色主色调'] || [])])
const styleOptions = computed(() => ['全部', ...(meta['风格'] || [])])

let latest = 0
async function load() {
  const sequence = ++latest
  loading.value = true
  error.value = ''
  try {
    const data = await getCatalog({
      group: group.value,
      q: q.value,
      category: filters.value.category,
      color: filters.value.color,
      style: filters.value.style,
      exclude_samples: true,
      review_status: reviewStatus.value,
    })
    if (sequence !== latest) return
    items.value = data.items
    count.value = data.count
  } catch (e) {
    if (sequence === latest) error.value = e instanceof Error ? e.message : String(e)
  } finally {
    if (sequence === latest) loading.value = false
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

watch([group, q, filters, reviewStatus], load, { deep: true })
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
          @click="setFilter('category', opt)"
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
          @click="setFilter('color', opt)"
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
          @click="setFilter('style', opt)"
        >
          {{ opt }}
        </button>
      </div>
    </aside>

    <div class="library-content">
      <h2 class="visually-hidden">款式列表</h2>
      <div class="library-toolbar">
        <div class="library-status-filter" role="group" aria-label="按审核状态筛选">
          <button v-for="option in statusOptions" :key="option.value" type="button" :aria-pressed="reviewStatus === option.value"
            @click="setFilter('review_status', option.value || '全部')">{{ option.label }}</button>
        </div>
        <p class="meta" role="status">{{ loading ? '加载中…' : `${count} 款` }}</p>
      </div>
      <p class="meta library-help">未审核款式按当前分类展示，可在人工审核中确认或修正。</p>
      <div v-if="error" class="error">{{ error }}</div>
      <div v-if="!items.length && !loading && !error" class="empty">暂无匹配款式。可调整筛选，或<RouterLink to="/import" class="review-text-link">导入新资料</RouterLink>。</div>
      <div v-else class="grid">
        <article v-for="item in items" :key="item.id" class="card">
          <RouterLink class="card-link" :to="{ name: 'record', params: { id: item.id }, query: { from: route.fullPath } }">
          <img
            :src="item.image_url"
            :alt="item.display_tags.一句话描述 || item.display_tags.品类"
            loading="lazy"
            width="180"
            height="240"
          />
          <div class="card-body">
            <div class="library-card-heading"><h3>{{ item.display_tags.品类 }}</h3><span class="library-review-badge" :class="item.review_status">{{ item.review_status === 'approved' ? '已审核' : '未审核' }}</span></div>
            <p class="sub">{{ item.display_tags.颜色主色调 }} · {{ item.display_tags.风格 }}</p>
            <div class="library-card-source">
              <p class="library-source-file" :title="item.source.source_file"><span>{{ sourceType(item.source) }}</span> {{ item.source.source_file }}</p>
              <p v-if="item.source.location" class="library-source-location">{{ item.source.location }}</p>
              <p>导入时间 <time :datetime="importTime(item.source)">{{ formatTime(importTime(item.source)) }}</time></p>
            </div>
          </div>
          </RouterLink>
        </article>
      </div>
    </div>
  </section>
</template>
