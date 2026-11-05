<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { assignSourceCategory, getLibraryMeta, getRecord, type CatalogItem, type LibraryMeta } from '../api'
import DesignNotesList from '../components/DesignNotesList.vue'
import RecordHistory from '../components/RecordHistory.vue'
import { formatTime, importTime, sourceType, swatchColor, swatchLight, visibleDescription } from '../utils/source'
const route = useRoute()
const item = ref<CatalogItem | null>(null)
const loading = ref(false)
const error = ref('')
const categoryMessage = ref('')
const libraryMeta = ref<LibraryMeta>({ source_categories: ['本厂产品', '网图趋势', '品牌资料包', '市调拍照'], unclassified: '未分类', factory: {} })
const labels = { pending: '待审核', approved: '已审核', rejected: '已驳回' }
const back = computed(() => {
  const path = typeof route.query.from === 'string' ? route.query.from : '/'
  return path.startsWith('/') && !path.startsWith('//') && !path.startsWith('/records/') ? path : '/'
})
const tags = computed(() => item.value?.display_tags)
const ownFactory = computed(() => item.value?.source_category === '本厂产品')
const categoryChoices = computed(() => [...libraryMeta.value.source_categories, libraryMeta.value.unclassified])
const display = (value: unknown) => Array.isArray(value) ? value.join('、') || '无' : String(value || '未提供')
const pieces = (value: number) => `${value.toLocaleString('zh-CN')} 件`
const unique = (values: (string | undefined)[]) => {
  const seen: string[] = []
  for (const value of values) if (value && !seen.includes(value)) seen.push(value)
  return seen
}
const seasons = computed(() => unique([
  ...(item.value?.variants || []).map(row => row.season),
  ...(item.value?.seasons || []),
]))
const needles = computed(() => unique((item.value?.variants || []).map(row => row.needle)))
const materials = computed(() => unique((item.value?.variants || []).map(row => row.material)))
const seriesNames = computed(() => unique((item.value?.variants || []).map(row => row.series)))
const designers = computed(() => unique((item.value?.variants || []).map(row => row.designer)))
const craftsmen = computed(() => unique((item.value?.variants || []).map(row => row.craftsman)))
const designCodes = computed(() => unique((item.value?.variants || []).map(row => row.design_code)))
const subcategories = computed(() => unique((item.value?.variants || []).map(row => row.subcategory)))
const orderQuery = ref('')
const orderKind = ref('全部')
const visibleOrders = computed(() => {
  const rows = item.value?.sales?.orders || []
  const needle = orderQuery.value.trim()
  return rows.filter(row => {
    if (orderKind.value !== '全部' && row.order_kind !== orderKind.value) return false
    if (!needle) return true
    return [row.customer, row.order_no, row.salesperson, row.variant_code, row.color, row.size_label]
      .some(value => String(value || '').includes(needle))
  })
})
const heatMax = computed(() => Math.max(0, ...(item.value?.sales?.heatmap?.rows || []).flatMap(row => row.quantities)))
const peopleMax = computed(() => Math.max(0, ...(item.value?.sales?.salespeople || []).map(row => row.quantity)))
function money(value?: number) { return (value || 0).toLocaleString('zh-CN') }
function unitText(value?: number) {
  if (!value) return '—'
  return value.toLocaleString('zh-CN', { maximumFractionDigits: 2 })
}
function heatStyle(value: number) {
  if (!value || !heatMax.value) return {}
  const alpha = 0.14 + 0.7 * (value / heatMax.value)
  return { background: `rgba(168, 139, 94, ${alpha.toFixed(3)})` }
}
function monthShare(row: { order_quantity: number; replenishment_quantity: number }, part: 'order' | 'replenish') {
  const rows = item.value?.sales?.monthly || []
  const max = Math.max(...rows.map(item => item.order_quantity + item.replenishment_quantity), 0)
  if (!max) return '0%'
  const value = part === 'order' ? row.order_quantity : row.replenishment_quantity
  return `${Math.round(value / max * 100)}%`
}
const colorNames = computed(() => {
  const names: string[] = []
  for (const variant of item.value?.variants || []) {
    for (const color of variant.colors) if (!names.includes(color.name)) names.push(color.name)
  }
  return names
})
const sizeLabels = computed(() => {
  const labels: string[] = []
  for (const variant of item.value?.variants || []) {
    for (const size of variant.sizes) if (!labels.includes(size.label)) labels.push(size.label)
  }
  return labels
})
function barWidth(quantity: number, rows: { quantity: number }[]) {
  const max = Math.max(...rows.map(row => row.quantity), 0)
  if (!max) return '0%'
  return `${Math.round(quantity / max * 100)}%`
}
let latest = 0
watch(() => route.params.id, async id => {
  const seq = ++latest
  loading.value = true; error.value = ''; item.value = null; categoryMessage.value = ''
  orderQuery.value = ''; orderKind.value = '全部'
  try {
    const [result, meta] = await Promise.all([getRecord(String(id)), getLibraryMeta()])
    if (seq !== latest) return
    item.value = result
    libraryMeta.value = meta
  } catch (e) { if (seq === latest) error.value = e instanceof Error ? e.message : String(e) }
  finally { if (seq === latest) loading.value = false }
}, { immediate: true })

async function changeCategory(event: Event) {
  if (!item.value) return
  const next = (event.target as HTMLSelectElement).value
  categoryMessage.value = ''
  error.value = ''
  try {
    await assignSourceCategory([item.value.id], next)
    item.value = await getRecord(item.value.id)
    categoryMessage.value = `资料大类已改为${next}`
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
    ;(event.target as HTMLSelectElement).value = item.value.source_category
  }
}
</script>
<template>
  <main class="record-page" v-if="item && tags">
    <div class="page-head"><RouterLink class="btn" :to="back">返回列表</RouterLink>
      <p class="detail-crumb">{{ item.source_category || '未分类' }}<template v-if="item.library_category"> / {{ item.library_category }}</template><template v-if="item.style_code"> / {{ item.style_code }}</template></p>
    </div>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <div class="record-layout detail-hero">
      <section class="record-image-panel" aria-label="款式原图">
        <a :href="item.image_url" target="_blank" rel="noopener"><img :src="item.image_url" :alt="item.style_code || tags.一句话描述 || item.library_category || tags.品类" width="600" height="800" /></a>
        <dl v-if="ownFactory" class="detail-caption">
          <div v-if="item.style_code"><dt>款号</dt><dd>{{ item.style_code }}</dd></div>
          <div v-if="colorNames.length"><dt>颜色</dt><dd>{{ colorNames.join('、') }}</dd></div>
          <div v-if="materials.length"><dt>成分</dt><dd>{{ materials.join('、') }}</dd></div>
        </dl>
        <p v-if="item.has_pending_image" class="meta">有一张新图片待审核。通过后才会替换当前原图。</p>
        <p v-if="item.image_width && item.image_height && Math.max(item.image_width, item.image_height) < 600" class="meta">来源图片分辨率较低，放大可能模糊。建议上传更高清的原始资料。</p>
      </section>
      <section class="record-information" aria-label="款式信息">
        <div class="detail-heading">
          <h2>{{ item.style_code || item.library_category || tags.品类 }}</h2>
          <span class="task-state" :class="item.review_status">{{ labels[item.review_status] }}</span>
        </div>
        <div class="detail-pills">
          <span v-if="item.library_category || tags.品类" class="detail-pill strong">{{ (item.library_category || tags.品类).replace('>', ' · ') }}</span>
          <span v-for="name in subcategories" :key="name" class="detail-pill">{{ name }}</span>
          <span v-if="ownFactory && item.sleeve_length" class="detail-pill">{{ item.sleeve_length }}</span>
          <span v-if="ownFactory && item.fabric" class="detail-pill">{{ item.fabric }}</span>
          <span v-for="season in (ownFactory ? seasons : [tags.季节].filter(Boolean))" :key="season" class="detail-pill">{{ season }}</span>
        </div>
        <dl v-if="ownFactory" class="detail-facts">
          <div><dt>档案品类</dt><dd>{{ [item.archive_category, ...subcategories].filter(Boolean).join(' / ') || '—' }}</dd></div>
          <div><dt>针型</dt><dd>{{ needles.join('、') || '—' }}</dd></div>
          <div><dt>原料</dt><dd>{{ materials.join('、') || '—' }}</dd></div>
          <div v-if="seriesNames.length"><dt>系列</dt><dd>{{ seriesNames.join('、') }}</dd></div>
          <div><dt>二级品类</dt><dd>{{ subcategories.join('、') || '—' }}</dd></div>
          <div><dt>设计师</dt><dd>{{ designers.join('、') || '—' }}</dd></div>
          <div><dt>工艺师</dt><dd>{{ craftsmen.join('、') || '—' }}</dd></div>
          <div><dt>设计款号</dt><dd>{{ designCodes.join(' · ') || '—' }}</dd></div>
          <div><dt>客户版本</dt><dd>{{ item.variants?.length || 0 }} 个</dd></div>
        </dl>
        <p v-else class="record-description">{{ visibleDescription(tags.一句话描述, item.review_status) || '暂无款式描述' }}</p>
        <dl v-if="ownFactory && item.sales?.has_order_lines" class="metric-row" aria-label="累计订单">
          <div><dt>累计订单</dt><dd><strong>{{ money(item.sales.total_quantity) }}</strong> 件 · <strong>{{ money(item.sales.total_amount) }}</strong> 元</dd></div>
          <div><dt>订单 · 客户</dt><dd>{{ item.sales.order_count }} 单 / {{ item.sales.customer_count }} 家</dd></div>
          <div><dt>平均单价</dt><dd>{{ unitText(item.sales.average_price) }} 元/件</dd></div>
          <div><dt>补单</dt><dd>{{ item.sales.replenishment_count }} 张 · {{ money(item.sales.replenishment_quantity) }} 件</dd></div>
          <div><dt>下单区间</dt><dd>{{ item.sales.first_order_date || '—' }} — {{ item.sales.last_order_date || '—' }}</dd></div>
        </dl>
        <div v-else-if="ownFactory && item.sales" class="sales-banner" aria-label="累计销售">
          <span>累计销售</span>
          <strong>{{ item.sales.total_quantity.toLocaleString('zh-CN') }}</strong><span>件</span>
          <span class="sales-dot" aria-hidden="true">·</span>
          <strong>{{ item.sales.total_amount.toLocaleString('zh-CN') }}</strong><span>元</span>
        </div>
        <p v-else-if="ownFactory" class="meta">这张本厂产品图还没有销量数据。</p>

        <section v-if="ownFactory" class="detail-block" aria-label="客户版本">
          <div class="detail-block-head"><h3>客户版本</h3><span class="meta">{{ item.variants?.length || 0 }} 个版本</span></div>
          <p v-if="!item.variants?.length" class="meta">款式档案里还没有这个款号。</p>
          <div v-else class="detail-table-wrap">
            <table class="detail-table">
              <thead><tr><th scope="col">客户</th><th scope="col">大货款号</th><th scope="col">设计款号</th><th scope="col">客户款号</th><th scope="col">季节</th><th scope="col">颜色</th><th scope="col">尺码</th><th scope="col">订单</th><th scope="col">单价</th><th scope="col">件数</th><th scope="col">金额（元）</th><th scope="col">最近下单</th></tr></thead>
              <tbody>
                <tr v-for="variant in item.variants" :key="variant.variant_code">
                  <td>{{ variant.customer }}<small v-if="(variant.buyer_count || 0) > 1" class="card-quiet">分给 {{ variant.buyer_count }} 家客户</small></td>
                  <td>{{ variant.variant_code }}</td>
                  <td>{{ variant.design_code || '—' }}</td>
                  <td>{{ variant.customer_style_code || '—' }}</td>
                  <td>{{ variant.season || '—' }}</td>
                  <td>
                    <span v-if="!variant.colors.length">—</span>
                    <span v-for="color in variant.colors" :key="color.name" class="color-name"><span class="card-dot" :class="{ light: swatchLight(color.family) }" :style="{ background: swatchColor(color.family) }"></span>{{ color.name }}</span>
                  </td>
                  <td>{{ variant.size_span || '—' }}</td>
                  <td>{{ variant.order_count || '—' }}</td>
                  <td>{{ unitText(variant.unit_price) }}</td>
                  <td>{{ variant.quantity.toLocaleString('zh-CN') }}</td>
                  <td>{{ variant.amount.toLocaleString('zh-CN') }}</td>
                  <td>{{ variant.last_order_date || '—' }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <p v-if="sizeLabels.length" class="meta">尺码 {{ sizeLabels.join('、') }}</p>
        </section>

        <div v-if="ownFactory && item.sales?.has_order_lines" class="detail-split">
          <section aria-label="月度下单走势">
            <div class="detail-block-head"><h3>月度下单走势</h3><span class="meta">补单为金色</span></div>
            <div v-for="row in item.sales.monthly" :key="row.month" class="month-row">
              <span>{{ row.month }}</span>
              <span class="month-track" :aria-label="`${row.month} 订单 ${row.order_quantity} 件，补单 ${row.replenishment_quantity} 件`">
                <span class="month-order" :style="{ width: monthShare(row, 'order') }"></span>
                <span class="month-replenish" :style="{ width: monthShare(row, 'replenish') }"></span>
              </span>
              <span class="bar-num">{{ (row.order_quantity + row.replenishment_quantity).toLocaleString('zh-CN') }}</span>
            </div>
          </section>
          <section v-if="item.sales.heatmap" aria-label="颜色和尺码">
            <div class="detail-block-head"><h3>颜色 × 尺码</h3><span class="meta">共 {{ pieces(item.sales.total_quantity) }}</span></div>
            <div class="detail-table-wrap">
              <table class="heat-table">
                <thead><tr><th scope="col">颜色</th><th v-for="size in item.sales.heatmap.sizes" :key="size" scope="col">{{ size }}</th><th scope="col">合计</th></tr></thead>
                <tbody>
                  <tr v-for="row in item.sales.heatmap.rows" :key="row.color">
                    <th scope="row">{{ row.color }}</th>
                    <td v-for="(value, index) in row.quantities" :key="`${row.color}-${index}`" :style="heatStyle(value)">{{ value || '' }}</td>
                    <td>{{ row.total.toLocaleString('zh-CN') }}</td>
                  </tr>
                </tbody>
                <tfoot><tr><th scope="row">合计</th><td v-for="(value, index) in item.sales.heatmap.totals" :key="`total-${index}`">{{ value.toLocaleString('zh-CN') }}</td><td>{{ item.sales.heatmap.total.toLocaleString('zh-CN') }}</td></tr></tfoot>
              </table>
            </div>
          </section>
        </div>
        <div v-if="ownFactory && item.sales?.has_order_lines" class="detail-split">
          <section aria-label="下单客户">
            <div class="detail-block-head"><h3>下单客户</h3><span class="meta">前 {{ item.sales.top_customers?.length || 0 }} 家</span></div>
            <div class="detail-table-wrap">
              <table class="detail-table">
                <thead><tr><th scope="col">客户</th><th scope="col">大货款号</th><th scope="col">业务员</th><th scope="col">订单</th><th scope="col">件数</th><th scope="col">金额（元）</th><th scope="col">最近下单</th></tr></thead>
                <tbody>
                  <tr v-for="row in item.sales.top_customers" :key="row.customer">
                    <td>{{ row.customer }}</td><td>{{ row.variant_code }}</td><td>{{ row.salesperson || '—' }}</td>
                    <td>{{ row.order_count }}</td><td>{{ row.quantity.toLocaleString('zh-CN') }}</td>
                    <td>{{ row.amount.toLocaleString('zh-CN') }}</td><td>{{ row.last_order_date || '—' }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </section>
          <section aria-label="业务员">
            <div class="detail-block-head"><h3>业务员</h3><span class="meta">按件数</span></div>
            <div v-for="row in item.sales.salespeople" :key="row.name" class="person-row">
              <span>{{ row.name }}</span>
              <span class="bar-track"><span class="person-fill" :style="{ width: peopleMax ? `${Math.round(row.quantity / peopleMax * 100)}%` : '0%' }"></span></span>
              <span class="bar-num">{{ row.quantity.toLocaleString('zh-CN') }}</span>
            </div>
          </section>
        </div>
        <div v-if="ownFactory && item.sales" class="detail-bars">
          <section aria-label="按颜色">
            <div class="detail-block-head"><h3>按颜色</h3><span class="meta">共 {{ pieces(item.sales.total_quantity) }}</span></div>
            <div v-for="row in item.sales.by_color" :key="row.name" class="bar-row">
              <span class="card-dot" :class="{ light: swatchLight(row.family || row.name) }" :style="{ background: swatchColor(row.family || row.name) }"></span>
              <span class="bar-name">{{ row.name }}</span>
              <span class="bar-track"><span class="bar-fill" :style="{ width: barWidth(row.quantity, item.sales.by_color), background: swatchColor(row.family || row.name) }"></span></span>
              <span class="bar-num">{{ row.quantity.toLocaleString('zh-CN') }}</span>
            </div>
          </section>
          <section aria-label="按尺码">
            <div class="detail-block-head"><h3>按尺码</h3><span class="meta">共 {{ pieces(item.sales.total_quantity) }}</span></div>
            <div v-for="row in item.sales.by_size" :key="row.name" class="bar-row">
              <span class="bar-name size-name">{{ row.name }}</span>
              <span class="bar-track"><span class="bar-fill size-fill" :style="{ width: barWidth(row.quantity, item.sales.by_size) }"></span></span>
              <span class="bar-num">{{ row.quantity.toLocaleString('zh-CN') }}</span>
            </div>
          </section>
        </div>
        <p v-if="ownFactory && item.library_category" class="meta">品类按固定品类表显示为「{{ item.library_category }}」<template v-if="item.sleeve_length">，袖长 {{ item.sleeve_length }}</template><template v-if="item.fabric">，面料 {{ item.fabric }}</template>。<template v-if="item.archive_category">档案一级品类「{{ item.archive_category }}」仍保留。</template>下方 AI 标签仅作参考。</p>

        <details v-if="ownFactory && item.sales?.orders?.length" class="detail-fold">
          <summary>订单明细 {{ item.sales.orders.length }} 行</summary>
          <div class="order-tools">
            <label>分类
              <select v-model="orderKind" aria-label="订单分类">
                <option value="全部">全部</option>
                <option value="订单">订单</option>
                <option value="补单">补单</option>
              </select>
            </label>
            <label class="visually-hidden" for="order-filter">筛选订单明细</label>
            <input id="order-filter" v-model="orderQuery" type="search" placeholder="客户、订单号、业务员" />
          </div>
          <div class="detail-table-wrap">
            <table class="detail-table">
              <thead><tr><th scope="col">订单号</th><th scope="col">分类</th><th scope="col">客户</th><th scope="col">大货款号</th><th scope="col">客户款号</th><th scope="col">颜色</th><th scope="col">尺码</th><th scope="col">件数</th><th scope="col">金额</th><th scope="col">日期</th><th scope="col">业务员</th></tr></thead>
              <tbody>
                <tr v-for="(row, index) in visibleOrders" :key="`${row.order_no}-${row.variant_code}-${row.color}-${row.size_label}-${index}`">
                  <td>{{ row.order_no }}</td><td>{{ row.order_kind || '—' }}</td><td>{{ row.customer || '—' }}</td>
                  <td>{{ row.variant_code }}</td><td>{{ row.customer_style_code || '—' }}</td><td>{{ row.color }}</td>
                  <td>{{ row.size_label }}</td><td>{{ row.quantity.toLocaleString('zh-CN') }}</td>
                  <td>{{ row.amount.toLocaleString('zh-CN') }}</td><td>{{ row.order_date || '—' }}</td><td>{{ row.salesperson || '—' }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <p v-if="!visibleOrders.length" class="meta">没有符合筛选的订单。</p>
        </details>
        <details class="detail-fold">
          <summary>来源与操作</summary>
          <p v-if="item.review_status === 'pending'" class="meta">当前分类已在主页展示，尚待人工审核确认。搜索时可选择是否包含未审核图片。</p>
          <p v-else-if="item.review_status === 'rejected'" class="meta">此图片已驳回，不在主页或搜索结果中展示。</p>
          <dl class="record-source">
            <div><dt>资料大类</dt><dd>
              <select :value="item.source_category" aria-label="资料大类" @change="changeCategory">
                <option v-for="choice in categoryChoices" :key="choice" :value="choice">{{ choice }}</option>
              </select>
              <span v-if="categoryMessage" class="meta">{{ categoryMessage }}</span>
            </dd></div>
            <div><dt>资料类型</dt><dd>{{ sourceType(item.source) }}</dd></div>
            <div><dt>导入时间</dt><dd><time :datetime="importTime(item.source)">{{ formatTime(importTime(item.source)) }}</time></dd></div>
            <div><dt>来源资料</dt><dd>{{ item.source.source_file }}</dd></div>
            <div><dt>所在位置</dt><dd>{{ item.source.location }}</dd></div>
            <div v-if="item.image_width && item.image_height"><dt>尺寸</dt><dd>{{ item.image_width }} × {{ item.image_height }} 像素</dd></div>
            <div v-if="item.reviewer"><dt>上次审核</dt><dd>{{ item.reviewer }} · {{ item.reviewed_at ? new Date(item.reviewed_at).toLocaleString('zh-CN', { hour12: false }) : '' }}</dd></div>
            <div v-if="item.review_note"><dt>审核意见</dt><dd>{{ item.review_note }}</dd></div>
          </dl>
          <div class="toolbar">
            <a class="btn" :href="item.download_url || item.image_url" download>下载原图</a>
            <a v-if="item.source_url" class="btn" :href="item.source_url" download>下载来源资料</a>
            <RouterLink class="btn" :to="{ name: 'search', query: { record: item.id } }">找相似款</RouterLink>
            <RouterLink class="btn" :to="{ path: '/review', query: { status: item.review_status, selected: item.id } }">{{ item.review_status === 'pending' ? '前往审核此款' : '查看审核结果' }}</RouterLink>
          </div>
        </details>
        <details v-if="item.image_history?.length" class="detail-fold">
          <summary>历史原图</summary>
          <div class="image-history">
            <a v-for="(old, index) in item.image_history" :key="index" :href="old.url" target="_blank" rel="noopener">
              <img :src="old.url" :alt="old.note || '替换前保留的原图'" width="80" height="106" />
            </a>
          </div>
        </details>
        <details class="detail-fold">
          <summary>分类标签对照</summary>
          <div class="record-tag-table"><table><caption class="visually-hidden">分类标签对照</caption><thead><tr><th scope="col">标签</th><th scope="col">AI 建议{{ item.ai_tags_source === 'mock' ? '（模拟）' : '' }}</th><th scope="col">{{ item.review_status === 'approved' ? '最终分类' : '待审核分类' }}</th></tr></thead><tbody>
            <tr v-for="(value, key) in tags" :key="key" :class="{ changed: item.ai_tags && display(item.ai_tags[key]) !== display(value) }"><th scope="row">{{ key }}</th><td>{{ key === '一句话描述' ? (visibleDescription(display(item.ai_tags?.[key]), item.review_status) || '未提供') : display(item.ai_tags?.[key]) }}</td><td>{{ key === '一句话描述' ? (visibleDescription(display(value), item.review_status) || '未提供') : display(value) }}</td></tr>
          </tbody></table></div>
        </details>
        <details class="detail-fold">
          <summary>设计说明</summary>
          <DesignNotesList :notes="item.notes" />
        </details>
        <details class="detail-fold">
          <summary>审核记录</summary>
          <RecordHistory :id="item.id" />
        </details>
      </section>
    </div>
  </main>
  <main v-else class="record-page">
    <div class="page-head"><RouterLink class="btn" :to="back">返回列表</RouterLink><span class="meta">款式详情</span></div>
    <p v-if="loading" class="empty" role="status">正在读取款式…</p>
    <p v-if="error" class="error" role="alert">{{ error }} <RouterLink to="/">返回图库</RouterLink></p>
  </main>
</template>
