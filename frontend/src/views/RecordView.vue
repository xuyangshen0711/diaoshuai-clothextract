<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  assignSourceCategory,
  getLibraryMeta,
  getRecord,
  type CatalogItem,
  type LibraryMeta,
} from '../api'
import DesignNotesList from '../components/DesignNotesList.vue'
import RecordHistory from '../components/RecordHistory.vue'
import SalesAnalysis from '../components/SalesAnalysis.vue'
import CoverEditor from '../components/CoverEditor.vue'
import {
  formatTime,
  importTime,
  sourceType,
  swatchColor,
  visibleDescription,
} from '../utils/source'
const route = useRoute()
const router = useRouter()
const item = ref<CatalogItem | null>(null)
const loading = ref(false)
const error = ref('')
const categoryMessage = ref('')
const libraryMeta = ref<LibraryMeta>({
  source_categories: ['本厂产品', '网图趋势', '品牌资料包', '市调拍照'],
  unclassified: '未分类',
  factory: {},
})
const labels = { pending: '待审核', approved: '已审核', rejected: '已驳回' }
const back = computed(() => {
  const path = typeof route.query.from === 'string' ? route.query.from : '/'
  return path.startsWith('/') && !path.startsWith('//') && !path.startsWith('/records/')
    ? path
    : '/'
})
const tags = computed(() => item.value?.display_tags)
const ownFactory = computed(() => item.value?.source_category === '本厂产品')
const categoryChoices = computed(() => [
  ...libraryMeta.value.source_categories,
  libraryMeta.value.unclassified,
])
const display = (value: unknown) =>
  Array.isArray(value) ? value.join('、') || '无' : String(value || '未提供')
const unique = (values: (string | undefined)[]) => {
  const seen: string[] = []
  for (const value of values) if (value && !seen.includes(value)) seen.push(value)
  return seen
}
const seasons = computed(() =>
  unique([
    ...(item.value?.variants || []).map((row) => row.season),
    ...(item.value?.seasons || []),
  ]),
)
const needles = computed(() => unique((item.value?.variants || []).map((row) => row.needle)))
const materials = computed(() => unique((item.value?.variants || []).map((row) => row.material)))
const seriesNames = computed(() => unique((item.value?.variants || []).map((row) => row.series)))
const designers = computed(() => unique((item.value?.variants || []).map((row) => row.designer)))
const craftsmen = computed(() => unique((item.value?.variants || []).map((row) => row.craftsman)))
const designCodes = computed(() =>
  unique((item.value?.variants || []).map((row) => row.design_code)),
)
const subcategories = computed(() =>
  unique((item.value?.variants || []).map((row) => row.subcategory)),
)
const orderQuery = ref('')
const orderKind = ref('全部')
const visibleOrders = computed(() => {
  const rows = item.value?.sales?.orders || []
  const needle = orderQuery.value.trim()
  return rows.filter((row) => {
    if (orderKind.value !== '全部' && row.order_kind !== orderKind.value) return false
    if (!needle) return true
    return [
      row.customer,
      row.order_no,
      row.salesperson,
      row.variant_code,
      row.color,
      row.size_label,
    ].some((value) => String(value || '').includes(needle))
  })
})
function money(value?: number) {
  return (value || 0).toLocaleString('zh-CN')
}
function unitText(value?: number) {
  if (!value) return '—'
  return value.toLocaleString('zh-CN', { maximumFractionDigits: 2 })
}
const sizeLabels = computed(() => {
  const labels: string[] = []
  for (const variant of item.value?.variants || []) {
    for (const size of variant.sizes) if (!labels.includes(size.label)) labels.push(size.label)
  }
  return labels
})
const tabs = [
  { key: 'sales', label: '销售分析' },
  { key: 'versions', label: '客户版本' },
  { key: 'orders', label: '订单明细' },
  { key: 'archive', label: '款式档案' },
] as const
const activeTab = computed(() =>
  tabs.some((tab) => tab.key === route.query.tab) ? String(route.query.tab) : 'sales',
)
function selectTab(tab: string) {
  void router.replace({ query: { ...route.query, tab } })
}
function tabKeydown(event: KeyboardEvent, index: number) {
  let next = index
  if (event.key === 'ArrowRight') next = (index + 1) % tabs.length
  else if (event.key === 'ArrowLeft') next = (index + tabs.length - 1) % tabs.length
  else if (event.key === 'Home') next = 0
  else if (event.key === 'End') next = tabs.length - 1
  else return
  event.preventDefault()
  selectTab(tabs[next]!.key)
  document.getElementById(`record-tab-${tabs[next]!.key}`)?.focus()
}
const imageDialog = ref<HTMLDialogElement | null>(null)
const expandedVariant = ref('')
const orderPage = ref(1)
const orderPageSize = 30
const orderPages = computed(() =>
  Math.max(1, Math.ceil(visibleOrders.value.length / orderPageSize)),
)
const pagedOrders = computed(() =>
  visibleOrders.value.slice((orderPage.value - 1) * orderPageSize, orderPage.value * orderPageSize),
)
watch([orderQuery, orderKind], () => {
  orderPage.value = 1
})
const productName = computed(
  () => subcategories.value[0] || item.value?.library_category || tags.value?.品类 || '款式详情',
)
const summaryColors = computed(() => {
  const result = new Map<string, string>()
  for (const variant of item.value?.variants || [])
    for (const color of variant.colors) result.set(color.name, color.family)
  return [...result].map(([name, family]) => ({ name, family }))
})
let latest = 0
watch(
  () => route.params.id,
  async (id) => {
    const seq = ++latest
    loading.value = true
    error.value = ''
    item.value = null
    categoryMessage.value = ''
    orderQuery.value = ''
    orderKind.value = '全部'
    orderPage.value = 1
    expandedVariant.value = ''
    try {
      const [result, meta] = await Promise.all([getRecord(String(id)), getLibraryMeta()])
      if (seq !== latest) return
      item.value = result
      libraryMeta.value = meta
    } catch (e) {
      if (seq === latest) error.value = e instanceof Error ? e.message : String(e)
    } finally {
      if (seq === latest) loading.value = false
    }
  },
  { immediate: true },
)

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
  <main v-if="item && tags" class="record-page record-redesign">
    <div class="record-breadcrumb">
      <RouterLink :to="back">← 返回图库</RouterLink><span>/</span
      ><span>{{ item.source_category || '未分类' }}</span
      ><span>/</span><span>{{ item.style_code || productName }}</span>
    </div>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <section class="product-summary" aria-label="款式概览">
      <div class="product-preview">
        <button
          class="product-image"
          type="button"
          aria-label="放大查看款式原图"
          @click="imageDialog?.showModal()"
        >
          <img
            :src="item.image_url"
            :alt="item.style_code || productName"
            width="600"
            height="800"
          /><span class="image-expand" aria-hidden="true">⤢</span>
        </button>
        <div class="preview-caption">
          <span>款式原图</span
          ><button type="button" @click="imageDialog?.showModal()">点击查看完整图片</button>
        </div>
        <p v-if="item.has_pending_image" class="preview-notice">
          有新图片待审核，通过后替换当前原图。
        </p>
        <p
          v-if="
            item.image_width &&
            item.image_height &&
            Math.max(item.image_width, item.image_height) < 600
          "
          class="preview-notice"
        >
          原图分辨率较低，放大可能模糊。
        </p>
      </div>
      <div class="product-overview">
        <div class="product-heading">
          <h1>
            <span v-if="item.style_code" translate="no"
              >{{ item.style_code }}<span class="title-separator"> · </span></span
            >{{ productName }}
          </h1>
          <span class="review-badge" :class="item.review_status">{{
            labels[item.review_status]
          }}</span>
        </div>
        <p class="product-subtitle">
          {{
            [
              ownFactory ? seasons.join(' / ') : tags.季节,
              item.fabric,
              item.sleeve_length,
              item.source_category,
            ]
              .filter(Boolean)
              .join(' · ')
          }}
        </p>
        <dl v-if="ownFactory && item.sales" class="product-metrics" aria-label="累计销售指标">
          <div>
            <dt>累计件数</dt>
            <dd>{{ money(item.sales.total_quantity) }}<small> 件</small></dd>
          </div>
          <div>
            <dt>累计金额</dt>
            <dd><small>¥</small>{{ money(item.sales.total_amount) }}</dd>
          </div>
          <div>
            <dt>下单客户</dt>
            <dd>{{ item.sales.customer_count ?? '—' }}<small> 家</small></dd>
          </div>
          <div>
            <dt>补单</dt>
            <dd>{{ item.sales.replenishment_count ?? '—' }}<small> 单</small></dd>
          </div>
        </dl>
        <p v-else-if="ownFactory" class="product-no-sales">
          这款还没有销量数据，导入销量表后可查看销售分析。
        </p>
        <p v-else class="product-description">
          {{ visibleDescription(tags.一句话描述, item.review_status) || '暂无款式描述' }}
        </p>
        <dl v-if="ownFactory" class="product-specs">
          <div v-if="summaryColors.length" class="summary-colors">
            <dt>颜色</dt>
            <dd>
              <span v-for="color in summaryColors" :key="color.name"
                ><i :style="{ background: swatchColor(color.family) }" aria-hidden="true"></i
                >{{ color.name }}</span
              >
            </dd>
          </div>
          <div v-if="materials.length">
            <dt>成分</dt>
            <dd>{{ materials.join('、') }}</dd>
          </div>
          <div v-if="sizeLabels.length">
            <dt>尺码</dt>
            <dd>{{ sizeLabels.join('、') }}</dd>
          </div>
        </dl>
        <div v-if="item.sales?.has_order_lines" class="product-order-meta">
          <span
            >下单区间 <time>{{ item.sales.first_order_date || '—' }}</time> —
            <time>{{ item.sales.last_order_date || '—' }}</time></span
          ><span>{{ item.sales.order_count }} 单</span
          ><span>均价 ¥{{ unitText(item.sales.average_price) }}/件</span
          ><span>补单 {{ money(item.sales.replenishment_quantity) }} 件</span>
        </div>
        <div class="product-actions">
          <CoverEditor :item="item" @updated="item = $event" />
          <RouterLink :to="{ name: 'search', query: { record: item.id } }">找相似款</RouterLink
          ><RouterLink
            :to="{ path: '/review', query: { status: item.review_status, selected: item.id } }"
            >{{ item.review_status === 'pending' ? '审核此款' : '查看审核结果' }}</RouterLink
          ><a :href="item.download_url || item.image_url" download>下载原图</a>
        </div>
      </div>
    </section>

    <template v-if="ownFactory">
      <div class="record-section-bar">
        <div class="record-tabs" role="tablist" aria-label="款式详情板块">
          <button
            v-for="(tab, index) in tabs"
            :id="`record-tab-${tab.key}`"
            :key="tab.key"
            type="button"
            role="tab"
            :aria-selected="activeTab === tab.key"
            :aria-controls="`record-panel-${tab.key}`"
            :tabindex="activeTab === tab.key ? 0 : -1"
            @click="selectTab(tab.key)"
            @keydown="tabKeydown($event, index)"
          >
            {{ tab.label
            }}<span v-if="tab.key === 'versions'">{{ item.variants?.length || 0 }}</span
            ><span v-if="tab.key === 'orders'">{{ item.sales?.orders?.length || 0 }}</span>
          </button>
        </div>
        <span v-if="activeTab === 'sales'" class="analysis-scope">累计销售 · 按件数分析</span>
      </div>
      <section
        v-show="activeTab === 'sales'"
        id="record-panel-sales"
        class="record-tab-panel"
        role="tabpanel"
        aria-labelledby="record-tab-sales"
        tabindex="0"
      >
        <h2 class="visually-hidden">销售分析</h2>
        <SalesAnalysis v-if="item.sales" :sales="item.sales" @orders="selectTab('orders')" />
        <div v-else class="record-empty">
          <h2>还没有这款的销量数据</h2>
          <p>上传销量表，查看月度走势、颜色占比和客户排名。</p>
          <RouterLink class="btn" to="/import">前往导入</RouterLink>
        </div>
      </section>
      <section
        v-show="activeTab === 'versions'"
        id="record-panel-versions"
        class="record-tab-panel record-data-panel"
        role="tabpanel"
        aria-labelledby="record-tab-versions"
        tabindex="0"
      >
        <div class="panel-heading">
          <h2>客户版本</h2>
          <span>{{ item.variants?.length || 0 }} 个版本 · 展开查看款号及档案信息</span>
        </div>
        <p v-if="!item.variants?.length" class="record-empty">
          款式档案里还没有这个款号，可在导入页上传款式档案。
        </p>
        <div v-else class="record-table-scroll" tabindex="0" aria-label="客户版本表，可横向滚动">
          <table class="archive-table variants-table">
            <caption class="visually-hidden">
              该款式的客户版本及累计销量
            </caption>
            <thead>
              <tr>
                <th scope="col">客户 / 版本</th>
                <th scope="col">大货款号</th>
                <th scope="col">颜色</th>
                <th scope="col">尺码</th>
                <th scope="col" class="numeric">件数</th>
                <th scope="col" class="numeric">金额（元）</th>
                <th scope="col">最近下单</th>
                <th scope="col"><span class="visually-hidden">版本详情</span></th>
              </tr>
            </thead>
            <tbody>
              <template v-for="(variant, index) in item.variants" :key="variant.variant_code"
                ><tr>
                  <th scope="row">
                    {{ variant.customer || '未提供客户'
                    }}<small v-if="(variant.buyer_count || 0) > 1"
                      >分给 {{ variant.buyer_count }} 家客户</small
                    >
                  </th>
                  <td translate="no">{{ variant.variant_code }}</td>
                  <td class="variant-colors">
                    <span v-if="!variant.colors.length">—</span
                    ><span v-for="color in variant.colors" :key="color.name"
                      ><i :style="{ background: swatchColor(color.family) }" aria-hidden="true"></i
                      >{{ color.name }}</span
                    >
                  </td>
                  <td>{{ variant.size_span || '—' }}</td>
                  <td class="numeric">{{ money(variant.quantity) }}</td>
                  <td class="numeric">{{ money(variant.amount) }}</td>
                  <td>{{ variant.last_order_date || '—' }}</td>
                  <td>
                    <button
                      type="button"
                      class="archive-text-button"
                      :aria-expanded="expandedVariant === variant.variant_code"
                      :aria-controls="`variant-details-${index}`"
                      :aria-label="`${expandedVariant === variant.variant_code ? '收起' : '展开'}${variant.customer || variant.variant_code}详情`"
                      @click="
                        expandedVariant =
                          expandedVariant === variant.variant_code ? '' : variant.variant_code
                      "
                    >
                      {{ expandedVariant === variant.variant_code ? '收起' : '详情' }}
                    </button>
                  </td>
                </tr>
                <tr
                  v-if="expandedVariant === variant.variant_code"
                  :id="`variant-details-${index}`"
                  class="expanded-variant"
                >
                  <td colspan="8">
                    <dl>
                      <div>
                        <dt>设计款号</dt>
                        <dd>{{ variant.design_code || '—' }}</dd>
                      </div>
                      <div>
                        <dt>客户款号</dt>
                        <dd>{{ variant.customer_style_code || '—' }}</dd>
                      </div>
                      <div>
                        <dt>季节</dt>
                        <dd>{{ variant.season || '—' }}</dd>
                      </div>
                      <div>
                        <dt>订单数</dt>
                        <dd>{{ variant.order_count ?? '—' }}</dd>
                      </div>
                      <div>
                        <dt>单价</dt>
                        <dd>¥{{ unitText(variant.unit_price) }}</dd>
                      </div>
                      <div>
                        <dt>成分</dt>
                        <dd>{{ variant.material || '—' }}</dd>
                      </div>
                      <div>
                        <dt>设计师</dt>
                        <dd>{{ variant.designer || '—' }}</dd>
                      </div>
                      <div>
                        <dt>工艺师</dt>
                        <dd>{{ variant.craftsman || '—' }}</dd>
                      </div>
                    </dl>
                  </td>
                </tr></template
              >
            </tbody>
          </table>
        </div>
      </section>
      <section
        v-show="activeTab === 'orders'"
        id="record-panel-orders"
        class="record-tab-panel record-data-panel"
        role="tabpanel"
        aria-labelledby="record-tab-orders"
        tabindex="0"
      >
        <div class="panel-heading">
          <h2>订单明细</h2>
          <span>共 {{ visibleOrders.length }} 行</span>
        </div>
        <div class="archive-order-tools">
          <label for="order-filter"
            >搜索订单<input
              id="order-filter"
              v-model="orderQuery"
              type="search"
              name="order-search"
              placeholder="客户、订单号、业务员…" /></label
          ><label for="order-kind"
            >订单分类<select id="order-kind" v-model="orderKind">
              <option value="全部">全部</option>
              <option value="订单">订单</option>
              <option value="补单">补单</option>
            </select></label
          >
        </div>
        <div
          v-if="visibleOrders.length"
          class="record-table-scroll"
          tabindex="0"
          aria-label="订单明细表，可横向滚动"
        >
          <table class="archive-table">
            <caption class="visually-hidden">
              订单明细，第
              {{
                orderPage
              }}
              页
            </caption>
            <thead>
              <tr>
                <th scope="col">订单号</th>
                <th scope="col">分类</th>
                <th scope="col">客户</th>
                <th scope="col">大货款号</th>
                <th scope="col">客户款号</th>
                <th scope="col">颜色</th>
                <th scope="col">尺码</th>
                <th scope="col" class="numeric">件数</th>
                <th scope="col" class="numeric">金额（元）</th>
                <th scope="col">日期</th>
                <th scope="col">业务员</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="(row, index) in pagedOrders"
                :key="`${row.order_no}-${row.variant_code}-${index}`"
              >
                <td>{{ row.order_no }}</td>
                <td>
                  <span :class="{ 'replenish-tag': row.order_kind === '补单' }">{{
                    row.order_kind || '—'
                  }}</span>
                </td>
                <td>{{ row.customer || '—' }}</td>
                <td translate="no">{{ row.variant_code }}</td>
                <td translate="no">{{ row.customer_style_code || '—' }}</td>
                <td>{{ row.color }}</td>
                <td>{{ row.size_label }}</td>
                <td class="numeric">{{ money(row.quantity) }}</td>
                <td class="numeric">{{ money(row.amount) }}</td>
                <td>{{ row.order_date || '—' }}</td>
                <td>{{ row.salesperson || '—' }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <p v-else class="record-empty">
          {{
            item.sales?.orders?.length
              ? '没有符合筛选的订单，试试其他关键词或分类。'
              : '暂无订单明细，可在导入页上传销量表。'
          }}
        </p>
        <div v-if="visibleOrders.length" class="order-pagination">
          <span>第 {{ orderPage }} / {{ orderPages }} 页 · 每页 {{ orderPageSize }} 行</span>
          <div>
            <button type="button" :disabled="orderPage <= 1" @click="orderPage--">上一页</button
            ><button type="button" :disabled="orderPage >= orderPages" @click="orderPage++">
              下一页
            </button>
          </div>
        </div>
      </section>
    </template>
    <section
      v-show="!ownFactory || activeTab === 'archive'"
      id="record-panel-archive"
      class="record-tab-panel record-archive-panel"
      :role="ownFactory ? 'tabpanel' : undefined"
      :aria-labelledby="ownFactory ? 'record-tab-archive' : undefined"
      tabindex="0"
    >
      <details v-if="ownFactory" class="detail-fold archive-facts" open>
        <summary>款式档案</summary>
        <dl class="detail-facts">
          <div>
            <dt>档案品类</dt>
            <dd>{{ item.archive_category || '—' }}</dd>
          </div>
          <div>
            <dt>二级品类</dt>
            <dd>{{ subcategories.join('、') || '—' }}</dd>
          </div>
          <div>
            <dt>针型</dt>
            <dd>{{ needles.join('、') || '—' }}</dd>
          </div>
          <div>
            <dt>系列</dt>
            <dd>{{ seriesNames.join('、') || '—' }}</dd>
          </div>
          <div>
            <dt>设计师</dt>
            <dd>{{ designers.join('、') || '—' }}</dd>
          </div>
          <div>
            <dt>工艺师</dt>
            <dd>{{ craftsmen.join('、') || '—' }}</dd>
          </div>
          <div>
            <dt>设计款号</dt>
            <dd>{{ designCodes.join('、') || '—' }}</dd>
          </div>
        </dl>
        <p v-if="item.library_category" class="meta">
          图库按固定品类显示为「{{ item.library_category }}」，原始档案品类保留，AI 分类仅作参考。
        </p>
      </details>
      <details class="detail-fold" open>
        <summary>来源与操作</summary>
        <p v-if="item.review_status === 'pending'" class="meta">
          当前分类已在主页展示，尚待人工审核确认。搜索时可选择是否包含未审核图片。
        </p>
        <p v-else-if="item.review_status === 'rejected'" class="meta">
          此图片已驳回，不在主页或搜索结果中展示。
        </p>
        <dl class="record-source">
          <div>
            <dt>资料大类</dt>
            <dd>
              <select :value="item.source_category" aria-label="资料大类" @change="changeCategory">
                <option v-for="choice in categoryChoices" :key="choice" :value="choice">
                  {{ choice }}
                </option>
              </select>
              <span v-if="categoryMessage" class="meta">{{ categoryMessage }}</span>
            </dd>
          </div>
          <div>
            <dt>资料类型</dt>
            <dd>{{ sourceType(item.source) }}</dd>
          </div>
          <div>
            <dt>导入时间</dt>
            <dd>
              <time :datetime="importTime(item.source)">{{
                formatTime(importTime(item.source))
              }}</time>
            </dd>
          </div>
          <div>
            <dt>来源资料</dt>
            <dd>{{ item.source.source_file }}</dd>
          </div>
          <div>
            <dt>所在位置</dt>
            <dd>{{ item.source.location }}</dd>
          </div>
          <div v-if="item.image_width && item.image_height">
            <dt>尺寸</dt>
            <dd>{{ item.image_width }} × {{ item.image_height }} 像素</dd>
          </div>
          <div v-if="item.reviewer">
            <dt>上次审核</dt>
            <dd>
              {{ item.reviewer }} ·
              {{
                item.reviewed_at
                  ? new Date(item.reviewed_at).toLocaleString('zh-CN', { hour12: false })
                  : ''
              }}
            </dd>
          </div>
          <div v-if="item.review_note">
            <dt>审核意见</dt>
            <dd>{{ item.review_note }}</dd>
          </div>
        </dl>
        <div class="toolbar">
          <a class="btn" :href="item.download_url || item.image_url" download>下载原图</a>
          <a v-if="item.source_url" class="btn" :href="item.source_url" download>下载来源资料</a>
          <RouterLink class="btn" :to="{ name: 'search', query: { record: item.id } }"
            >找相似款</RouterLink
          >
          <RouterLink
            class="btn"
            :to="{ path: '/review', query: { status: item.review_status, selected: item.id } }"
            >{{ item.review_status === 'pending' ? '前往审核此款' : '查看审核结果' }}</RouterLink
          >
        </div>
      </details>
      <details v-if="item.image_history?.length" class="detail-fold">
        <summary>历史原图</summary>
        <div class="image-history">
          <a
            v-for="(old, index) in item.image_history"
            :key="index"
            :href="old.url"
            target="_blank"
            rel="noopener"
          >
            <img :src="old.url" :alt="old.note || '替换前保留的原图'" width="80" height="106" />
          </a>
        </div>
      </details>
      <details class="detail-fold">
        <summary>分类标签对照</summary>
        <div class="record-tag-table">
          <table>
            <caption class="visually-hidden">
              分类标签对照
            </caption>
            <thead>
              <tr>
                <th scope="col">标签</th>
                <th scope="col">AI 建议{{ item.ai_tags_source === 'mock' ? '（模拟）' : '' }}</th>
                <th scope="col">
                  {{ item.review_status === 'approved' ? '最终分类' : '待审核分类' }}
                </th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="(value, key) in tags"
                :key="key"
                :class="{ changed: item.ai_tags && display(item.ai_tags[key]) !== display(value) }"
              >
                <th scope="row">{{ key }}</th>
                <td>
                  {{
                    key === '一句话描述'
                      ? visibleDescription(display(item.ai_tags?.[key]), item.review_status) ||
                        '未提供'
                      : display(item.ai_tags?.[key])
                  }}
                </td>
                <td>
                  {{
                    key === '一句话描述'
                      ? visibleDescription(display(value), item.review_status) || '未提供'
                      : display(value)
                  }}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
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
    <dialog
      ref="imageDialog"
      class="record-image-dialog"
      aria-label="款式原图预览"
      @click="$event.target === imageDialog && imageDialog?.close()"
    >
      <div class="image-dialog-bar">
        <span>{{ item.style_code || productName }} · 原图</span
        ><button type="button" aria-label="关闭原图预览" @click="imageDialog?.close()">×</button>
      </div>
      <img :src="item.image_url" :alt="item.style_code || productName" width="600" height="800" />
    </dialog>
  </main>
  <main v-else class="record-page record-redesign">
    <div class="record-breadcrumb">
      <RouterLink :to="back">← 返回图库</RouterLink><span>/ 款式详情</span>
    </div>
    <p v-if="loading" class="empty" role="status">正在读取款式…</p>
    <p v-if="error" class="error" role="alert">
      {{ error }} <RouterLink to="/">返回图库</RouterLink>
    </p>
  </main>
</template>
<style src="../record-detail.css"></style>
