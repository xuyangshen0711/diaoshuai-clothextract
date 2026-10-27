<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { assignSourceCategory, getCatalog, getFilters, getLibraryMeta, type CatalogItem, type FilterMeta, type LibraryMeta } from '../api'
import { formatTime, importTime, sourceType } from '../utils/source'

const route = useRoute()
const router = useRouter()
const items = ref<CatalogItem[]>([])
const count = ref(0)
const loading = ref(false)
const error = ref('')
const showSidebar = ref(true)
const meta = reactive<FilterMeta>({})
const libraryMeta = ref<LibraryMeta>({ source_categories: ['本厂产品', '网图趋势', '品牌资料包', '市调拍照'], unclassified: '未分类', factory: {} })
const bulkCategory = ref('本厂产品')
const bulkMessage = ref('')

const queryText = (key: string) => typeof route.query[key] === 'string' ? route.query[key] as string : '全部'
const filters = computed(() => ({
  category: queryText('category'),
  color: queryText('color'),
  style: queryText('style'),
  archive_season: queryText('archive_season'),
  color_family: queryText('color_family'),
  size: queryText('size'),
  series: queryText('series'),
  needle: queryText('needle'),
  customer: queryText('customer'),
}))
function setFilter(key: string, value: string) {
  router.replace({ query: { ...route.query, [key]: value === '全部' ? undefined : value } })
}
const group = computed(() => (typeof route.query.group === 'string' ? route.query.group : '全部'))
const q = computed(() => (typeof route.query.q === 'string' ? route.query.q : ''))
const reviewStatus = computed(() => ['pending', 'approved'].includes(String(route.query.review_status)) ? String(route.query.review_status) : '')
const sourceCategory = computed(() => typeof route.query.source_category === 'string' ? route.query.source_category : '全部')
const sort = computed(() => route.query.sort === 'sales' ? 'sales' : '全部')
const ownFactory = computed(() => sourceCategory.value === '本厂产品')
const showArchiveFilters = computed(() => sourceCategory.value === '全部' || ownFactory.value)
const statusOptions = [{ value: '', label: '全部款式' }, { value: 'pending', label: '未审核' }, { value: 'approved', label: '已审核' }]
const sourceTabs = computed(() => ['全部', ...libraryMeta.value.source_categories, libraryMeta.value.unclassified])
const categoryOptions = computed(() => {
  const archive = libraryMeta.value.factory['品类'] || []
  const ai = meta['品类'] || []
  if (ownFactory.value) return ['全部', ...archive]
  if (sourceCategory.value !== '全部') return ['全部', ...ai]
  return ['全部', ...ai, ...archive.filter(item => !ai.includes(item))]
})
const colorOptions = computed(() => ['全部', ...(meta['颜色主色调'] || [])])
const styleOptions = computed(() => ['全部', ...(meta['风格'] || [])])
const archiveFilters = computed(() => [
  ['archive_season', '季节', libraryMeta.value.factory['季节'] || []],
  ['color_family', '色系', libraryMeta.value.factory['色系'] || []],
  ['size', '尺码', libraryMeta.value.factory['尺码'] || []],
  ['series', '系列', libraryMeta.value.factory['系列'] || []],
  ['needle', '针型', libraryMeta.value.factory['针型'] || []],
  ['customer', '客户', libraryMeta.value.factory['客户'] || []],
] as const)

function setSource(value: string) {
  const next: Record<string, string | string[] | undefined> = { ...route.query, category: undefined, color: undefined, style: undefined,
    archive_season: undefined, color_family: undefined, size: undefined, series: undefined, needle: undefined, customer: undefined }
  next.source_category = value === '全部' ? undefined : value
  router.replace({ query: next })
}

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
      color: ownFactory.value ? '' : filters.value.color,
      style: ownFactory.value ? '' : filters.value.style,
      archive_season: showArchiveFilters.value ? filters.value.archive_season : '',
      color_family: showArchiveFilters.value ? filters.value.color_family : '',
      size: showArchiveFilters.value ? filters.value.size : '',
      series: showArchiveFilters.value ? filters.value.series : '',
      needle: showArchiveFilters.value ? filters.value.needle : '',
      customer: showArchiveFilters.value ? filters.value.customer : '',
      source_category: sourceCategory.value,
      sort: sort.value,
      exclude_samples: true,
      review_status: reviewStatus.value,
    })
    if (sequence !== latest) return
    items.value = data.items
    count.value = data.count
  } catch (reason) {
    if (sequence === latest) error.value = reason instanceof Error ? reason.message : String(reason)
  } finally {
    if (sequence === latest) loading.value = false
  }
}

async function bulkAssign() {
  bulkMessage.value = ''
  if (!items.value.length) return
  if (!window.confirm(`将当前 ${items.value.length} 条结果的资料大类改为「${bulkCategory.value}」？`)) return
  try {
    await assignSourceCategory(items.value.map(item => item.id), bulkCategory.value)
    bulkMessage.value = `已将 ${items.value.length} 条结果改为${bulkCategory.value}`
    await load()
  } catch (reason) { error.value = reason instanceof Error ? reason.message : String(reason) }
}

function onToggle(event: Event) {
  showSidebar.value = Boolean((event as CustomEvent).detail)
}

onMounted(async () => {
  try {
    Object.assign(meta, await getFilters())
    libraryMeta.value = await getLibraryMeta()
  } catch (reason) { error.value = reason instanceof Error ? reason.message : String(reason) }
  window.addEventListener('toggle-library-filters', onToggle)
  await load()
})
onUnmounted(() => window.removeEventListener('toggle-library-filters', onToggle))
watch(() => route.query, load, { deep: true })
</script>

<template>
  <section class="library-layout" :class="{ 'sidebar-collapsed': !showSidebar }">
    <aside v-if="showSidebar" class="sidebar">
      <div class="sidebar-head">
        <span>筛选</span>
        <button type="button" aria-label="关闭筛选" @click="showSidebar = false">×</button>
      </div>
      <div class="filter-block">
        <h4>{{ ownFactory ? '一级品类' : '品类' }}</h4>
        <button v-for="opt in categoryOptions" :key="`c-${opt}`" type="button" :class="{ active: filters.category === opt }" @click="setFilter('category', opt)">{{ opt }}</button>
      </div>
      <template v-if="showArchiveFilters">
        <div v-for="[key, label, options] in archiveFilters" :key="key" class="filter-block">
          <h4>{{ label }}</h4>
          <button type="button" :class="{ active: filters[key] === '全部' }" @click="setFilter(key, '全部')">全部</button>
          <button v-for="opt in options" :key="`${key}-${opt}`" type="button" :class="{ active: filters[key] === opt }" @click="setFilter(key, opt)">{{ opt }}</button>
        </div>
      </template>
      <template v-if="!ownFactory">
        <div class="filter-block">
          <h4>颜色</h4>
          <button v-for="opt in colorOptions" :key="`color-${opt}`" type="button" :class="{ active: filters.color === opt }" @click="setFilter('color', opt)">{{ opt }}</button>
        </div>
        <div class="filter-block">
          <h4>风格</h4>
          <button v-for="opt in styleOptions" :key="`s-${opt}`" type="button" :class="{ active: filters.style === opt }" @click="setFilter('style', opt)">{{ opt }}</button>
        </div>
      </template>
    </aside>

    <div class="library-content">
      <h2 class="visually-hidden">款式列表</h2>
      <div class="source-tabs" role="group" aria-label="资料大类">
        <button v-for="item in sourceTabs" :key="item" type="button" :aria-pressed="sourceCategory === item" @click="setSource(item)">{{ item }}</button>
      </div>
      <div class="library-toolbar">
        <div class="library-status-filter" role="group" aria-label="按审核状态筛选">
          <button v-for="option in statusOptions" :key="option.value" type="button" :aria-pressed="reviewStatus === option.value" @click="setFilter('review_status', option.value || '全部')">{{ option.label }}</button>
        </div>
        <label class="sort-field">排序
          <select :value="sort" aria-label="排序" @change="setFilter('sort', ($event.target as HTMLSelectElement).value)">
            <option value="全部">默认</option>
            <option value="sales">按销量</option>
          </select>
        </label>
        <p class="meta" role="status">{{ loading ? '加载中…' : `${count} 款` }}</p>
      </div>
      <p class="meta library-help">{{ ownFactory ? '本厂产品有款式档案时，品类按一级品类筛选；AI 标签只作参考。' : '未审核款式按当前分类展示，可在人工审核中确认或修正。' }}</p>
      <div class="bulk-category">
        <label for="bulk-category">将当前结果的资料大类改为</label>
        <select id="bulk-category" v-model="bulkCategory">
          <option v-for="item in sourceTabs.filter(item => item !== '全部')" :key="item" :value="item">{{ item }}</option>
        </select>
        <button class="btn" type="button" :disabled="!items.length || loading" @click="bulkAssign">应用到当前结果</button>
        <span v-if="bulkMessage" class="meta">{{ bulkMessage }}</span>
      </div>
      <div v-if="error" class="error">{{ error }}</div>
      <div v-if="!items.length && !loading && !error" class="empty">暂无匹配款式。可调整筛选，或<RouterLink to="/import" class="review-text-link">导入新资料</RouterLink>。</div>
      <div v-else class="grid">
        <article v-for="item in items" :key="item.id" class="card">
          <RouterLink class="card-link" :to="{ name: 'record', params: { id: item.id }, query: { from: route.fullPath } }">
          <img :src="item.thumbnail_url || item.image_url" :alt="item.display_tags.一句话描述 || item.library_category || item.display_tags.品类" loading="lazy" width="180" height="240" />
          <div class="card-body">
            <div class="library-card-heading"><h3>{{ item.library_category || item.display_tags.品类 }}</h3><span class="library-review-badge" :class="item.review_status">{{ item.review_status === 'approved' ? '已审核' : '未审核' }}</span></div>
            <p v-if="item.style_code" class="sub">款号 {{ item.style_code }}</p>
            <p v-if="item.source_category === '本厂产品'" class="sub">已售 {{ item.sales_quantity.toLocaleString('zh-CN') }} 件</p>
            <p v-else class="sub">{{ item.display_tags.颜色主色调 }} · {{ item.display_tags.风格 }}</p>
            <p v-if="item.source_category === '本厂产品' && item.display_tags.品类 !== item.library_category" class="sub">AI 参考：{{ item.display_tags.品类 }}</p>
            <div class="library-card-source">
              <p class="library-source-file" :title="item.source.source_file"><span>{{ item.source_category || '未分类' }}</span> {{ sourceType(item.source) }} {{ item.source.source_file }}</p>
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
