<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import {
  analyzeSalesAttribution, analyzeSalesSelection, exportSalesAttributionPdf,
  exportSalesSelectionPdf, exportSalesSelectionXlsx, previewSalesAttribution,
  previewSalesCustomers, previewSalesSelection, salesOptions, saveSalesDecision,
  type AttributionReport, type CustomerInsightReport, type DecisionStatus,
  type SalesOptions, type SelectionCandidate, type SelectionReport,
} from '../analyticsApi'

const props = defineProps<{
  mode: 'attribution' | 'selection' | 'customers'
  active: boolean
}>()

const emit = defineEmits<{ (e: 'update:busy', v: boolean): void }>()

const opts = ref<SalesOptions | null>(null)
const seasons = ref<string[]>([])
const category = ref('长袖套衫')
const includeBrand = ref(false)
const extSources = ref<string[]>(['网图趋势', '品牌资料包', '市调拍照'])
const catalogSeasons = ref<string[]>([])
const poolSize = ref(12)
const scanLimit = ref(128)
const targetStyleCode = ref('')
const excludeBusyPatterns = ref(true)
const selectedStyles = ref<string[]>([])
const excludedRecordIds = ref<string[]>([])
const batchNumber = ref(1)
const selectedCustomer = ref('')
const customerQuery = ref('')
const onlyAdoptedExport = ref(false)

const attrReport = ref<AttributionReport | null>(null)
const selReport = ref<SelectionReport | null>(null)
const custReport = ref<CustomerInsightReport | null>(null)

const loadingPreview = ref(false)
const runningAi = ref(false)
const exporting = ref(false)
const error = ref('')
const showDuplicates = ref(false)
const showAiRejected = ref(false)
const showAiDropped = ref(false)

const isBusy = computed(() => loadingPreview.value || runningAi.value || exporting.value)
watch(isBusy, (v) => emit('update:busy', v))

function errMsg(reason: unknown) {
  return reason instanceof Error ? reason.message : '操作暂时无法完成，请稍后再试。'
}

function pct(v: number) {
  return `${Math.round((v || 0) * 100)}%`
}

function triggerDownload(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.append(a)
  a.click()
  a.remove()
  window.setTimeout(() => URL.revokeObjectURL(url), 10_000)
}

async function ensureInit() {
  if (!props.active) return
  if (!opts.value) {
    loadingPreview.value = true
    error.value = ''
    try {
      const o = await salesOptions()
      opts.value = o
      seasons.value = o.default_seasons.length ? [...o.default_seasons] : o.seasons.slice(0, 1).map(s => s.season)
      category.value = o.default_category || '长袖套衫'
    } catch (e) {
      error.value = errMsg(e)
      loadingPreview.value = false
      return
    } finally {
      loadingPreview.value = false
    }
  }
  await refreshCurrentMode()
}

watch(() => [props.active, props.mode], async ([act]) => {
  if (act) await ensureInit()
}, { immediate: true })

function resetSelectionBatch() {
  excludedRecordIds.value = []
  batchNumber.value = 1
  showAiRejected.value = false
  showAiDropped.value = false
}

function buildSelectionPayload() {
  return {
    seasons: seasons.value,
    category: category.value,
    sources: extSources.value,
    catalog_seasons: catalogSeasons.value,
    include_brand: includeBrand.value,
    pool_size: poolSize.value,
    target_style_code: targetStyleCode.value,
    exclude_busy_patterns: excludeBusyPatterns.value,
    styles: selectedStyles.value,
    scan_limit: scanLimit.value,
    exclude_record_ids: excludedRecordIds.value,
    skip_rejected: true,
  }
}

function toggleSeason(s: string) {
  if (seasons.value.includes(s)) {
    if (seasons.value.length > 1) seasons.value = seasons.value.filter(x => x !== s)
  } else {
    seasons.value = [...seasons.value, s]
  }
  resetSelectionBatch()
}

function toggleSource(s: string) {
  if (extSources.value.includes(s)) {
    if (extSources.value.length > 1) extSources.value = extSources.value.filter(x => x !== s)
  } else {
    extSources.value = [...extSources.value, s]
  }
  resetSelectionBatch()
}

function toggleCatalogSeason(s: string) {
  if (catalogSeasons.value.includes(s)) {
    catalogSeasons.value = catalogSeasons.value.filter(x => x !== s)
  } else {
    catalogSeasons.value = [...catalogSeasons.value, s]
  }
  resetSelectionBatch()
}

function toggleStyleFilter(st: string) {
  if (selectedStyles.value.includes(st)) {
    selectedStyles.value = selectedStyles.value.filter(x => x !== st)
  } else {
    selectedStyles.value = [...selectedStyles.value, st]
  }
  resetSelectionBatch()
}

async function pickHeroStyle(code: string) {
  if (isBusy.value) return
  if (targetStyleCode.value === code && batchNumber.value === 1) return
  targetStyleCode.value = code
  resetSelectionBatch()
  await refreshCurrentMode()
}

async function refreshCurrentMode() {
  if (!seasons.value.length || isBusy.value) return
  error.value = ''
  loadingPreview.value = true
  try {
    if (props.mode === 'attribution') {
      attrReport.value = await previewSalesAttribution({
        seasons: seasons.value,
        category: category.value,
        cutoff: null,
        include_brand: includeBrand.value,
      })
    } else if (props.mode === 'selection') {
      selReport.value = await previewSalesSelection(buildSelectionPayload())
    } else if (props.mode === 'customers') {
      const res = await previewSalesCustomers({
        seasons: seasons.value,
        customer: selectedCustomer.value,
        include_brand: includeBrand.value,
      })
      custReport.value = res
      if (res.profile) selectedCustomer.value = res.profile.name
    }
  } catch (e) {
    error.value = errMsg(e)
  } finally {
    loadingPreview.value = false
  }
}

async function pickCategory(cat: string) {
  if (category.value === cat && ((props.mode === 'attribution' && attrReport.value) || (props.mode === 'selection' && selReport.value))) return
  category.value = cat
  targetStyleCode.value = ''
  resetSelectionBatch()
  await refreshCurrentMode()
}

async function pickCustomer(name: string) {
  if (isBusy.value) return
  selectedCustomer.value = name
  await refreshCurrentMode()
}

async function runAttributionDeepAi(force = false) {
  if (isBusy.value || !seasons.value.length) return
  error.value = ''
  runningAi.value = true
  try {
    attrReport.value = await analyzeSalesAttribution({
      seasons: seasons.value,
      category: category.value,
      cutoff: null,
      include_brand: includeBrand.value,
    }, force)
  } catch (e) {
    error.value = errMsg(e)
  } finally {
    runningAi.value = false
  }
}

async function downloadAttributionPdf() {
  if (isBusy.value || !attrReport.value) return
  exporting.value = true
  error.value = ''
  try {
    const blob = await exportSalesAttributionPdf({
      seasons: seasons.value,
      category: category.value,
      cutoff: null,
      include_brand: includeBrand.value,
    })
    triggerDownload(blob, `本厂爆款归因-${category.value}-${seasons.value.join('_')}.pdf`)
  } catch (e) {
    error.value = errMsg(e)
  } finally {
    exporting.value = false
  }
}

async function runSelectionDeepAi(force = false) {
  if (isBusy.value || !seasons.value.length) return
  error.value = ''
  runningAi.value = true
  try {
    selReport.value = await analyzeSalesSelection(buildSelectionPayload(), force)
  } catch (e) {
    error.value = errMsg(e)
  } finally {
    runningAi.value = false
  }
}

async function loadNextSelectionBatch() {
  if (isBusy.value || !selReport.value) return
  const hadAi = Boolean(selReport.value.ai_selection)
  const batchIds = hadAi
    ? (selReport.value.ai_selection?.scanned_record_ids || selReport.value.scan_candidate_ids || [])
    : selReport.value.shortlist.map(i => i.record_id)
  if (!batchIds.length) return
  const merged = new Set([...excludedRecordIds.value, ...batchIds])
  excludedRecordIds.value = Array.from(merged)
  batchNumber.value += 1
  if (hadAi && selReport.value.ai_status.ready) {
    await runSelectionDeepAi(false)
  } else {
    await refreshCurrentMode()
  }
}

async function resetToFirstBatch() {
  if (isBusy.value) return
  resetSelectionBatch()
  await refreshCurrentMode()
}

const activeSelectionItems = computed<SelectionCandidate[]>(() => {
  if (!selReport.value) return []
  return selReport.value.ai_selection ? selReport.value.ai_selection.recommendations : selReport.value.shortlist
})

const adoptedCount = computed(() => activeSelectionItems.value.filter(i => i.decision === 'adopted').length)

const hasNextBatch = computed(() => {
  if (!selReport.value) return false
  if (selReport.value.ai_selection) {
    return selReport.value.funnel.remaining_unscanned > 0
  }
  return selReport.value.funnel.gate2_scored_pool > selReport.value.shortlist.length
})

async function setDecision(item: SelectionCandidate, status: DecisionStatus) {
  const nextStatus: DecisionStatus = item.decision === status ? 'none' : status
  item.decision = nextStatus
  try {
    await saveSalesDecision(item.record_id, nextStatus, item.decision_note || '')
  } catch (e) {
    error.value = errMsg(e)
  }
}

async function saveNote(item: SelectionCandidate) {
  try {
    await saveSalesDecision(item.record_id, item.decision || 'none', item.decision_note || '')
  } catch (e) {
    error.value = errMsg(e)
  }
}

async function downloadSelectionPdf() {
  if (isBusy.value || !selReport.value) return
  exporting.value = true
  error.value = ''
  try {
    const blob = await exportSalesSelectionPdf(buildSelectionPayload(), onlyAdoptedExport.value)
    const prefix = onlyAdoptedExport.value ? '已采纳打版清单' : '外部图库微创新选款'
    const heroSuffix = targetStyleCode.value ? `-${targetStyleCode.value}` : ''
    triggerDownload(blob, `${prefix}-${category.value}${heroSuffix}-${seasons.value.join('_')}.pdf`)
  } catch (e) {
    error.value = errMsg(e)
  } finally {
    exporting.value = false
  }
}

async function downloadSelectionXlsx() {
  if (isBusy.value || !selReport.value) return
  exporting.value = true
  error.value = ''
  try {
    const blob = await exportSalesSelectionXlsx(buildSelectionPayload(), onlyAdoptedExport.value)
    const prefix = onlyAdoptedExport.value ? '已采纳打版清单' : '选款打版清单'
    const heroSuffix = targetStyleCode.value ? `-${targetStyleCode.value}` : ''
    triggerDownload(blob, `${prefix}-${category.value}${heroSuffix}-${seasons.value.join('_')}.xlsx`)
  } catch (e) {
    error.value = errMsg(e)
  } finally {
    exporting.value = false
  }
}

const filteredCustomers = computed(() => {
  const list = custReport.value?.customers || []
  const q = customerQuery.value.trim().toLowerCase()
  return q ? list.filter(c => c.name.toLowerCase().includes(q)) : list
})
</script>

<template>
  <section class="sales-insights">
    <!-- Top Scope Control Bar -->
    <div class="sales-control-bar">
      <div class="sales-control-row">
        <div class="sales-control-group">
          <span class="sales-control-label">本厂参考季节</span>
          <div class="sales-chip-list">
            <button
              v-for="s in opts?.seasons || []"
              :key="s.season"
              type="button"
              class="sales-chip"
              :class="{ 'is-active': seasons.includes(s.season) }"
              :disabled="isBusy"
              @click="toggleSeason(s.season)"
            >
              {{ s.season }} <small>{{ s.styles }}</small>
            </button>
          </div>
        </div>

        <div class="sales-control-right">
          <label class="sales-brand-toggle">
            <input v-model="includeBrand" type="checkbox" :disabled="isBusy" @change="refreshCurrentMode" />
            包含品牌专款
          </label>
          <span v-if="opts?.ai" class="sales-ai-badge" :class="{ 'is-ready': opts.ai.ready }">
            {{ opts.ai.ready ? `深度引擎：${opts.ai.label}` : '未配置深度 AI 密钥' }}
          </span>
          <button type="button" class="analytics-quiet" :disabled="isBusy" @click="refreshCurrentMode">
            {{ loadingPreview ? '正在加载…' : '更新统计' }}
          </button>
        </div>
      </div>

      <!-- Category quick tabs for Attribution & Selection -->
      <div v-if="mode !== 'customers'" class="sales-control-row sales-cat-row">
        <span class="sales-control-label">目标品类</span>
        <div class="sales-chip-list">
          <button
            v-for="cat in (attrReport?.category_tabs?.length ? attrReport.category_tabs.map(c => c.name) : (opts?.categories || []))"
            :key="cat"
            type="button"
            class="sales-chip"
            :class="{ 'is-active': category === cat }"
            :disabled="isBusy"
            @click="pickCategory(cat)"
          >
            {{ cat }}
          </button>
        </div>
      </div>

      <!-- Extra Selection Filters -->
      <div v-if="mode === 'selection'" class="sales-control-row sales-ext-row">
        <div class="sales-control-group">
          <span class="sales-control-label">外部候选库</span>
          <div class="sales-chip-list">
            <button
              v-for="src in opts?.external_sources || []"
              :key="src.name"
              type="button"
              class="sales-chip"
              :class="{ 'is-active': extSources.includes(src.name) }"
              :disabled="isBusy"
              @click="toggleSource(src.name); refreshCurrentMode()"
            >
              {{ src.name }} <small>{{ src.count }}</small>
            </button>
          </div>
        </div>

        <div class="sales-control-group">
          <span class="sales-control-label">图库季节标签</span>
          <div class="sales-chip-list">
            <button
              v-for="cs in opts?.catalog_seasons || []"
              :key="cs"
              type="button"
              class="sales-chip"
              :class="{ 'is-active': catalogSeasons.includes(cs) }"
              :disabled="isBusy"
              @click="toggleCatalogSeason(cs); refreshCurrentMode()"
            >
              {{ cs }}
            </button>
          </div>
          <small class="sales-inline-hint">{{ catalogSeasons.length ? '' : '默认不限' }}</small>
        </div>

        <label class="sales-pool-select">
          视觉海选扫图
          <select v-model.number="scanLimit" :disabled="isBusy" @change="resetSelectionBatch(); refreshCurrentMode()">
            <option :value="64">快速扫 64 张</option>
            <option :value="128">标准扫 128 张</option>
            <option :value="200">深度扫 200 张</option>
          </select>
        </label>

        <label class="sales-pool-select">
          1v1 精出数
          <select v-model.number="poolSize" :disabled="isBusy" @change="refreshCurrentMode">
            <option :value="8">Top 8 张</option>
            <option :value="12">Top 12 张</option>
            <option :value="16">Top 16 张</option>
            <option :value="20">Top 20 张</option>
          </select>
        </label>
      </div>

      <!-- Quality Guardrail & Style Filter Row -->
      <div v-if="mode === 'selection'" class="sales-control-row sales-guard-row">
        <label class="sales-brand-toggle sales-guard-toggle">
          <input v-model="excludeBusyPatterns" type="checkbox" :disabled="isBusy" @change="resetSelectionBatch(); refreshCurrentMode()" />
          排除大花卉 / 碎花 / 满身花纹（保高级实穿调性）
        </label>

        <div class="sales-control-group">
          <span class="sales-control-label">限定风格</span>
          <div class="sales-chip-list">
            <button
              v-for="st in opts?.style_options || []"
              :key="st"
              type="button"
              class="sales-chip"
              :class="{ 'is-active': selectedStyles.includes(st) }"
              :disabled="isBusy"
              @click="toggleStyleFilter(st); refreshCurrentMode()"
            >
              {{ st }}
            </button>
          </div>
          <small class="sales-inline-hint">{{ selectedStyles.length ? '' : '默认不限' }}</small>
        </div>
      </div>
    </div>

    <p v-if="error" class="analytics-error" role="alert">{{ error }}</p>

    <!-- ================================================================ -->
    <!-- MODULE 1: 本厂爆款归因 -->
    <!-- ================================================================ -->
    <div v-if="mode === 'attribution' && attrReport" class="sales-panel">
      <div class="sales-action-banner">
        <div>
          <h2>{{ attrReport.scope.category }} · 本厂爆款胜率对照与深度归因</h2>
          <p>
            对比高卖/高补单款与滞销无产量款，区分「多客户通吃复购款」与「大客户包场款」。支持一键导出图文 PDF 发给个人 AI 继续追问。
          </p>
        </div>
        <div class="sales-action-buttons">
          <button
            type="button"
            class="analytics-primary"
            :disabled="isBusy || !attrReport.ai_status.ready"
            @click="runAttributionDeepAi(Boolean(attrReport.ai_insight))"
          >
            {{ runningAi ? '正在看图深度归因（约15–30秒）…' : attrReport.ai_insight ? '重新 AI 深度归因' : 'AI 深度看图归因' }}
          </button>
          <button type="button" class="analytics-quiet" :disabled="isBusy" @click="downloadAttributionPdf">
            {{ exporting ? '正在生成 PDF…' : '导出图文 PDF' }}
          </button>
        </div>
      </div>

      <!-- KPI Strip -->
      <dl class="analytics-metrics sales-kpis">
        <div>
          <dt>本厂总款数 (SKU)</dt>
          <dd>{{ attrReport.overview.sku }}</dd>
          <small>有图 {{ attrReport.overview.imaged_sku }} 款 ({{ pct(attrReport.overview.image_coverage) }})</small>
        </div>
        <div>
          <dt>订货动销款数</dt>
          <dd>{{ attrReport.overview.ordered_sku }} <small>({{ pct(attrReport.overview.sell_through) }})</small></dd>
          <small>大货生产 {{ attrReport.overview.produced_sku }} 款 ({{ pct(attrReport.overview.production_ratio) }})</small>
        </div>
        <div>
          <dt>高卖爆款数</dt>
          <dd>{{ attrReport.overview.winner_sku }} <small>({{ pct(attrReport.overview.winner_rate) }})</small></dd>
          <small>通吃复购 {{ attrReport.overview.broad_winner_sku }} 款 / 大客户包场 {{ attrReport.overview.vip_winner_sku }} 款</small>
        </div>
        <div>
          <dt>累计订单与补单</dt>
          <dd>{{ attrReport.overview.total_quantity.toLocaleString() }} <small>件</small></dd>
          <small>其中补单 {{ attrReport.overview.replenishment_quantity.toLocaleString() }} 件</small>
        </div>
      </dl>

      <!-- AI Deep Attribution Result -->
      <section v-if="attrReport.ai_insight" class="sales-ai-box">
        <header class="sales-ai-head">
          <div>
            <span class="sales-kicker">旗舰视觉 + 硬数据联合复盘</span>
            <h3>AI 深度归因结论</h3>
          </div>
          <small>由 {{ attrReport.ai_insight.model }} 生成并已缓存</small>
        </header>

        <p v-if="attrReport.ai_insight.headline" class="sales-ai-headline">{{ attrReport.ai_insight.headline }}</p>

        <div class="sales-driver-grid">
          <article v-for="(d, idx) in attrReport.ai_insight.winning_drivers" :key="idx" class="sales-driver-card">
            <h4>卖好原因 {{ idx + 1 }}：{{ d.title }}</h4>
            <p><strong>数据支撑：</strong>{{ d.data_evidence }}</p>
            <p><strong>视觉与工艺共性：</strong>{{ d.visual_commonality }}</p>
            <div v-if="d.example_codes?.length" class="sales-code-tags">
              <span>代表款：</span>
              <em v-for="c in d.example_codes" :key="c">{{ c }}</em>
            </div>
          </article>
        </div>

        <div v-if="attrReport.ai_insight.losing_pitfalls?.length" class="sales-pitfall-box">
          <h4>滞销 / 无产量避坑提示</h4>
          <ul>
            <li v-for="(p, idx) in attrReport.ai_insight.losing_pitfalls" :key="idx">
              <strong>{{ p.title }}：</strong>{{ p.reason }}
              <span v-if="p.example_codes?.length" class="sales-inline-codes">（例：{{ p.example_codes.join('、') }}）</span>
            </li>
          </ul>
        </div>

        <div v-if="attrReport.ai_insight.selection_methodology?.length" class="sales-method-box">
          <h4>下轮选款与微创新方法论（已同步用于外部图库选款）</h4>
          <div class="sales-method-grid">
            <div v-for="(m, idx) in attrReport.ai_insight.selection_methodology" :key="idx" class="sales-method-item">
              <strong>{{ m.dimension }}</strong>
              <p><span>保留基因：</span>{{ m.keep_gene }}</p>
              <p><span>微创新方向：</span>{{ m.micro_innovation }}</p>
              <p class="is-warn"><span>规避：</span>{{ m.avoid }}</p>
            </div>
          </div>
        </div>
      </section>

      <div v-else class="sales-ai-placeholder">
        <p>下方已生成客观数据胜率对照表。点击右上角 <strong>「AI 深度看图归因」</strong>，模型将结合下方统计表与 Top 爆款/滞销原图总结卖好原因与微创新选款法则。</p>
      </div>

      <!-- 8-Dimension Empirical Win-Rate Lift Tables -->
      <section class="sales-section">
        <div class="analytics-section-title">
          <h3>各维度客观胜率对照表</h3>
          <p>按高卖爆款数与爆款率排序，直观识别本品类真正跑出来的材质、价位与视觉特征</p>
        </div>
        <div class="sales-dim-grid">
          <div v-for="dim in attrReport.dimensions" :key="dim.key" class="sales-dim-card">
            <header>
              <strong>{{ dim.title }}</strong>
              <small>样本 {{ dim.sample_size }} 款</small>
            </header>
            <div class="analytics-table-scroll">
              <table class="analytics-table sales-compact-table">
                <thead>
                  <tr>
                    <th>特征</th>
                    <th>款数</th>
                    <th>动销率</th>
                    <th>爆款(率)</th>
                    <th>补单率</th>
                    <th>款均件数</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="r in dim.rows.slice(0, 7)" :key="r.label">
                    <td :title="r.sample_codes.join('、')">{{ r.label }}</td>
                    <td>{{ r.sku }}</td>
                    <td>{{ pct(r.sell_through) }}</td>
                    <td :class="{ 'is-highlight': r.winner_sku > 0 }">{{ r.winner_sku }} ({{ pct(r.winner_rate) }})</td>
                    <td>{{ pct(r.replenish_rate) }}</td>
                    <td>{{ Math.round(r.avg_quantity).toLocaleString() }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </section>

      <!-- Winner vs Laggard Photo Cards -->
      <section v-if="attrReport.top_winners.length" class="sales-section">
        <div class="analytics-section-title">
          <h3>代表高卖爆款样本</h3>
          <p>自动标注「通吃复购款」（多客补单，最具复制价值）与「大客户包场款」</p>
        </div>
        <div class="sales-card-grid">
          <article v-for="w in attrReport.top_winners" :key="w.code" class="sales-style-card">
            <RouterLink v-if="w.record_id" :to="`/records/${w.record_id}`" target="_blank" class="sales-card-thumb">
              <img v-if="w.image_url" :src="w.image_url" :alt="w.code" loading="lazy" decoding="async" />
              <span v-else class="sales-no-img">{{ w.code }}<br />暂无图</span>
            </RouterLink>
            <div v-else class="sales-card-thumb">
              <span class="sales-no-img">{{ w.code }}<br />暂无图</span>
            </div>
            <div class="sales-card-body">
              <div class="sales-card-badges">
                <span class="sales-grade-pill">{{ w.grade }}级</span>
                <span v-if="w.winner_type" class="sales-type-pill" :class="{ 'is-broad': w.winner_type === '通吃复购款' }">
                  {{ w.winner_type }}
                </span>
              </div>
              <strong>{{ w.code }}</strong>
              <p class="sales-card-nums">{{ w.quantity.toLocaleString() }} 件 · 补单 {{ w.replenishment.toLocaleString() }} · {{ w.customer_count }} 家客户</p>
              <p class="sales-card-meta">{{ w.material_short }} · ¥{{ w.unit_price }}</p>
              <p v-if="w.tags?.一句话描述" class="sales-card-desc">{{ w.tags.一句话描述 }}</p>
            </div>
          </article>
        </div>
      </section>

      <section v-if="attrReport.sample_laggards.length" class="sales-section">
        <div class="analytics-section-title">
          <h3>滞销 / 无产量对照样本</h3>
          <p>供对照排查版型、元素或纱线匹配问题</p>
        </div>
        <div class="sales-card-grid is-compact">
          <article v-for="l in attrReport.sample_laggards" :key="l.code" class="sales-style-card">
            <RouterLink v-if="l.record_id" :to="`/records/${l.record_id}`" target="_blank" class="sales-card-thumb">
              <img v-if="l.image_url" :src="l.image_url" :alt="l.code" loading="lazy" decoding="async" />
              <span v-else class="sales-no-img">{{ l.code }}<br />暂无图</span>
            </RouterLink>
            <div v-else class="sales-card-thumb">
              <span class="sales-no-img">{{ l.code }}<br />暂无图</span>
            </div>
            <div class="sales-card-body">
              <span class="sales-grade-pill is-muted">{{ l.grade }}</span>
              <strong>{{ l.code }}</strong>
              <p class="sales-card-nums">订单 {{ l.quantity }} 件</p>
              <p class="sales-card-meta">{{ l.material_short }}</p>
              <p v-if="l.tags?.一句话描述" class="sales-card-desc">{{ l.tags.一句话描述 }}</p>
            </div>
          </article>
        </div>
      </section>
    </div>

    <!-- ================================================================ -->
    <!-- MODULE 2: 外部图库微创新选款 -->
    <!-- ================================================================ -->
    <div v-else-if="mode === 'selection' && selReport" class="sales-panel">
      <!-- Target Hero Style Picker ("按款号选一批") -->
      <section v-if="selReport.hero_styles?.length" class="sales-hero-picker">
        <header class="sales-hero-picker-head">
          <div>
            <strong>按本厂爆款定向挑一批</strong>
            <small>
              <template v-if="selReport.target_hero">
                当前锁定款号 <b>{{ selReport.target_hero.code }}</b>（{{ selReport.target_hero.grade }}级 · {{ selReport.target_hero.quantity.toLocaleString() }}件{{ selReport.target_hero.cat2 ? ` · ERP细分:${selReport.target_hero.cat2}` : '' }}）专项找微创新升级款
              </template>
              <template v-else>
                点选任意本厂款号，即可围绕该款版型/领型（如帽衫、翻领等）专门海选一批；也可保持「全品类爆款综合」
              </template>
            </small>
          </div>
          <button v-if="targetStyleCode" type="button" class="sales-link-btn" :disabled="isBusy" @click="pickHeroStyle('')">
            切回全品类爆款综合
          </button>
        </header>
        <div class="sales-hero-strip">
          <button
            type="button"
            class="sales-hero-card is-all"
            :class="{ 'is-selected': !targetStyleCode }"
            :disabled="isBusy"
            @click="pickHeroStyle('')"
          >
            <div class="sales-hero-all-badge">综合</div>
            <strong>全品类爆款综合</strong>
            <small>{{ selReport.funnel.own_winners_count }} 款主力爆款</small>
          </button>
          <button
            v-for="h in selReport.hero_styles"
            :key="h.code"
            type="button"
            class="sales-hero-card"
            :class="{ 'is-selected': targetStyleCode === h.code }"
            :disabled="isBusy"
            @click="pickHeroStyle(h.code)"
          >
            <div class="sales-hero-thumb">
              <img v-if="h.image_url" :src="h.image_url" :alt="h.code" loading="lazy" decoding="async" />
              <span v-else>{{ h.code }}</span>
            </div>
            <div class="sales-hero-meta">
              <strong>{{ h.code }}</strong>
              <small>{{ h.grade }}级 · {{ h.quantity.toLocaleString() }}件</small>
              <small v-if="h.cat2 || h.tags?.领型" class="sales-hero-sub">{{ h.cat2 || h.tags?.领型 }}</small>
            </div>
          </button>
        </div>
      </section>

      <div class="sales-action-banner">
        <div>
          <h2>
            {{ selReport.scope.category }}
            <template v-if="selReport.target_hero"> · 围绕 {{ selReport.target_hero.code }} 专项选款</template>
            <template v-else> · 外部图库真视觉微创新选款</template>
          </h2>
          <p>
            第 1 道自动去重（含外部图库自重）并拦截大花卉/满印 → 第 2 道用 4×4 编号网格让千问肉眼海选扫图 → 第 3 道 1v1 原图并排终审防撞款。
          </p>
        </div>
        <div class="sales-action-buttons">
          <button
            type="button"
            class="analytics-primary"
            :disabled="isBusy || !selReport.ai_status.ready || !selReport.scan_candidate_ids.length"
            @click="runSelectionDeepAi(Boolean(selReport.ai_selection))"
          >
            {{
              runningAi
                ? `正在真视觉海选 ${selReport.funnel.scan_pool_size} 张并 1v1 终审（约30–50秒）…`
                : selReport.ai_selection
                  ? '重新本批 AI 视觉海选'
                  : `AI 真视觉海选 ${selReport.funnel.scan_pool_size} 张 + 1v1 终审`
            }}
          </button>
          <button
            type="button"
            class="analytics-quiet sales-next-batch-btn"
            :disabled="isBusy || !hasNextBatch"
            @click="loadNextSelectionBatch"
          >
            {{ selReport.ai_selection ? '换下一批 AI 海选' : '换下一批预览' }}
          </button>
          <label class="sales-export-check">
            <input v-model="onlyAdoptedExport" type="checkbox" :disabled="isBusy" />
            仅导已采纳 ({{ adoptedCount }})
          </label>
          <button type="button" class="analytics-quiet" :disabled="isBusy" @click="downloadSelectionPdf">
            {{ exporting ? '导出中…' : '导出图文 PDF' }}
          </button>
          <button type="button" class="analytics-quiet" :disabled="isBusy" @click="downloadSelectionXlsx">
            导出打版 Excel
          </button>
        </div>
      </div>

      <!-- Batch & Visual Mode Status Banner -->
      <div v-if="selReport.ai_selection" class="sales-visual-banner is-verified">
        <div class="sales-visual-banner-top">
          <span class="sales-visual-pill is-verified">✓ {{ selReport.ai_selection.model }} 真视觉海选已完成</span>
          <span class="sales-batch-pill">
            第 {{ batchNumber }} 批 · 本批实扫 {{ selReport.ai_selection.visual_screened_count || selReport.ai_selection.reviewed_count }} 张（{{ selReport.ai_selection.contact_sheets_count || 0 }} 页拼图）· 池内剩余未看 {{ selReport.funnel.remaining_unscanned }} 张
          </span>
          <button v-if="batchNumber > 1 || excludedRecordIds.length" type="button" class="sales-link-btn" :disabled="isBusy" @click="resetToFirstBatch">
            回到第 1 批
          </button>
        </div>
        <p v-if="selReport.ai_selection.hero_visual_read" class="sales-hero-read">
          <strong>AI 肉眼核验本厂参照款：</strong>{{ selReport.ai_selection.hero_visual_read }}
        </p>
      </div>
      <div v-else class="sales-visual-banner is-preview">
        <div class="sales-visual-banner-top">
          <span class="sales-visual-pill is-preview">快速标签预览（尚未启动千问真视觉海选）</span>
          <span class="sales-batch-pill">
            第 {{ batchNumber }} 批 · 待海选 {{ selReport.funnel.scan_pool_size }} 张 · 池内总剩余 {{ selReport.funnel.gate2_scored_pool }} 张
          </span>
          <button v-if="batchNumber > 1 || excludedRecordIds.length" type="button" class="sales-link-btn" :disabled="isBusy" @click="resetToFirstBatch">
            回到第 1 批
          </button>
        </div>
        <p class="sales-hero-read">
          下方先按旧标签展示快速预览（旧标签可能存在个别领型或花纹误差）。点击上方 <strong>「AI 真视觉海选 {{ selReport.funnel.scan_pool_size }} 张 + 1v1 终审」</strong>，千问将直接肉眼核验本厂原图（如精准识别帽衫/翻领）并成批视觉筛图，准确率显著更高。
        </p>
      </div>

      <!-- 3-Gate Funnel Strip -->
      <div class="sales-funnel-bar">
        <div class="sales-funnel-step">
          <span>外部同品类图库</span>
          <strong>{{ selReport.funnel.external_matched_category }} 张</strong>
          <small>参照本厂 {{ selReport.funnel.own_category_sku }} 款（爆款 {{ selReport.funnel.own_winners_count }} 款）</small>
        </div>
        <div class="sales-funnel-arrow">→</div>
        <div class="sales-funnel-step">
          <span>第 1 道：去重与防花哨</span>
          <strong>拦下 {{ selReport.funnel.gate1_duplicates_removed + selReport.funnel.gate1_offtone_removed }} 张</strong>
          <small>
            本厂撞款 {{ selReport.funnel.gate1_own_duplicates_removed }} · 图库自重 {{ selReport.funnel.gate1_ext_duplicates_removed }} · 花纹拦截 {{ selReport.funnel.gate1_offtone_removed }}
          </small>
          <button
            v-if="selReport.duplicate_samples.length || selReport.offtone_samples?.length"
            type="button"
            class="sales-link-btn"
            @click="showDuplicates = !showDuplicates"
          >
            {{ showDuplicates ? '收起样本' : '查看拦截样本' }}
          </button>
        </div>
        <div class="sales-funnel-arrow">→</div>
        <div class="sales-funnel-step">
          <span>第 2 道：4×4 真视觉海选</span>
          <template v-if="selReport.ai_selection">
            <strong>实扫 {{ selReport.ai_selection.visual_screened_count || selReport.ai_selection.reviewed_count }} 张</strong>
            <small>肉眼精选 {{ selReport.ai_selection.stage1_picked_count || selReport.ai_selection.reviewed_count }} 张进入 1v1</small>
          </template>
          <template v-else>
            <strong>待扫 {{ selReport.funnel.scan_pool_size }} 张</strong>
            <small>池内有效候选共 {{ selReport.funnel.gate2_scored_pool }} 张</small>
          </template>
        </div>
        <div class="sales-funnel-arrow">→</div>
        <div class="sales-funnel-step is-final">
          <span>第 3 道：1v1 高清并排终审</span>
          <template v-if="selReport.ai_selection">
            <strong>推荐 {{ selReport.ai_selection.recommended_count }} 张</strong>
            <small>
              撞款 {{ selReport.ai_selection.rejected_similar_count }}
              <button
                v-if="selReport.ai_selection.rejected_similar.length"
                type="button"
                class="sales-link-btn"
                @click="showAiRejected = !showAiRejected"
              >
                {{ showAiRejected ? '收起' : '看' }}
              </button>
              · 调性淘汰 {{ selReport.ai_selection.dropped_count }}
              <button
                v-if="selReport.ai_selection.dropped?.length"
                type="button"
                class="sales-link-btn"
                @click="showAiDropped = !showAiDropped"
              >
                {{ showAiDropped ? '收起' : '看' }}
              </button>
            </small>
          </template>
          <template v-else>
            <strong>待执行 AI 终审</strong>
            <small>点击右上角绿色按钮启动</small>
          </template>
        </div>
      </div>

      <!-- Collapsible Gate 1 & Gate 3 rejected drawers -->
      <div v-if="showDuplicates && (selReport.duplicate_samples.length || selReport.offtone_samples?.length)" class="sales-rejected-drawer">
        <template v-if="selReport.duplicate_samples.length">
          <h4>第 1 道自动去重样本（本厂已有撞款 + 外部图库自重）</h4>
          <ul>
            <li v-for="d in selReport.duplicate_samples" :key="d.record_id">
              <strong>[{{ d.source_category }}]</strong> {{ d.summary }} — <em>{{ d.reason }}</em>
            </li>
          </ul>
        </template>
        <template v-if="selReport.offtone_samples?.length">
          <h4 style="margin-top: 10px;">花纹/满印前置拦截样本</h4>
          <ul>
            <li v-for="o in selReport.offtone_samples" :key="o.record_id">
              <strong>[{{ o.source_category }}]</strong> {{ o.summary }} — <em>{{ o.reason }}</em>
            </li>
          </ul>
        </template>
      </div>

      <div v-if="showAiRejected && selReport.ai_selection?.rejected_similar?.length" class="sales-rejected-drawer">
        <h4>第 3 道 AI 肉眼对比淘汰的「疑似撞款」（与本厂老款过于接近，不建议重开）</h4>
        <div class="sales-rejected-grid">
          <div v-for="r in selReport.ai_selection.rejected_similar" :key="r.record_id" class="sales-rejected-item">
            <img v-if="r.image_url" :src="r.image_url" alt="" loading="lazy" />
            <div>
              <strong>撞款参照：本厂 {{ r.reference_winner?.code || r.closest_any_code }}</strong>
              <p>{{ r.similarity_note || '版型与织法结构与本厂已做款高度接近' }}</p>
            </div>
          </div>
        </div>
      </div>

      <div v-if="showAiDropped && selReport.ai_selection?.dropped?.length" class="sales-rejected-drawer">
        <h4>第 3 道 AI 肉眼淘汰的「调性不符 / 花哨跑偏款」</h4>
        <div class="sales-rejected-grid">
          <div v-for="r in selReport.ai_selection.dropped" :key="r.record_id" class="sales-rejected-item">
            <img v-if="r.image_url" :src="r.image_url" alt="" loading="lazy" />
            <div>
              <strong>[{{ r.source_category }}] {{ r.tags?.一句话描述 || '已淘汰' }}</strong>
              <p>{{ r.similarity_note || '视觉风格或工艺落地性与本厂客群调性不匹配' }}</p>
            </div>
          </div>
        </div>
      </div>

      <!-- In-Library Brand Trend Radar -->
      <div v-if="selReport.trend_radar.highlights.length" class="sales-trend-strip">
        <strong>库内外部品牌高频趋势雷达：</strong>
        <span v-for="h in selReport.trend_radar.highlights" :key="h.dimension" class="sales-trend-item">
          <em>{{ h.dimension }}</em>
          {{ h.top.slice(0, 3).map(t => `${t.label}(${pct(t.share)})`).join(' / ') }}
        </span>
      </div>

      <!-- Side-by-side Candidate Cards -->
      <div v-if="activeSelectionItems.length" class="sales-pair-list">
        <article
          v-for="(item, idx) in activeSelectionItems"
          :key="item.record_id"
          class="sales-pair-card"
          :class="[`is-decision-${item.decision || 'none'}`]"
        >
          <!-- Visual Pair: Candidate vs Closest Own-Factory Winner -->
          <div class="sales-pair-photos">
            <div class="sales-photo-col">
              <span class="sales-photo-tag is-ext">外部候选 #{{ idx + 1 }} · {{ item.source_category }}</span>
              <RouterLink :to="`/records/${item.record_id}`" target="_blank" class="sales-pair-img">
                <img v-if="item.image_url" :src="item.image_url" alt="外部候选款" loading="lazy" decoding="async" />
              </RouterLink>
            </div>
            <div v-if="item.reference_winner" class="sales-photo-col is-ref">
              <span class="sales-photo-tag">本厂相近爆款 {{ item.reference_winner.code }}</span>
              <RouterLink
                v-if="item.reference_winner.record_id"
                :to="`/records/${item.reference_winner.record_id}`"
                target="_blank"
                class="sales-pair-img"
              >
                <img v-if="item.reference_winner.image_url" :src="item.reference_winner.image_url" :alt="item.reference_winner.code" loading="lazy" decoding="async" />
              </RouterLink>
              <small class="sales-ref-caption">
                {{ item.reference_winner.grade }}级 · {{ item.reference_winner.quantity.toLocaleString() }}件 / 补{{ item.reference_winner.replenishment.toLocaleString() }} · {{ item.reference_winner.material_short }}{{ item.reference_winner.cat2 ? ` · ${item.reference_winner.cat2}` : '' }}
              </small>
            </div>
          </div>

          <!-- Reasoning & Decision -->
          <div class="sales-pair-info">
            <header class="sales-pair-head">
              <div>
                <span v-if="item.tier" class="sales-tier-badge" :class="{ 'is-steady': item.tier.includes('稳妥') }">{{ item.tier }}</span>
                <span v-if="item.stars" class="sales-stars">{{ '★'.repeat(item.stars) }}</span>
                <span v-if="item.visual_screen_score" class="sales-vscreen-pill">视觉海选 {{ item.visual_screen_score }} 分</span>
                <span class="sales-score-pill">标签初评 {{ item.total_score }} 分（基因 {{ item.gene_score }} + 创新 {{ item.novelty_score }}）</span>
              </div>
              <small class="sales-source-file" :title="item.source_file">{{ item.tags?.品类 }}</small>
            </header>

            <h4 class="sales-pair-desc">{{ item.tags?.一句话描述 || '外部图库候选款式' }}</h4>

            <div v-if="item.gene_reason || item.novelty_point" class="sales-ai-reasons">
              <p><strong>为什么能卖好：</strong>{{ item.gene_reason }}</p>
              <p class="is-novelty"><strong>相比老款的微创新改动点：</strong>{{ item.novelty_point }}</p>
              <p v-if="item.yarn_and_price_advice"><strong>建议纱线与价位：</strong>{{ item.yarn_and_price_advice }}</p>
              <p v-if="item.risk_note" class="is-muted"><strong>工艺/落地提示：</strong>{{ item.risk_note }}</p>
            </div>
            <div v-else class="sales-ai-reasons is-prelim">
              <p><strong>初筛命中高胜率基因：</strong>{{ item.gene_hits.join('、') || item.matched_genes.join('、') || '实穿针织廓形' }}</p>
              <p class="is-novelty"><strong>初筛识别差异点：</strong>{{ item.diff_points.join('；') || '局部细节差异（点上方 AI 并排看图终审获取完整视觉拆解）' }}</p>
            </div>

            <footer class="sales-decision-bar">
              <div class="sales-decision-btns">
                <button
                  type="button"
                  :class="{ 'is-adopted': item.decision === 'adopted' }"
                  @click="setDecision(item, 'adopted')"
                >
                  ✓ 采纳打版
                </button>
                <button
                  type="button"
                  :class="{ 'is-maybe': item.decision === 'maybe' }"
                  @click="setDecision(item, 'maybe')"
                >
                  待定备选
                </button>
                <button
                  type="button"
                  :class="{ 'is-rejected': item.decision === 'rejected' }"
                  @click="setDecision(item, 'rejected')"
                >
                  ✕ 淘汰
                </button>
              </div>
              <input
                v-model="item.decision_note"
                type="text"
                placeholder="打版备注 / 改版想法（回车或失焦自动保存）…"
                maxlength="200"
                @change="saveNote(item)"
              />
            </footer>
          </div>
        </article>
      </div>
      <p v-else class="analytics-empty">当前筛选条件下暂无符合品类的外部图库图片。</p>
    </div>

    <!-- ================================================================ -->
    <!-- MODULE 3: 客户偏好画像与定向推款 -->
    <!-- ================================================================ -->
    <div v-else-if="mode === 'customers' && custReport" class="sales-customers-layout">
      <aside class="sales-customer-sidebar">
        <div class="sales-customer-search">
          <input v-model="customerQuery" type="search" placeholder="搜索客户名称…" aria-label="搜索客户名称" />
        </div>
        <ul class="sales-customer-list">
          <li v-for="c in filteredCustomers" :key="c.name">
            <button
              type="button"
              :class="{ 'is-active': custReport.profile?.name === c.name }"
              :disabled="isBusy"
              @click="pickCustomer(c.name)"
            >
              <div>
                <strong>{{ c.name }}</strong>
                <small>{{ c.style_count }} 款 · 补单 {{ c.replenishment.toLocaleString() }} 件</small>
              </div>
              <span>{{ c.quantity.toLocaleString() }} 件</span>
            </button>
          </li>
        </ul>
      </aside>

      <div v-if="custReport.profile" class="sales-customer-main">
        <div class="sales-profile-card">
          <header>
            <div>
              <span class="sales-kicker">客户历史采购画像</span>
              <h2>{{ custReport.profile.name }}</h2>
            </div>
            <div class="sales-profile-kpis">
              <span>累计 <strong>{{ custReport.profile.quantity.toLocaleString() }}</strong> 件</span>
              <span>下单 <strong>{{ custReport.profile.style_count }}</strong> 款</span>
              <span>补单率 <strong>{{ pct(custReport.profile.replenish_rate) }}</strong></span>
              <span>主力单价 <strong>¥{{ custReport.profile.avg_unit_price }}</strong> ({{ custReport.profile.price_band }})</span>
            </div>
          </header>

          <div class="sales-pref-tags">
            <div>
              <span>常订品类：</span>
              <em v-for="x in custReport.profile.favorite_categories" :key="x.name">{{ x.name }} ({{ x.quantity }}件)</em>
            </div>
            <div>
              <span>偏爱纱线：</span>
              <em v-for="x in custReport.profile.favorite_materials" :key="x.full_name">{{ x.name }} ({{ x.quantity }}件)</em>
            </div>
            <div>
              <span>主力色系：</span>
              <em v-for="x in custReport.profile.favorite_colors" :key="x.name">{{ x.name }}</em>
            </div>
            <div v-if="Object.keys(custReport.profile.favorite_tags).length">
              <span>视觉偏好：</span>
              <template v-for="(vals, k) in custReport.profile.favorite_tags" :key="k">
                <em v-for="v in vals.slice(0, 2)" :key="`${k}-${v}`">{{ v }}</em>
              </template>
            </div>
          </div>
        </div>

        <!-- Immediate Push: Unbought Own-Factory Winners -->
        <section class="sales-section">
          <div class="analytics-section-title">
            <h3>立即可推：本厂高匹配未购爆款（零打版成本）</h3>
            <p>该客户尚未下过单，但在全厂已验证热销且高度契合其品类、纱线与单价偏好</p>
          </div>
          <div v-if="custReport.own_recommendations.length" class="sales-card-grid">
            <article v-for="r in custReport.own_recommendations" :key="r.code" class="sales-style-card">
              <RouterLink v-if="r.record_id" :to="`/records/${r.record_id}`" target="_blank" class="sales-card-thumb">
                <img v-if="r.image_url" :src="r.image_url" :alt="r.code" loading="lazy" decoding="async" />
                <span v-else class="sales-no-img">{{ r.code }}</span>
              </RouterLink>
              <div v-else class="sales-card-thumb"><span class="sales-no-img">{{ r.code }}</span></div>
              <div class="sales-card-body">
                <div class="sales-card-badges">
                  <span class="sales-grade-pill">{{ r.grade }}级</span>
                  <span class="sales-type-pill is-broad">匹配 {{ r.match_score }}%</span>
                </div>
                <strong>{{ r.code }}</strong>
                <p class="sales-card-nums">全厂已订 {{ r.quantity.toLocaleString() }} 件 · 补 {{ r.replenishment.toLocaleString() }}</p>
                <p class="sales-card-meta">{{ r.material_short }} · ¥{{ r.unit_price }}</p>
                <p class="sales-push-reasons">{{ (r.push_reasons || []).join(' · ') }}</p>
              </div>
            </article>
          </div>
          <p v-else class="analytics-empty">该客户已覆盖当前季节主要匹配爆款。</p>
        </section>

        <!-- External Library Proposals for VIP Customer -->
        <section v-if="custReport.external_recommendations.length" class="sales-section">
          <div class="analytics-section-title">
            <h3>定向开发提案：外部图库契合新款</h3>
            <p>按该客户最常买的品类与版型/领型/色彩偏好从外部三库匹配，适合定向打样提案</p>
          </div>
          <div class="sales-card-grid">
            <article v-for="ex in custReport.external_recommendations" :key="ex.record_id" class="sales-style-card">
              <RouterLink :to="`/records/${ex.record_id}`" target="_blank" class="sales-card-thumb">
                <img v-if="ex.image_url" :src="ex.image_url" alt="" loading="lazy" decoding="async" />
              </RouterLink>
              <div class="sales-card-body">
                <div class="sales-card-badges">
                  <span class="sales-grade-pill is-muted">{{ ex.source_category }}</span>
                  <span class="sales-type-pill">契合 {{ ex.match_score }}%</span>
                </div>
                <p class="sales-card-desc">{{ ex.summary }}</p>
                <p class="sales-push-reasons">命中：{{ ex.matched_preferences.join(' · ') }}</p>
              </div>
            </article>
          </div>
        </section>
      </div>
      <p v-else class="analytics-empty">所选季节暂无客户订单明细。</p>
    </div>

    <p v-else-if="loadingPreview" class="analytics-empty" role="status">正在汇总销售端洞察数据…</p>
  </section>
</template>
