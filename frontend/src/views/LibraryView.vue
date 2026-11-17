<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  assignSourceCategory,
  deleteGalleryImages,
  getCatalog,
  getFilters,
  getLibraryMeta,
  getFactoryWorkspace,
  getTaxonomy,
  type CatalogItem,
  type FilterMeta,
  type LibraryMeta,
  type Taxonomy,
} from '../api'
import TagEditor from '../components/TagEditor.vue'
import { tagStatus } from '../utils/recordPresentation'
import { swatchColor, swatchLight } from '../utils/source'

const editing = ref<CatalogItem | null>(null)
const editMessage = ref('')
async function tagsSaved() { editMessage.value = '标签已保存，筛选与搜索已更新。'; await load() }
const route = useRoute()
const router = useRouter()
const items = ref<CatalogItem[]>([])
const count = ref(0)
const colorCounts = ref<Record<string, number>>({})
const loading = ref(false)
const error = ref('')
const metadataError = ref('')
const meta = ref<FilterMeta>({ fields: {}, detail_groups: {} })
const libraryMeta = ref<LibraryMeta>({
  source_categories: ['本厂产品', '网图趋势', '品牌资料包', '市调拍照'],
  unclassified: '未分类',
  factory: {},
})
const taxonomy = ref<Taxonomy>({
  unsorted: '待归类',
  sleeves: [],
  fabrics: [],
  tree: [],
  labels: [],
})
const bulkCategory = ref('本厂产品')
const bulkMessage = ref('')
const selectionMode = ref(false)
const selected = ref(new Set<string>())
const deleting = ref(false)
const deletionMessage = ref('')
const cleanupPending = ref(0)
const queryDraft = ref('')
let searchTimer = 0

const FACTORY_COLORS = [
  '红',
  '橙',
  '黄',
  '绿',
  '蓝',
  '紫',
  '粉',
  '棕咖',
  '米白',
  '灰',
  '黑',
  '白',
  '其他',
]
const queryText = (key: string) =>
  typeof route.query[key] === 'string' ? (route.query[key] as string) : '全部'
const orderSeasons = computed(() => {
  const value = route.query.order_season ?? route.query.archive_season
  const values = Array.isArray(value) ? value : [value]
  return [...new Set(values.filter((item): item is string => typeof item === 'string')
    .map(item => item.trim()).filter(item => item && item !== '全部'))]
})
const filters = computed(() => ({
  category: queryText('category'),
  subcategory: queryText('subcategory'),
  sleeve_length: queryText('sleeve_length'),
  fabric: queryText('fabric'),
  color: queryText('color'),
  style: queryText('style'),
  season: queryText('season'),
  fit: queryText('fit'),
  neckline: queryText('neckline'),
  sleeve: queryText('sleeve'),
  pattern: queryText('pattern'),
  color_family: queryText('color_family'),
  ordered_from: queryText('ordered_from'),
  ordered_to: queryText('ordered_to'),
}))
function setFilter(key: string, value: string) {
  const next = { ...route.query, [key]: value === '全部' ? undefined : value }
  if (key === 'category') delete next.subcategory
  if (key === 'order_season') delete next.archive_season
  router.replace({ query: next })
}
const q = computed(() => (typeof route.query.q === 'string' ? route.query.q : ''))
const tagOrigin = computed(() =>
  ['ai', 'manual'].includes(String(route.query.tag_origin))
    ? String(route.query.tag_origin)
    : '',
)
const sourceCategory = computed(() =>
  typeof route.query.source_category === 'string' ? route.query.source_category : '全部',
)
const sort = computed(() =>
  ['sales', 'amount', 'ordered', 'replenishment'].includes(String(route.query.sort))
    ? String(route.query.sort)
    : 'imported',
)
const periodOptions = computed(() => libraryMeta.value.factory['下单'] || [])
const ownFactory = computed(() => sourceCategory.value === '本厂产品')
const pendingFactory = ref(0)
let factoryRequest: ReturnType<typeof getFactoryWorkspace> | undefined
watch(ownFactory, () => { factoryRequest = undefined })
function loadPendingFactory() {
  if (!ownFactory.value || factoryRequest) return
  const request = getFactoryWorkspace()
  factoryRequest = request
  void request.then(data => {
    if (factoryRequest === request) pendingFactory.value = data.counts.missing_images
  }, () => {
    if (factoryRequest === request) factoryRequest = undefined
  })
}
const statusOptions = [
  { value: '', label: '全部' },
  { value: 'ai', label: 'AI 识别' },
  { value: 'manual', label: '人工已修改' },
]
const sourceTabs = computed(() => [
  '全部',
  ...libraryMeta.value.source_categories,
  libraryMeta.value.unclassified,
])
const level1Options = computed(() => [
  '全部',
  ...taxonomy.value.tree.map((item) => item.level1),
  taxonomy.value.unsorted || '待归类',
])
const subcategoryOptions = computed(() => {
  const node = taxonomy.value.tree.find((item) => item.level1 === filters.value.category)
  return node?.children.length ? ['全部', ...node.children] : []
})
function seasonSelected(value: string) {
  if (!ownFactory.value) return filters.value.season === value
  return value === '全部' ? !orderSeasons.value.length : orderSeasons.value.includes(value)
}
function toggleSeason(value: string) {
  if (!ownFactory.value) return setFilter('season', value)
  const seasons = value === '全部' ? [] : orderSeasons.value.includes(value)
    ? orderSeasons.value.filter(season => season !== value)
    : [...orderSeasons.value, value]
  const next = { ...route.query }
  if (seasons.length) next.order_season = seasons
  else delete next.order_season
  delete next.archive_season
  router.replace({ query: next })
}
function removeChip(chip: { key: string; label: string }) {
  if (chip.key === 'order_season') toggleSeason(chip.label)
  else setFilter(chip.key, '全部')
}
const seasonOptions = computed(() => [
  '全部',
  ...(ownFactory.value ? libraryMeta.value.factory['订单年度/季节'] || [] : meta.value.fields['季节'] || []),
])
const colorKey = computed(() => (ownFactory.value ? 'color_family' : 'color'))
const activeColor = computed(() =>
  ownFactory.value ? filters.value.color_family : filters.value.color,
)
const swatches = computed(() => {
  const order = ownFactory.value ? FACTORY_COLORS : meta.value.fields['颜色主色调'] || []
  const names = order.filter((name) => colorCounts.value[name])
  for (const name of Object.keys(colorCounts.value)) if (!names.includes(name)) names.push(name)
  if (activeColor.value !== '全部' && !names.includes(activeColor.value))
    names.push(activeColor.value)
  return names.map((name) => ({ name, count: colorCounts.value[name] || 0 }))
})
const extraFilters = computed(
  () =>
    [
      ['style', '风格', meta.value.fields['风格'] || []],
      ['fit', '版型', meta.value.fields['版型'] || []],
      ['neckline', '领型', meta.value.fields['领型'] || []],
      ['sleeve', '袖型', meta.value.fields['袖型'] || []],
      ['pattern', '图案', meta.value.fields['图案'] || []],
    ] as const,
)
const selectedChips = computed(() => {
  const chips: { key: string; label: string }[] = []
  if (filters.value.category !== '全部')
    chips.push({ key: 'category', label: filters.value.category })
  if (filters.value.subcategory !== '全部')
    chips.push({ key: 'subcategory', label: filters.value.subcategory })
  if (ownFactory.value && filters.value.sleeve_length !== '全部')
    chips.push({ key: 'sleeve_length', label: filters.value.sleeve_length })
  if (ownFactory.value && filters.value.fabric !== '全部')
    chips.push({ key: 'fabric', label: filters.value.fabric })
  if (ownFactory.value) {
    for (const season of orderSeasons.value) chips.push({ key: 'order_season', label: season })
  } else if (filters.value.season !== '全部') chips.push({ key: 'season', label: filters.value.season })
  if (ownFactory.value && filters.value.ordered_from !== '全部')
    chips.push({ key: 'ordered_from', label: `从 ${filters.value.ordered_from}` })
  if (ownFactory.value && filters.value.ordered_to !== '全部')
    chips.push({ key: 'ordered_to', label: `到 ${filters.value.ordered_to}` })
  if (activeColor.value !== '全部') chips.push({ key: colorKey.value, label: activeColor.value })
  if (filters.value.pattern !== '全部') chips.push({ key: 'pattern', label: filters.value.pattern })
  if (!ownFactory.value) {
    for (const [key, , options] of extraFilters.value) {
      if (key === 'pattern') continue
      const value = filters.value[key]
      if (value !== '全部' && (options.includes(value) || value)) chips.push({ key, label: value })
    }
  }
  return chips
})

const facetQueryKeys = [
  'category',
  'subcategory',
  'sleeve_length',
  'fabric',
  'color',
  'style',
  'season',
  'fit',
  'neckline',
  'sleeve',
  'pattern',
  'archive_season',
  'order_season',
  'color_family',
  'size',
  'series',
  'needle',
  'customer',
  'group',
  'ordered_from',
  'ordered_to',
]
function setSource(value: string) {
  const next = { ...route.query }
  for (const key of facetQueryKeys) delete next[key]
  if (value === '全部') delete next.source_category
  else next.source_category = value
  router.replace({ query: next })
}
function categoryLine(item: CatalogItem) {
  const parts: string[] = []
  if (item.library_category) parts.push(item.library_category.replace('>', ' · '))
  if (item.subcategory && !item.library_category?.includes(item.subcategory))
    parts.push(item.subcategory)
  return parts.join(' · ')
}
function formatWan(amount: number) {
  const text = (amount / 10000).toLocaleString('zh-CN', { maximumFractionDigits: 1 })
  return `¥${text} 万`
}
function cardTitle(item: CatalogItem) {
  return (
    item.style_code ||
    (item.library_category || item.display_tags.品类).split('>').at(-1) ||
    '待归类'
  )
}
function cardSeason(item: CatalogItem) {
  if (item.source_category === '本厂产品')
    return item.order_seasons?.length ? `订单：${item.order_seasons.join(' / ')}` : '订单季节未提供'
  return item.display_tags.季节 ? `适穿：${item.display_tags.季节}` : ''
}
const orderScopeText = computed(() => {
  const parts = []
  if (orderSeasons.value.length) parts.push(orderSeasons.value.join('、'))
  let start = filters.value.ordered_from
  let end = filters.value.ordered_to
  const periodKey = (value: string) => {
    const match = value.match(/^(\d{4})年(\d{1,2})月(上旬|中旬|下旬)$/)
    return match ? `${match[1]}-${match[2]!.padStart(2, '0')}-${['上旬', '中旬', '下旬'].indexOf(match[3]!) + 1}` : value
  }
  if (start !== '全部' && end !== '全部' && periodKey(start) > periodKey(end)) [start, end] = [end, start]
  if (start !== '全部' || end !== '全部')
    parts.push(`下单时间 ${start === '全部' ? '不限' : start} 至 ${end === '全部' ? '不限' : end}（含起止旬）`)
  return parts.length ? parts.join(' · ') : '全部订单季节、全部下单时间（含已计入的历史汇总）'
})
function cardSubtitle(item: CatalogItem) {
  if (item.source_category === '本厂产品') return categoryLine(item).replace(' · ', ' / ')
  const group = (item.library_category || item.display_tags.品类).split('>')[0]
  return [group, item.display_tags.风格].filter(Boolean).join(' / ')
}
function clearFacets() {
  const next = { ...route.query }
  for (const key of facetQueryKeys) delete next[key]
  router.replace({ query: next })
}
function applySearch(value: string) {
  window.clearTimeout(searchTimer)
  const next = value.trim()
  if (next === q.value) return
  setFilter('q', next || '全部')
}
function onSearchInput(event: Event) {
  const value = (event.target as HTMLInputElement).value
  queryDraft.value = value
  window.clearTimeout(searchTimer)
  searchTimer = window.setTimeout(() => applySearch(value), 250)
}

let latest = 0
async function load() {
  const sequence = ++latest
  loading.value = true
  error.value = ''
  try {
    const data = await getCatalog({
      q: q.value,
      category: filters.value.category,
      subcategory: filters.value.subcategory,
      sleeve_length: ownFactory.value ? filters.value.sleeve_length : '',
      fabric: ownFactory.value ? filters.value.fabric : '',
      color: ownFactory.value ? '' : filters.value.color,
      style: ownFactory.value ? '' : filters.value.style,
      season: ownFactory.value ? '' : filters.value.season,
      fit: ownFactory.value ? '' : filters.value.fit,
      neckline: ownFactory.value ? '' : filters.value.neckline,
      sleeve: ownFactory.value ? '' : filters.value.sleeve,
      pattern: filters.value.pattern,
      order_season: ownFactory.value ? orderSeasons.value : '',
      color_family: ownFactory.value ? filters.value.color_family : '',
      ordered_from: ownFactory.value ? filters.value.ordered_from : '',
      ordered_to: ownFactory.value ? filters.value.ordered_to : '',
      source_category: sourceCategory.value,
      sort: sort.value,
      exclude_samples: true,
      tag_origin: tagOrigin.value,
      batch: typeof route.query.batch === 'string' ? route.query.batch : '',
    })
    if (sequence !== latest) return
    items.value = data.items
    count.value = data.count
    colorCounts.value = data.color_counts || {}
    loadPendingFactory()
  } catch (reason) {
    if (sequence === latest) error.value = reason instanceof Error ? reason.message : String(reason)
  } finally {
    if (sequence === latest) loading.value = false
  }
}

async function bulkAssign() {
  bulkMessage.value = ''
  if (!items.value.length) return
  if (
    !window.confirm(`将当前 ${items.value.length} 条结果的资料大类改为「${bulkCategory.value}」？`)
  )
    return
  try {
    await assignSourceCategory(
      items.value.map((item) => item.id),
      bulkCategory.value,
    )
    bulkMessage.value = `已将 ${items.value.length} 条结果改为${bulkCategory.value}`
    await load()
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : String(reason)
  }
}

function toggleSelectionMode() {
  selectionMode.value = !selectionMode.value
  selected.value.clear()
}

function toggleImage(id: string) {
  if (loading.value || deleting.value) return
  if (selected.value.has(id)) selected.value.delete(id)
  else selected.value.add(id)
}

function onCardClick(event: MouseEvent, id: string) {
  if (!selectionMode.value) return
  event.preventDefault()
  toggleImage(id)
}

async function deleteSelected() {
  const ids = items.value.filter(item => selected.value.has(item.id)).map(item => item.id)
  if (!ids.length || deleting.value || loading.value) return
  if (!window.confirm(`删除已选的 ${ids.length} 张图片？删除后无法在图库内恢复。`)) return
  deleting.value = true
  error.value = ''
  deletionMessage.value = ''
  cleanupPending.value = 0
  try {
    const result = await deleteGalleryImages(ids)
    selected.value.clear()
    selectionMode.value = false
    deletionMessage.value = `已删除 ${result.deleted} 张图片。`
    cleanupPending.value = result.cleanup_pending
    await load()
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : String(reason)
  } finally {
    deleting.value = false
  }
}

onUnmounted(() => {
  window.clearTimeout(searchTimer)
  latest++
  factoryRequest = undefined
})

onMounted(async () => {
  queryDraft.value = q.value
  void load()
  try {
    await Promise.all([
      getFilters().then(data => { meta.value = data }),
      getLibraryMeta().then(data => { libraryMeta.value = data }),
      getTaxonomy().then(data => { taxonomy.value = data }),
    ])
  } catch (reason) {
    metadataError.value = reason instanceof Error ? reason.message : String(reason)
  }
})
watch(
  () => route.query.q,
  (value) => {
    const text = typeof value === 'string' ? value : ''
    if (text !== queryDraft.value) queryDraft.value = text
  },
)
watch(() => route.query, () => {
  selected.value.clear()
  void load()
}, { deep: true })
</script>

<template>
  <main class="library-page gallery-redesign">
    <div class="gallery-page-heading">
      <h1>款式图库</h1>
      <form class="library-search" @submit.prevent="applySearch(queryDraft)">
        <label class="visually-hidden" for="library-search">搜索款号、客户、颜色名</label
        ><input
          id="library-search"
          :value="queryDraft"
          type="search"
          name="library-search"
          placeholder="搜索款号、客户、颜色名…"
          @input="onSearchInput"
        />
      </form>
    </div>
    <div class="library-top">
      <div class="source-tabs filter-scroll" role="group" aria-label="资料大类">
        <button
          v-for="item in sourceTabs"
          :key="item"
          type="button"
          :aria-pressed="sourceCategory === item"
          @click="setSource(item)"
        >
          {{ item }}
        </button>
      </div>
      <RouterLink
        class="gallery-analytics-link"
        :to="ownFactory
          ? { name: 'analytics' }
          : { name: 'analytics', query: { tab: 'questions', source: sourceCategory } }"
      >{{ ownFactory ? '数据分析' : '问数据' }}</RouterLink>
    </div>
    <div v-if="ownFactory" class="factory-library-tools">
      <RouterLink :to="{ path: '/import', query: { mode: 'factory' } }">管理本厂资料与导入</RouterLink>
      <span v-if="pendingFactory">{{ pendingFactory }} 个款号已导入数据，待补图片</span>
    </div>
    <div v-if="ownFactory" class="gallery-order-note">
      <p><strong>订单统计范围：</strong>{{ orderScopeText }}</p>
      <details><summary>统计说明</summary>
        <p>卡片件数、金额及订单排序随订单季节和下单时间变化；品类、颜色等条件仅筛选款式。限定季节或时间时，仅统计对应订单明细，不计入无法对应范围的历史汇总。</p>
        <p>订单包含补单，不代表已发货或已回款。春夏、秋冬为独立季节，不自动合并春、夏或秋、冬。</p>
      </details>
    </div>
    <div class="library-filters">
      <div class="filter-row">
        <span class="filter-row-label">品类</span>
        <div class="filter-scroll" role="group" aria-label="品类">
          <button
            v-for="opt in level1Options"
            :key="`c-${opt}`"
            type="button"
            class="chip"
            :aria-pressed="filters.category === opt"
            @click="setFilter('category', opt)"
          >
            {{ opt }}
          </button>
        </div>
      </div>
      <div v-if="subcategoryOptions.length" class="filter-row">
        <span class="filter-row-label">二级</span>
        <div class="filter-scroll" role="group" aria-label="二级品类">
          <button
            v-for="opt in subcategoryOptions"
            :key="`sub-${opt}`"
            type="button"
            class="chip"
            :aria-pressed="filters.subcategory === opt"
            @click="setFilter('subcategory', opt)"
          >
            {{ opt }}
          </button>
        </div>
      </div>

      <div class="season-color-row">
        <div class="filter-row">
          <span class="filter-row-label season-filter-label">{{ ownFactory ? '订单年度 / 季节（可多选）' : '适穿季节' }}</span>
          <div class="filter-scroll" role="group" :aria-label="ownFactory ? '订单年度 / 季节' : '适穿季节'">
            <button
              v-for="opt in seasonOptions"
              :key="`s-${opt}`"
              type="button"
              class="chip"
              :aria-pressed="seasonSelected(opt)"
              @click="toggleSeason(opt)"
            >
              {{ opt }}
            </button>
          </div>
        </div>

        <div class="filter-row">
          <span class="filter-row-label">颜色</span>
          <div class="filter-scroll swatch-row" role="group" aria-label="颜色">
            <button
              v-for="swatch in swatches"
              :key="swatch.name"
              type="button"
              class="swatch"
              :class="{ light: swatchLight(swatch.name) }"
              :style="{ background: swatchColor(swatch.name) }"
              :aria-pressed="activeColor === swatch.name"
              :aria-label="`${swatch.name}，${swatch.count} 款`"
              @click="setFilter(colorKey, activeColor === swatch.name ? '全部' : swatch.name)"
            >
              <span class="swatch-tip">{{ swatch.name }} · {{ swatch.count }} 款</span>
            </button>
            <span v-if="!swatches.length" class="meta">暂无颜色</span>
          </div>
        </div>
      </div>
      <details class="more-filters">
        <summary>更多筛选</summary>
        <div v-if="ownFactory" class="filter-row">
          <span class="filter-row-label">袖长</span>
          <div class="filter-scroll" role="group" aria-label="袖长">
            <button
              type="button"
              class="chip"
              :aria-pressed="filters.sleeve_length === '全部'"
              @click="setFilter('sleeve_length', '全部')"
            >
              全部
            </button>
            <button
              v-for="opt in taxonomy.sleeves"
              :key="`sleeve-${opt}`"
              type="button"
              class="chip"
              :aria-pressed="filters.sleeve_length === opt"
              @click="setFilter('sleeve_length', opt)"
            >
              {{ opt }}
            </button>
          </div>
        </div>
        <div v-if="ownFactory" class="filter-row">
          <span class="filter-row-label">下单</span>
          <div class="period-range">
            <label
              >从
              <select
                aria-label="下单开始"
                :value="filters.ordered_from"
                @change="setFilter('ordered_from', ($event.target as HTMLSelectElement).value)"
              >
                <option value="全部">不限</option>
                <option v-for="opt in periodOptions" :key="`from-${opt}`" :value="opt">
                  {{ opt }}
                </option>
              </select>
            </label>
            <label
              >到
              <select
                aria-label="下单结束"
                :value="filters.ordered_to"
                @change="setFilter('ordered_to', ($event.target as HTMLSelectElement).value)"
              >
                <option value="全部">不限</option>
                <option v-for="opt in periodOptions" :key="`to-${opt}`" :value="opt">
                  {{ opt }}
                </option>
              </select>
            </label>
          </div>
        </div>
        <div v-if="ownFactory" class="filter-row">
          <span class="filter-row-label">面料</span>
          <div class="filter-scroll" role="group" aria-label="面料">
            <button
              type="button"
              class="chip"
              :aria-pressed="filters.fabric === '全部'"
              @click="setFilter('fabric', '全部')"
            >
              全部
            </button>
            <button
              v-for="opt in taxonomy.fabrics"
              :key="`fabric-${opt}`"
              type="button"
              class="chip"
              :aria-pressed="filters.fabric === opt"
              @click="setFilter('fabric', opt)"
            >
              {{ opt }}
            </button>
          </div>
        </div>
        <div
          v-for="[key, label, options] in extraFilters"
          v-show="!ownFactory || key === 'pattern'"
          :key="key"
          class="filter-row"
        >
          <span class="filter-row-label">{{ label }}</span>
          <div class="filter-scroll" role="group" :aria-label="label">
            <button
              type="button"
              class="chip"
              :aria-pressed="filters[key] === '全部'"
              @click="setFilter(key, '全部')"
            >
              全部
            </button>
            <button
              v-for="opt in options"
              :key="`${key}-${opt}`"
              type="button"
              class="chip"
              :aria-pressed="filters[key] === opt"
              @click="setFilter(key, opt)"
            >
              {{ opt }}
            </button>
          </div>
        </div>
      </details>
    </div>
    <div v-if="selectedChips.length" class="gallery-active-filters">
      <span>已选条件</span>
      <div class="selected-chips">
        <button
          v-for="chip in selectedChips"
          :key="`${chip.key}-${chip.label}`"
          type="button"
          class="selected-chip"
          @click="removeChip(chip)"
        >
          {{ chip.label }} <span aria-hidden="true">×</span
          ><span class="visually-hidden">移除</span>
        </button>
        <button
          v-if="selectedChips.length"
          id="library-clear"
          type="button"
          class="text-button"
          @click="clearFacets"
        >
          清空
        </button>
      </div>
    </div>
    <div class="gallery-results-toolbar">
      <p class="gallery-count" role="status">共 {{ loading ? '…' : count }} 款</p>
      <div class="library-selected-side">
        <div class="library-status-filter" role="group" aria-label="按标签来源筛选">
          <button
            v-for="option in statusOptions"
            :key="option.value || 'all'"
            type="button"
            :aria-pressed="tagOrigin === option.value"
            @click="setFilter('tag_origin', option.value || '全部')"
          >
            {{ option.label }}
          </button>
        </div>
        <label class="sort-field"
          >排序
          <select
            id="library-sort"
            :value="sort"
            aria-label="排序"
            @change="
              setFilter(
                'sort',
                ($event.target as HTMLSelectElement).value === 'imported'
                  ? '全部'
                  : ($event.target as HTMLSelectElement).value,
              )
            "
          >
            <option value="imported">最新导入</option>
            <option value="sales">按订单件数</option>
            <option value="amount">按订单金额</option>
            <option value="ordered">最近下单</option>
            <option value="replenishment">补单次数</option>
          </select>
        </label>
        <button type="button" class="btn small" :aria-pressed="selectionMode" :disabled="deleting || (!items.length && !selectionMode)" @click="toggleSelectionMode">
          {{ selectionMode ? '退出选择' : '批量删除' }}
        </button>
        <details class="quiet-more">
          <summary>更多</summary>
          <div class="quiet-more-body">
            <label for="bulk-category">将当前结果的资料大类改为</label>
            <select id="bulk-category" v-model="bulkCategory">
              <option
                v-for="item in sourceTabs.filter((item) => item !== '全部')"
                :key="item"
                :value="item"
              >
                {{ item }}
              </option>
            </select>
            <button
              class="btn"
              type="button"
              :disabled="!items.length || loading"
              @click="bulkAssign"
            >
              应用到当前结果
            </button>
            <span v-if="bulkMessage" class="meta">{{ bulkMessage }}</span>
          </div>
        </details>
      </div>
    </div>

    <div v-if="selectionMode" class="gallery-selection-toolbar" aria-label="批量选择">
      <span role="status">已选 {{ selected.size }} 张</span>
      <button type="button" class="btn small" :disabled="loading || deleting || !items.length || selected.size === items.length" @click="selected = new Set(items.map(item => item.id))">全选当前结果</button>
      <button type="button" class="btn small" :disabled="deleting || !selected.size" @click="selected.clear()">清空选择</button>
      <button type="button" class="btn small media-delete" :disabled="loading || deleting || !selected.size" @click="deleteSelected">{{ deleting ? '正在删除…' : '删除已选图片' }}</button>
    </div>
    <p v-if="deletionMessage" class="gallery-delete-result" role="status">
      {{ deletionMessage }}
      <template v-if="cleanupPending">还有 {{ cleanupPending }} 项文件待清理。<RouterLink to="/import">到导入页重试清理</RouterLink></template>
    </p>
    <p v-if="editMessage" class="meta" role="status">{{ editMessage }}</p>
    <p v-if="route.query.batch" class="meta">当前显示本批导入的图片。<RouterLink :to="{ path: '/', query: { ...route.query, batch: undefined } }">查看全部批次</RouterLink></p>
    <div v-if="error || metadataError" class="error">{{ error || metadataError }}</div>
    <div v-if="!items.length && !loading && !error" class="empty">
      暂无匹配款式。可调整筛选，或<RouterLink to="/import" class="review-text-link"
        >导入新资料</RouterLink
      >。
    </div>
    <div v-else class="gallery-grid" :aria-busy="loading">
      <article v-for="item in items" :key="item.id" class="gallery-card" :class="{ 'is-selected': selected.has(item.id) }">
        <label v-if="selectionMode" class="gallery-card-select">
          <input type="checkbox" :aria-label="`选择 ${cardTitle(item)} · ${item.source.source_file} ${item.source.location}`" :checked="selected.has(item.id)" :disabled="deleting || loading" @change="toggleImage(item.id)" />
        </label>
        <RouterLink
          class="gallery-card-link"
          :to="{ name: 'record', params: { id: item.id }, query: { from: route.fullPath } }"
          :tabindex="selectionMode ? -1 : undefined"
          @click.capture="onCardClick($event, item.id)"
        >
          <div class="gallery-card-image" :class="{ 'has-cover-crop': item.cover_crop }">
            <img
              :src="item.cover_url || item.thumbnail_url || item.image_url"
              :alt="item.style_code || item.library_category || item.display_tags.品类"
              loading="lazy"
              width="480"
              height="640"
            />
            <span class="gallery-review-badge" :class="item.review_status">{{
              tagStatus(item)
            }}</span>
            <span v-if="item.source_category === '本厂产品'" class="gallery-factory-badge"
              >本厂产品</span
            >
          </div>
          <div class="gallery-card-body">
            <div class="gallery-card-heading">
              <h2 :title="cardTitle(item)" :translate="item.style_code ? 'no' : undefined">
                {{ cardTitle(item) }}
              </h2>
              <span :title="cardSeason(item)">{{
                cardSeason(item)
              }}</span>
            </div>
            <p class="gallery-card-subtitle" :title="cardSubtitle(item)">
              {{ cardSubtitle(item) || '待补充分类' }}
            </p>
            <div class="gallery-card-options">
              <div
                class="gallery-color-dots"
                :aria-label="
                  item.color_families?.length
                    ? `色系：${item.color_families.join('、')}`
                    : '暂无色系信息'
                "
              >
                <span
                  v-for="name in (item.color_families || []).slice(0, 7)"
                  :key="name"
                  class="gallery-color-dot"
                  :class="{ light: swatchLight(name) }"
                  :style="{ background: swatchColor(name) }"
                  :title="name"
                ></span
                ><span v-if="(item.color_families?.length || 0) > 7" class="gallery-more-colors"
                  >+{{ item.color_families!.length - 7 }}</span
                >
              </div>
              <span
                v-if="item.source_category === '本厂产品' && item.variant_count"
                class="gallery-variant-count"
                >{{ item.variant_count }} 个版本</span
              >
            </div>
            <div class="gallery-card-footer" :title="item.source_category === '本厂产品' ? `订单件数与订单金额 · ${ownFactory ? orderScopeText : '全部季节、全部下单时间累计'}` : undefined">
              <template v-if="item.source_category === '本厂产品'"
                ><strong><small>订单 </small>{{ item.sales_quantity.toLocaleString('zh-CN') }}<small> 件</small></strong
                ><span v-if="item.sales_amount" class="gallery-sales-amount">{{
                  formatWan(item.sales_amount)
                }}</span
                ><span v-else class="gallery-source-name">暂无金额</span></template
              ><span v-else class="gallery-source-name">{{
                item.source_category || '未分类'
              }}</span>
            </div>
          </div>
        </RouterLink>
        <button v-if="!selectionMode" type="button" class="gallery-edit-tags" :aria-label="`修改标签：${cardTitle(item)}`" @click="editing = item">修改标签</button>
      </article>
    </div>
    <TagEditor v-if="editing" :key="editing.id" :item="editing" @close="editing = null" @saved="tagsSaved" />
  </main>
</template>
<style src="../library-gallery.css"></style>
