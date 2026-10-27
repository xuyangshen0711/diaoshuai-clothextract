<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { assignSourceCategory, getCatalog, getFilters, getLibraryMeta, getTaxonomy, type CatalogItem, type FilterMeta, type LibraryMeta, type Taxonomy } from '../api'
import { swatchColor, swatchLight } from '../utils/source'

const route = useRoute()
const router = useRouter()
const items = ref<CatalogItem[]>([])
const count = ref(0)
const colorCounts = ref<Record<string, number>>({})
const loading = ref(false)
const error = ref('')
const meta = ref<FilterMeta>({})
const libraryMeta = ref<LibraryMeta>({ source_categories: ['本厂产品', '网图趋势', '品牌资料包', '市调拍照'], unclassified: '未分类', factory: {} })
const taxonomy = ref<Taxonomy>({ unsorted: '待归类', sleeves: [], fabrics: [], tree: [], labels: [], synonyms: [] })
const bulkCategory = ref('本厂产品')
const bulkMessage = ref('')
const queryDraft = ref('')
let searchTimer = 0

const FACTORY_COLORS = ['红', '橙', '黄', '绿', '蓝', '紫', '粉', '棕咖', '米白', '灰', '黑', '白', '其他']
const queryText = (key: string) => typeof route.query[key] === 'string' ? route.query[key] as string : '全部'
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
  archive_season: queryText('archive_season'),
  color_family: queryText('color_family'),
}))
function setFilter(key: string, value: string) {
  const next = { ...route.query, [key]: value === '全部' ? undefined : value }
  if (key === 'category') delete next.subcategory
  router.replace({ query: next })
}
const q = computed(() => (typeof route.query.q === 'string' ? route.query.q : ''))
const reviewStatus = computed(() => ['pending', 'approved'].includes(String(route.query.review_status)) ? String(route.query.review_status) : '')
const sourceCategory = computed(() => typeof route.query.source_category === 'string' ? route.query.source_category : '全部')
const sort = computed(() => route.query.sort === 'sales' || route.query.sort === 'amount' ? route.query.sort : 'imported')
const ownFactory = computed(() => sourceCategory.value === '本厂产品')
const statusOptions = [{ value: '', label: '全部' }, { value: 'pending', label: '未审核' }, { value: 'approved', label: '已审核' }]
const sourceTabs = computed(() => ['全部', ...libraryMeta.value.source_categories, libraryMeta.value.unclassified])
const level1Options = computed(() => ['全部', ...taxonomy.value.tree.map(item => item.level1), taxonomy.value.unsorted || '待归类'])
const subcategoryOptions = computed(() => {
  const node = taxonomy.value.tree.find(item => item.level1 === filters.value.category)
  return node?.children.length ? ['全部', ...node.children] : []
})
const seasonKey = computed(() => ownFactory.value ? 'archive_season' : 'season')
const seasonValue = computed(() => ownFactory.value ? filters.value.archive_season : filters.value.season)
const seasonOptions = computed(() => ['全部', ...(ownFactory.value ? libraryMeta.value.factory['季节'] || [] : meta.value['季节'] || [])])
const colorKey = computed(() => ownFactory.value ? 'color_family' : 'color')
const activeColor = computed(() => ownFactory.value ? filters.value.color_family : filters.value.color)
const swatches = computed(() => {
  const order = ownFactory.value ? FACTORY_COLORS : (meta.value['颜色主色调'] || [])
  const names = order.filter(name => colorCounts.value[name])
  for (const name of Object.keys(colorCounts.value)) if (!names.includes(name)) names.push(name)
  if (activeColor.value !== '全部' && !names.includes(activeColor.value)) names.push(activeColor.value)
  return names.map(name => ({ name, count: colorCounts.value[name] || 0 }))
})
const extraFilters = computed(() => [
  ['style', '风格', meta.value['风格'] || []],
  ['fit', '版型', meta.value['版型'] || []],
  ['neckline', '领型', meta.value['领型'] || []],
  ['sleeve', '袖型', meta.value['袖型'] || []],
] as const)
const selectedChips = computed(() => {
  const chips: { key: string; label: string }[] = []
  if (filters.value.category !== '全部') chips.push({ key: 'category', label: filters.value.category })
  if (filters.value.subcategory !== '全部') chips.push({ key: 'subcategory', label: filters.value.subcategory })
  if (ownFactory.value && filters.value.sleeve_length !== '全部') chips.push({ key: 'sleeve_length', label: filters.value.sleeve_length })
  if (ownFactory.value && filters.value.fabric !== '全部') chips.push({ key: 'fabric', label: filters.value.fabric })
  if (seasonValue.value !== '全部') chips.push({ key: seasonKey.value, label: seasonValue.value })
  if (activeColor.value !== '全部') chips.push({ key: colorKey.value, label: activeColor.value })
  if (!ownFactory.value) {
    for (const [key, , options] of extraFilters.value) {
      const value = filters.value[key]
      if (value !== '全部' && (options.includes(value) || value)) chips.push({ key, label: value })
    }
  }
  return chips
})

const facetQueryKeys = ['category', 'subcategory', 'sleeve_length', 'fabric', 'color', 'style', 'season', 'fit', 'neckline', 'sleeve', 'archive_season', 'color_family', 'size', 'series', 'needle', 'customer', 'group']
function setSource(value: string) {
  const next = { ...route.query }
  for (const key of facetQueryKeys) delete next[key]
  if (value === '全部') delete next.source_category
  else next.source_category = value
  router.replace({ query: next })
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
      archive_season: ownFactory.value ? filters.value.archive_season : '',
      color_family: ownFactory.value ? filters.value.color_family : '',
      source_category: sourceCategory.value,
      sort: sort.value,
      exclude_samples: true,
      review_status: reviewStatus.value,
    })
    if (sequence !== latest) return
    items.value = data.items
    count.value = data.count
    colorCounts.value = data.color_counts || {}
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

onMounted(async () => {
  queryDraft.value = q.value
  try {
    meta.value = await getFilters()
    libraryMeta.value = await getLibraryMeta()
    taxonomy.value = await getTaxonomy()
  } catch (reason) { error.value = reason instanceof Error ? reason.message : String(reason) }
  await load()
})
watch(() => route.query.q, value => {
  const text = typeof value === 'string' ? value : ''
  if (text !== queryDraft.value) queryDraft.value = text
})
watch(() => route.query, load, { deep: true })
</script>

<template>
  <section class="library-page">
    <h2 class="visually-hidden">款式列表</h2>
    <div class="library-filters">
      <div class="library-top">
        <div class="source-tabs filter-scroll" role="group" aria-label="资料大类">
          <button v-for="item in sourceTabs" :key="item" type="button" :aria-pressed="sourceCategory === item" @click="setSource(item)">{{ item }}</button>
        </div>
        <form class="library-search" @submit.prevent="applySearch(queryDraft)">
          <label class="visually-hidden" for="library-search">搜索款号、客户、颜色名</label>
          <input id="library-search" :value="queryDraft" type="search" placeholder="搜索款号、客户、颜色名…" @input="onSearchInput" />
        </form>
        <label class="sort-field">排序
          <select id="library-sort" :value="sort" aria-label="排序" @change="setFilter('sort', ($event.target as HTMLSelectElement).value === 'imported' ? '全部' : ($event.target as HTMLSelectElement).value)">
            <option value="imported">最新导入</option>
            <option value="sales">按订单件数</option>
            <option value="amount">按订单金额</option>
          </select>
        </label>
      </div>

      <div class="filter-row">
        <span class="filter-row-label">品类</span>
        <div class="filter-scroll" role="group" aria-label="品类">
          <button v-for="opt in level1Options" :key="`c-${opt}`" type="button" class="chip" :aria-pressed="filters.category === opt" @click="setFilter('category', opt)">{{ opt }}</button>
        </div>
      </div>
      <div v-if="subcategoryOptions.length" class="filter-row">
        <span class="filter-row-label">二级</span>
        <div class="filter-scroll" role="group" aria-label="二级品类">
          <button v-for="opt in subcategoryOptions" :key="`sub-${opt}`" type="button" class="chip" :aria-pressed="filters.subcategory === opt" @click="setFilter('subcategory', opt)">{{ opt }}</button>
        </div>
      </div>
      <div v-if="ownFactory" class="filter-row">
        <span class="filter-row-label">袖长</span>
        <div class="filter-scroll" role="group" aria-label="袖长">
          <button type="button" class="chip" :aria-pressed="filters.sleeve_length === '全部'" @click="setFilter('sleeve_length', '全部')">全部</button>
          <button v-for="opt in taxonomy.sleeves" :key="`sleeve-${opt}`" type="button" class="chip" :aria-pressed="filters.sleeve_length === opt" @click="setFilter('sleeve_length', opt)">{{ opt }}</button>
        </div>
      </div>
      <div class="filter-row">
        <span class="filter-row-label">季节</span>
        <div class="filter-scroll" role="group" aria-label="季节">
          <button v-for="opt in seasonOptions" :key="`s-${opt}`" type="button" class="chip" :aria-pressed="seasonValue === opt" @click="setFilter(seasonKey, opt)">{{ opt }}</button>
        </div>
      </div>
      <div class="filter-row">
        <span class="filter-row-label">颜色</span>
        <div class="filter-scroll swatch-row" role="group" aria-label="颜色">
          <button v-for="swatch in swatches" :key="swatch.name" type="button" class="swatch" :class="{ light: swatchLight(swatch.name) }" :style="{ background: swatchColor(swatch.name) }" :aria-pressed="activeColor === swatch.name" :aria-label="`${swatch.name}，${swatch.count} 款`" @click="setFilter(colorKey, activeColor === swatch.name ? '全部' : swatch.name)">
            <span class="swatch-tip">{{ swatch.name }} · {{ swatch.count }} 款</span>
          </button>
          <span v-if="!swatches.length" class="meta">暂无颜色</span>
        </div>
      </div>
      <details class="more-filters">
        <summary>更多筛选</summary>
        <div v-if="ownFactory" class="filter-row">
          <span class="filter-row-label">面料</span>
          <div class="filter-scroll" role="group" aria-label="面料">
            <button type="button" class="chip" :aria-pressed="filters.fabric === '全部'" @click="setFilter('fabric', '全部')">全部</button>
            <button v-for="opt in taxonomy.fabrics" :key="`fabric-${opt}`" type="button" class="chip" :aria-pressed="filters.fabric === opt" @click="setFilter('fabric', opt)">{{ opt }}</button>
          </div>
        </div>
        <div v-for="[key, label, options] in extraFilters" v-show="!ownFactory" :key="key" class="filter-row">
          <span class="filter-row-label">{{ label }}</span>
          <div class="filter-scroll" role="group" :aria-label="label">
            <button type="button" class="chip" :aria-pressed="filters[key] === '全部'" @click="setFilter(key, '全部')">全部</button>
            <button v-for="opt in options" :key="`${key}-${opt}`" type="button" class="chip" :aria-pressed="filters[key] === opt" @click="setFilter(key, opt)">{{ opt }}</button>
          </div>
        </div>
      </details>

      <div class="library-selected">
        <div class="selected-chips">
          <button v-for="chip in selectedChips" :key="chip.key" type="button" class="selected-chip" @click="setFilter(chip.key, '全部')">{{ chip.label }} <span aria-hidden="true">×</span><span class="visually-hidden">移除</span></button>
          <button v-if="selectedChips.length" id="library-clear" type="button" class="text-button" @click="clearFacets">清空</button>
        </div>
        <div class="library-selected-side">
          <div class="library-status-filter" role="group" aria-label="按审核状态筛选">
            <button v-for="option in statusOptions" :key="option.value || 'all'" type="button" :aria-pressed="reviewStatus === option.value" @click="setFilter('review_status', option.value || '全部')">{{ option.label }}</button>
          </div>
          <p class="meta" role="status">共 {{ loading ? '…' : count }} 款</p>
          <details class="quiet-more">
            <summary>更多</summary>
            <div class="quiet-more-body">
              <label for="bulk-category">将当前结果的资料大类改为</label>
              <select id="bulk-category" v-model="bulkCategory">
                <option v-for="item in sourceTabs.filter(item => item !== '全部')" :key="item" :value="item">{{ item }}</option>
              </select>
              <button class="btn" type="button" :disabled="!items.length || loading" @click="bulkAssign">应用到当前结果</button>
              <span v-if="bulkMessage" class="meta">{{ bulkMessage }}</span>
            </div>
          </details>
        </div>
      </div>
    </div>

    <div v-if="error" class="error">{{ error }}</div>
    <div v-if="!items.length && !loading && !error" class="empty">暂无匹配款式。可调整筛选，或<RouterLink to="/import" class="review-text-link">导入新资料</RouterLink>。</div>
    <div v-else class="grid">
      <article v-for="item in items" :key="item.id" class="card">
        <RouterLink class="card-link" :to="{ name: 'record', params: { id: item.id }, query: { from: route.fullPath } }">
          <span class="library-review-badge" :class="item.review_status">{{ item.review_status === 'approved' ? '已审核' : '未审核' }}</span>
          <img :src="item.thumbnail_url || item.image_url" :alt="item.style_code || item.library_category || item.display_tags.品类" loading="lazy" width="180" height="240" />
          <div class="card-body">
            <div class="card-title-row">
              <strong>{{ item.style_code || item.library_category || item.display_tags.品类 }}</strong>
              <span v-if="item.style_code">{{ item.library_category || item.display_tags.品类 }}</span>
              <span v-else-if="item.display_tags.风格">{{ item.display_tags.风格 }}</span>
            </div>
            <div v-if="item.color_families?.length" class="card-dots" :aria-label="item.color_families.join('、')">
              <span v-for="name in item.color_families" :key="name" class="card-dot" :class="{ light: swatchLight(name) }" :style="{ background: swatchColor(name) }" :title="name"></span>
            </div>
            <div v-if="item.source_category === '本厂产品'" class="card-pills">
              <span v-if="item.variant_count" class="mini-pill">{{ item.variant_count }} 个客户版本</span>
              <span class="sold-label">已售 {{ item.sales_quantity.toLocaleString('zh-CN') }} 件</span>
            </div>
          </div>
        </RouterLink>
      </article>
    </div>
  </section>
</template>
