<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { assignSourceCategory, getCatalog, getFilters, getLibraryMeta, getTaxonomy, type CatalogItem, type FilterMeta, type LibraryMeta, type Taxonomy } from '../api'

const route = useRoute()
const router = useRouter()
const items = ref<CatalogItem[]>([])
const count = ref(0)
const colorCounts = ref<Record<string, number>>({})
const loading = ref(false)
const error = ref('')
const meta = ref<FilterMeta>({ fields: {}, detail_groups: {} })
const libraryMeta = ref<LibraryMeta>({ source_categories: ['本厂产品', '网图趋势', '品牌资料包', '市调拍照'], unclassified: '未分类', factory: {} })
const taxonomy = ref<Taxonomy>({ unsorted: '待归类', sleeves: [], fabrics: [], tree: [], labels: [] })
const bulkCategory = ref('本厂产品')
const bulkMessage = ref('')
const queryDraft = ref('')
const viewMode = ref<'grid' | 'list'>('grid')
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
  pattern: queryText('pattern'),
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
const seasonOptions = computed(() => ['全部', ...(ownFactory.value ? libraryMeta.value.factory['季节'] || [] : meta.value.fields['季节'] || [])])
const sleeveKey = computed(() => ownFactory.value ? 'sleeve_length' : 'sleeve')
const sleeveValue = computed(() => ownFactory.value ? filters.value.sleeve_length : filters.value.sleeve)
const sleeveLabel = computed(() => ownFactory.value ? '袖长' : '袖型')
const sleeveOptions = computed(() => ['全部', ...(ownFactory.value ? taxonomy.value.sleeves : meta.value.fields['袖型'] || [])])
const colorKey = computed(() => ownFactory.value ? 'color_family' : 'color')
const activeColor = computed(() => ownFactory.value ? filters.value.color_family : filters.value.color)
const colorOptions = computed(() => {
  const order = ownFactory.value ? FACTORY_COLORS : (meta.value.fields['颜色主色调'] || [])
  const names = order.filter(name => ownFactory.value || colorCounts.value[name])
  for (const name of Object.keys(colorCounts.value)) if (!names.includes(name)) names.push(name)
  if (activeColor.value !== '全部' && !names.includes(activeColor.value)) names.push(activeColor.value)
  return ['全部', ...names]
})
const extraFilters = computed(() => [
  ['style', '风格', meta.value.fields['风格'] || []],
  ['fit', '版型', meta.value.fields['版型'] || []],
  ['neckline', '领型', meta.value.fields['领型'] || []],
  ['sleeve', '袖型', meta.value.fields['袖型'] || []],
  ['pattern', '图案', meta.value.fields['图案'] || []],
] as const)
const selectedChips = computed(() => {
  const chips: { key: string; label: string }[] = []
  if (filters.value.category !== '全部') chips.push({ key: 'category', label: filters.value.category })
  if (filters.value.subcategory !== '全部') chips.push({ key: 'subcategory', label: filters.value.subcategory })
  if (ownFactory.value && filters.value.sleeve_length !== '全部') chips.push({ key: 'sleeve_length', label: filters.value.sleeve_length })
  if (ownFactory.value && filters.value.fabric !== '全部') chips.push({ key: 'fabric', label: filters.value.fabric })
  if (seasonValue.value !== '全部') chips.push({ key: seasonKey.value, label: seasonValue.value })
  if (activeColor.value !== '全部') chips.push({ key: colorKey.value, label: activeColor.value })
  if (reviewStatus.value) chips.push({ key: 'review_status', label: reviewStatus.value === 'pending' ? '未审核' : '已审核' })
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

const facetQueryKeys = ['category', 'subcategory', 'sleeve_length', 'fabric', 'color', 'style', 'season', 'fit', 'neckline', 'sleeve', 'pattern', 'archive_season', 'color_family', 'size', 'series', 'needle', 'customer', 'group', 'review_status']
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
      pattern: filters.value.pattern,
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
        <div class="source-tabs library-source-tabs filter-scroll" role="group" aria-label="资料大类">
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

      <div class="library-filterbar">
        <div class="library-select-filters">
          <label class="compact-filter" for="library-category">品类
            <select id="library-category" :value="filters.category" @change="setFilter('category', ($event.target as HTMLSelectElement).value)">
              <option v-for="option in level1Options" :key="option" :value="option">{{ option }}</option>
            </select>
          </label>
          <label class="compact-filter" for="library-sleeve">{{ sleeveLabel }}
            <select id="library-sleeve" :value="sleeveValue" @change="setFilter(sleeveKey, ($event.target as HTMLSelectElement).value)">
              <option v-for="option in sleeveOptions" :key="option" :value="option">{{ option }}</option>
            </select>
          </label>
          <label class="compact-filter" for="library-season">季节
            <select id="library-season" :value="seasonValue" @change="setFilter(seasonKey, ($event.target as HTMLSelectElement).value)">
              <option v-for="option in seasonOptions" :key="option" :value="option">{{ option }}</option>
            </select>
          </label>
          <label class="compact-filter" for="library-color">颜色
            <select id="library-color" :value="activeColor" @change="setFilter(colorKey, ($event.target as HTMLSelectElement).value)">
              <option v-for="option in colorOptions" :key="option" :value="option">{{ option }}</option>
            </select>
          </label>
          <details class="more-filters">
            <summary>更多筛选</summary>
            <div class="advanced-filters">
              <label v-if="subcategoryOptions.length" class="compact-filter" for="library-subcategory">二级品类
                <select id="library-subcategory" :value="filters.subcategory" @change="setFilter('subcategory', ($event.target as HTMLSelectElement).value)">
                  <option v-for="option in subcategoryOptions" :key="option" :value="option">{{ option }}</option>
                </select>
              </label>
              <div v-if="ownFactory" class="filter-row">
                <span class="filter-row-label">面料</span>
                <div class="filter-scroll" role="group" aria-label="面料">
                  <button type="button" class="chip" :aria-pressed="filters.fabric === '全部'" @click="setFilter('fabric', '全部')">全部</button>
                  <button v-for="option in taxonomy.fabrics" :key="`fabric-${option}`" type="button" class="chip" :aria-pressed="filters.fabric === option" @click="setFilter('fabric', option)">{{ option }}</button>
                </div>
              </div>
              <div v-for="[key, label, options] in extraFilters" v-show="!ownFactory || key === 'pattern'" :key="key" class="filter-row">
                <span class="filter-row-label">{{ label }}</span>
                <div class="filter-scroll" role="group" :aria-label="label">
                  <button type="button" class="chip" :aria-pressed="filters[key] === '全部'" @click="setFilter(key, '全部')">全部</button>
                  <button v-for="option in options" :key="`${key}-${option}`" type="button" class="chip" :aria-pressed="filters[key] === option" @click="setFilter(key, option)">{{ option }}</button>
                </div>
              </div>
              <div class="filter-row">
                <span class="filter-row-label">审核状态</span>
                <div class="library-status-filter" role="group" aria-label="按审核状态筛选">
                  <button v-for="option in statusOptions" :key="option.value || 'all'" type="button" :aria-pressed="reviewStatus === option.value" @click="setFilter('review_status', option.value || '全部')">{{ option.label }}</button>
                </div>
              </div>
            </div>
          </details>
        </div>
        <div class="library-tools">
          <p class="meta" role="status">{{ loading ? '正在统计…' : count > 0 && count <= 3 ? `当前结果共 ${count} 款` : `共 ${count} 款` }}</p>
          <div class="library-view-switch" role="group" aria-label="款式显示方式">
            <button type="button" :aria-pressed="viewMode === 'grid'" @click="viewMode = 'grid'">网格</button>
            <button type="button" :aria-pressed="viewMode === 'list'" @click="viewMode = 'list'">列表</button>
          </div>
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

      <div v-if="selectedChips.length" class="library-selected">
        <div class="selected-chips">
          <button v-for="chip in selectedChips" :key="chip.key" type="button" class="selected-chip" @click="setFilter(chip.key, '全部')">{{ chip.label }} <span aria-hidden="true">×</span><span class="visually-hidden">移除</span></button>
          <button id="library-clear" type="button" class="text-button" @click="clearFacets">清空筛选</button>
        </div>
      </div>
    </div>

    <div v-if="error" class="error">{{ error }}</div>
    <div v-else-if="loading && !items.length" class="library-empty" role="status">正在读取款式…</div>
    <div v-else-if="!items.length" class="library-empty">
      <p class="library-empty-title">{{ count === 0 && !q && !selectedChips.length ? '图库还没有款式' : '暂无匹配款式' }}</p>
      <p>导入服装图片、PPT、Excel 或 PDF 后，款式会显示在这里。</p>
      <RouterLink to="/import" class="review-text-link">导入新资料</RouterLink>
    </div>
    <div v-else class="library-grid" :class="{ 'is-list': viewMode === 'list', 'is-sparse': viewMode === 'grid' && items.length <= 3 }">
      <article v-for="item in items" :key="item.id" class="library-card">
        <RouterLink class="library-card-link" :to="{ name: 'record', params: { id: item.id }, query: { from: route.fullPath } }">
          <img class="library-card-image" :src="item.thumbnail_url || item.image_url" :alt="item.style_code || item.library_category || item.display_tags.品类" loading="lazy" width="315" height="357" />
          <div class="library-card-caption">
            <div v-if="viewMode !== 'list'" class="library-card-description">
              <strong>{{ item.style_code || item.library_category || item.display_tags.品类 }}</strong>
              <span class="library-card-divider" aria-hidden="true">|</span>
              <span>{{ item.library_category || item.display_tags.品类 }}</span>
            </div>
            <span v-if="viewMode === 'list'" class="visually-hidden">{{ item.review_status === 'approved' ? '已审核' : '未审核' }}</span>
            <span v-else class="library-review-badge" :class="item.review_status">{{ item.review_status === 'approved' ? '已审核' : '未审核' }}</span>
          </div>
        </RouterLink>
      </article>
    </div>
  </section>
</template>
