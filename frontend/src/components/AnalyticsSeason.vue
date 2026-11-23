<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import {
  exportSeason, previewSeason, seasonOptions,
  type SeasonExportScope, type SeasonFilters, type SeasonGrade, type SeasonGroupRow, type SeasonOptions, type SeasonReport,
} from '../analyticsApi'

const props = defineProps<{ active: boolean }>()
const emit = defineEmits<{ 'update:busy': [value: boolean] }>()

const GRADES: SeasonGrade[] = ['S', 'A', 'B', 'C', 'D', '无产量']
const intFmt = new Intl.NumberFormat('zh-CN')
const pct = (v: number | null | undefined) => v == null ? '—' : `${(v * 100).toFixed(1)}%`
const money = (v: number) => `¥${intFmt.format(Math.round(v))}`

const options = ref<SeasonOptions | null>(null)
const seasons = ref<string[]>([])
const cutoff = ref('')
const compare = ref('')
const includeBrand = ref(false)
const report = ref<SeasonReport | null>(null)
const submitted = ref('')
const loading = ref(false)
const preparing = ref(false)
const downloadingScope = ref<SeasonExportScope | ''>('')
const error = ref('')
const notice = ref('')
const wallMode = ref<'category' | 'material'>('category')
const wallIndex = ref(0)
const expandedUnproduced = ref<Record<string, boolean>>({})

const filters = computed<SeasonFilters>(() => ({
  seasons: [...seasons.value].sort(),
  cutoff: cutoff.value || null,
  compare: compare.value || null,
  include_brand: includeBrand.value,
}))
const signature = computed(() => JSON.stringify(filters.value))
const changed = computed(() => Boolean(report.value) && submitted.value !== signature.value)
const downloading = computed(() => Boolean(downloadingScope.value))
const busy = computed(() => loading.value || preparing.value || downloading.value)
watch(busy, value => emit('update:busy', value))

const walls = computed(() => report.value ? (wallMode.value === 'category' ? report.value.walls.category : report.value.walls.material) : [])
const wall = computed(() => walls.value[Math.min(wallIndex.value, Math.max(0, walls.value.length - 1))])
const wallBlocks = computed(() => {
  const current = wall.value
  if (!current) return []
  return 'rows' in current
    ? current.rows
    : [{ material: '', sku: current.headline.sku, grades: current.grades }]
})
watch(wallMode, () => { wallIndex.value = 0; expandedUnproduced.value = {} })
watch(wallIndex, () => { expandedUnproduced.value = {} })

function blockKey(material: string) {
  return `${wallMode.value}:${wall.value?.name || ''}:${material}`
}

function toggleUnproduced(key: string) {
  expandedUnproduced.value = { ...expandedUnproduced.value, [key]: !expandedUnproduced.value[key] }
}

const sourceHints = computed(() => {
  const s = options.value?.sources
  if (!s) return []
  const hints: string[] = []
  if (!s.production_rows) hints.push('还没有导入生产数据：请到「导入资料 → 本厂产品 → 生产数据」上传。')
  if (!s.guide_rows) hints.push('还没有导入款式指示书：请到「导入资料 → 本厂产品 → 款式指示书」上传。')
  if (!s.stock_rows) hints.push('还没有备单明细：用「订单数据」入口重新导入一次最新订单即可自动识别备单。')
  return hints
})

async function loadOptions() {
  if (options.value || loading.value) return
  loading.value = true; error.value = ''
  try {
    options.value = await seasonOptions()
    seasons.value = [...options.value.default_seasons]
  } catch (e) { error.value = e instanceof Error ? e.message : String(e) }
  finally { loading.value = false }
}
watch(() => props.active, value => { if (value) void loadOptions() }, { immediate: true })

function toggle(season: string) {
  seasons.value = seasons.value.includes(season) ? seasons.value.filter(s => s !== season) : [...seasons.value, season]
}

async function generate() {
  if (!seasons.value.length) { error.value = '请至少选择一个季节。'; return }
  preparing.value = true; error.value = ''; notice.value = ''
  try {
    report.value = await previewSeason(filters.value)
    wallIndex.value = 0
    expandedUnproduced.value = {}
    submitted.value = signature.value
    if (!cutoff.value && report.value.scope.cutoff) cutoff.value = report.value.scope.cutoff
    if (!compare.value && report.value.scope.compare) compare.value = report.value.scope.compare
    submitted.value = signature.value
  } catch (e) { error.value = e instanceof Error ? e.message : String(e) }
  finally { preparing.value = false }
}

const EXPORT_META: Record<SeasonExportScope, { prefix: string; label: string; sheets: string }> = {
  tracking: { prefix: '日常订补跟踪', label: '日常跟踪（3表）', sheets: '3 张日常跟踪表（市场款号订补情况、单款销售周期、订单与备单的对比）+ 口径说明' },
  review: { prefix: '季度款式复盘', label: '季度复盘（6表）', sheets: '6 张季度复盘表（按品类/按材质等级表、材质与品类对比、纱线颜色、设计师工艺师）+ 口径说明' },
  all: { prefix: '季度款式分析', label: '完整合集（9表）', sheets: '全部 9 张表 + 口径说明' },
}

async function download(scope: SeasonExportScope) {
  if (!report.value || changed.value) return
  downloadingScope.value = scope; error.value = ''; notice.value = ''
  try {
    const { blob, images } = await exportSeason(filters.value, report.value.snapshot, scope)
    const meta = EXPORT_META[scope]
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `${meta.prefix}-${report.value.scope.seasons.join('_')}-${report.value.scope.cutoff || ''}.xlsx`
    document.body.appendChild(link); link.click(); link.remove()
    setTimeout(() => URL.revokeObjectURL(url), 2000)
    notice.value = `已导出 ${meta.label} Excel（${meta.sheets}，嵌入 ${images} 张高清款式图）。`
  } catch (e) { error.value = e instanceof Error ? e.message : String(e) }
  finally { downloadingScope.value = '' }
}

function groupCells(row: SeasonGroupRow) {
  return [row.sku, row.ordered, pct(row.sell_through), row.produced, pct(row.production_ratio), row.skc,
    pct(row.sku_share), intFmt.format(row.quantity), pct(row.quantity_share), money(row.amount), pct(row.amount_share)]
}
</script>

<template>
  <div class="analytics-layout season-layout">
    <form class="analytics-controls" @submit.prevent="generate">
      <div class="analytics-section-title"><h2>季度款式分析</h2></div>
      <fieldset :disabled="preparing || loading">
        <legend>商品资料季节 <small>可多选</small></legend>
        <div v-if="options?.seasons.length" class="yarn-season-chips" role="group" aria-label="商品资料季节">
          <button v-for="item in options.seasons" :key="item.season" type="button" class="yarn-chip"
            :class="{ 'is-active': seasons.includes(item.season) }" :aria-pressed="seasons.includes(item.season)"
            @click="toggle(item.season)">{{ item.season }} <small>{{ item.styles }}款</small></button>
        </div>
        <p v-else-if="options" class="analytics-note">还没有商品资料，请先在导入资料里上传商品资料表。</p>
      </fieldset>
      <fieldset :disabled="preparing || loading">
        <legend>统计口径</legend>
        <label class="season-checkbox">
          <input v-model="includeBrand" type="checkbox" />
          <span>包含品牌专款</span>
        </label>
        <p class="analytics-note">默认只统计市场款，排除带品牌后缀的款号（如 662513风笛、615502佐色）。</p>
      </fieldset>
      <fieldset :disabled="preparing || loading">
        <legend>统计时间 <small>可选</small></legend>
        <label class="analytics-field">统计截止日<input v-model="cutoff" type="date" aria-label="统计截止日" /></label>
        <label class="analytics-field">对比日（订补表「增长 / 新增」）<input v-model="compare" type="date" aria-label="对比日" /></label>
        <p class="analytics-note">留空时截止日取最新订单日期，对比日取截止日前 10 天。<template v-if="options?.last_order_date">当前最新订单：{{ options.last_order_date }}</template></p>
      </fieldset>
      <button type="submit" class="analytics-primary" :disabled="preparing || loading || !options">{{ preparing ? '正在计算…' : '生成季度款式分析' }}</button>
      <div class="season-rules analytics-note">
        <p><strong>等级</strong>：S ≥ 8000 件；A 3000–7999；B 1000–2999；C 300–999；D 0–299；没进生产数据为「无产量」。</p>
        <p><strong>不计销量</strong>：备单、1 件小备单、无金额发样。</p>
      </div>
    </form>

    <div class="analytics-workspace">
      <ul v-if="sourceHints.length" class="season-hints" aria-label="数据准备提示"><li v-for="hint in sourceHints" :key="hint">{{ hint }}</li></ul>
      <div v-if="report" class="analytics-export season-export-bar">
        <p v-if="changed">筛选条件已调整，请重新点击「生成季度款式分析」后再导出。</p>
        <div v-else class="season-export-desc">
          <p><strong>日常跟踪（3表）</strong>：市场款号订补情况、单款销售周期、订单与备单的对比（高频使用、导出快）。</p>
          <p><strong>季度复盘（6表）</strong>：按品类/按材质等级表（带高清图）、材质与品类对比、纱线颜色、设计师工艺师。</p>
        </div>
        <div class="season-export-actions">
          <button type="button" class="analytics-primary" :disabled="downloading || preparing || changed" @click="download('tracking')">{{ downloadingScope === 'tracking' ? '正在导出日常跟踪…' : '导出日常跟踪（3表）' }}</button>
          <button type="button" class="season-btn-secondary" :disabled="downloading || preparing || changed" @click="download('review')">{{ downloadingScope === 'review' ? '正在导出季度复盘…' : '导出季度复盘（6表）' }}</button>
          <button type="button" class="season-btn-secondary" :disabled="downloading || preparing || changed" @click="download('all')">{{ downloadingScope === 'all' ? '正在导出完整合集…' : '导出完整合集（9表）' }}</button>
        </div>
      </div>
      <p v-if="error" class="analytics-error" role="alert">{{ error }}</p>
      <p v-if="notice" class="analytics-note" role="status">{{ notice }}</p>
      <p v-if="!report && !preparing" class="analytics-empty">选择季节后点击「生成季度款式分析」。</p>

      <article v-if="report" class="analytics-report season-report" aria-label="季度款式分析预览">
        <header class="analytics-report-head">
          <div><h2>季度款式分析</h2><p>{{ report.scope.seasons.join('、') }} · 截止 {{ report.scope.cutoff || '—' }}</p></div>
          <span v-if="report.scope.include_brand">市场款 {{ report.overview.market_sku }} + 品牌专款 {{ report.overview.brand_sku }}</span>
          <span v-else>仅市场款 {{ report.overview.market_sku }}（已排除 {{ report.overview.excluded_brand_sku }} 款品牌专款）</span>
        </header>

        <section v-if="report.checks.length" class="season-checks" aria-label="数据检查">
          <h3>数据检查 <small>只提醒，不影响生成</small></h3>
          <ul class="yarn-missing-list">
            <li v-for="check in report.checks" :key="check.title" :class="'is-' + (check.level === 'info' ? 'medium' : check.level)">
              <strong>{{ check.title }}<template v-if="check.count">（{{ check.count }} 款）</template></strong>
              <p>{{ check.detail }}</p>
              <details v-if="check.codes.length"><summary>查看款号</summary><div class="yarn-code-list"><code v-for="code in check.codes" :key="code">{{ code }}</code></div></details>
            </li>
          </ul>
        </section>

        <section class="season-kpis" aria-label="总览">
          <div><span>SKU</span><strong>{{ report.overview.sku }}</strong></div>
          <div><span>动销率</span><strong>{{ pct(report.overview.sell_through) }}</strong><small>{{ report.overview.ordered }}/{{ report.overview.sku }}</small></div>
          <div><span>生产比</span><strong>{{ pct(report.overview.production_ratio) }}</strong><small>{{ report.overview.produced }}/{{ report.overview.sku }}</small></div>
          <div><span>无效率</span><strong>{{ pct(report.overview.invalid_ratio) }}</strong><small>{{ report.overview.unproduced }} 款无产量</small></div>
          <div><span>订单件数</span><strong>{{ intFmt.format(report.overview.quantity) }}</strong><small>{{ money(report.overview.amount) }}</small></div>
          <div><span>SKC</span><strong>{{ report.overview.skc }}</strong></div>
        </section>
        <div class="season-grade-bar" aria-label="等级分布">
          <span v-for="g in GRADES" :key="g" :class="'grade-' + (g === '无产量' ? 'none' : g)">{{ g }} <strong>{{ report.overview.grade_counts[g] }}</strong></span>
        </div>

        <section class="yarn-section">
          <h3>按材质的生产、销售对比</h3>
          <div class="analytics-table-scroll"><table class="analytics-table">
            <thead><tr><th>原料</th><th>SKU</th><th>订货SKU</th><th>动销率</th><th>生产数</th><th>生产比</th><th>SKC</th><th>SKU占比</th><th>订单件数</th><th>订单占比</th><th>订单金额</th><th>金额占比</th></tr></thead>
            <tbody>
              <tr v-for="row in report.materials.rows" :key="row.name"><th>{{ row.name }}</th><td v-for="(cell, i) in groupCells(row)" :key="i">{{ cell }}</td></tr>
              <tr class="season-total"><th>总计</th><td v-for="(cell, i) in groupCells(report.materials.totals)" :key="i">{{ cell }}</td></tr>
            </tbody>
          </table></div>
        </section>

        <section class="yarn-section">
          <h3>按品类的生产、销售对比</h3>
          <div class="analytics-table-scroll"><table class="analytics-table">
            <thead><tr><th>品类</th><th>SKU</th><th>订货SKU</th><th>动销率</th><th>生产数</th><th>生产比</th><th>客单价</th><th>SKU占比</th><th>销售数量</th><th>数量占比</th><th>订单金额</th><th>金额占比</th></tr></thead>
            <tbody>
              <tr v-for="row in [...report.categories.rows, report.categories.totals]" :key="row.name" :class="{ 'season-total': row.name === '总计' }">
                <th>{{ row.name }}</th><td>{{ row.sku }}</td><td>{{ row.ordered }}</td><td>{{ pct(row.sell_through) }}</td><td>{{ row.produced }}</td><td>{{ pct(row.production_ratio) }}</td>
                <td>{{ row.unit_price ? `¥${row.unit_price.toFixed(1)}` : '—' }}</td><td>{{ pct(row.sku_share) }}</td><td>{{ intFmt.format(row.quantity) }}</td><td>{{ pct(row.quantity_share) }}</td><td>{{ money(row.amount) }}</td><td>{{ pct(row.amount_share) }}</td>
              </tr>
            </tbody>
          </table></div>
        </section>

        <section class="yarn-section season-wall-section">
          <div class="season-wall-head">
            <h3>款式等级图墙</h3>
            <div class="yarn-mode-switch" role="tablist" aria-label="图墙分组">
              <button type="button" :class="{ 'is-active': wallMode === 'category' }" :aria-selected="wallMode === 'category'" @click="wallMode = 'category'">按品类</button>
              <button type="button" :class="{ 'is-active': wallMode === 'material' }" :aria-selected="wallMode === 'material'" @click="wallMode = 'material'">按材质</button>
            </div>
          </div>
          <div class="yarn-season-chips season-wall-tabs" role="group" aria-label="选择分组">
            <button v-for="(item, index) in walls" :key="item.name" type="button" class="yarn-chip" :class="{ 'is-active': index === wallIndex }" @click="wallIndex = index">{{ item.name }} <small>{{ item.headline.sku }}</small></button>
          </div>
          <div v-if="wall" :key="`${wallMode}:${wall.name}`">
            <p class="season-headline">SKU {{ wall.headline.sku }} · 动销率 {{ wall.headline.ordered }}/{{ wall.headline.sku }} = {{ pct(wall.headline.sell_through) }} · 产销率（生产比）{{ wall.headline.produced }}/{{ wall.headline.sku }} = {{ pct(wall.headline.production_ratio) }} · 无效率 {{ pct(wall.headline.invalid_ratio) }}</p>
            <div v-for="block in wallBlocks" :key="blockKey(block.material)" class="season-wall-block">
              <h4 v-if="block.material" class="season-wall-material">原料：{{ block.material }}（{{ block.sku }} 款）</h4>
              <div v-for="g in GRADES" :key="g" class="season-grade-row">
                <div class="season-grade-label" :class="'grade-' + (g === '无产量' ? 'none' : g)">{{ g }}<small>{{ block.grades[g].length }} 款</small></div>
                <div class="season-grade-items">
                  <template v-if="g === '无产量' && block.grades[g].length && !expandedUnproduced[blockKey(block.material)]">
                    <button type="button" class="season-unproduced-toggle" @click="toggleUnproduced(blockKey(block.material))">
                      已折叠 {{ block.grades[g].length }} 款无产量（点击展开查看）
                    </button>
                  </template>
                  <template v-else>
                    <button v-if="g === '无产量' && block.grades[g].length" type="button" class="season-unproduced-toggle is-open" @click="toggleUnproduced(blockKey(block.material))">
                      收起无产量（{{ block.grades[g].length }} 款）
                    </button>
                    <figure v-for="item in block.grades[g]" :key="item.code" class="season-style">
                      <img v-if="item.image_url" :src="item.image_url" :alt="item.code" loading="lazy" decoding="async" />
                      <div v-else class="season-noimg">无图</div>
                      <figcaption><strong>{{ item.code }}</strong><span>{{ intFmt.format(item.quantity) }} 件</span></figcaption>
                    </figure>
                    <span v-if="!block.grades[g].length" class="season-empty">—</span>
                  </template>
                </div>
              </div>
            </div>
          </div>
        </section>
      </article>
    </div>
  </div>
</template>

<style scoped>
.season-hints { margin: 0 0 12px; padding: 10px 14px 10px 30px; border: 1px solid #e3c98f; background: #fff8e8; border-radius: 10px; font-size: 13px; }
.season-checkbox { display: flex; align-items: center; gap: 8px; font-size: 14px; font-weight: 500; cursor: pointer; margin: 4px 0 6px; }
.season-checkbox input { width: 16px; height: 16px; cursor: pointer; }
.season-export-bar { flex-direction: column; align-items: stretch; gap: 10px; }
.season-export-desc p { margin: 2px 0; font-size: 13px; color: var(--muted); }
.season-export-desc strong { color: var(--ink); }
.season-export-actions { display: flex; flex-wrap: wrap; gap: 8px; }
.season-btn-secondary { padding: 9px 14px; border: 1px solid var(--line); border-radius: 8px; background: #fff; color: var(--ink); font-size: 13px; font-weight: 500; cursor: pointer; }
.season-btn-secondary:hover:not(:disabled) { border-color: var(--ink); background: #f8f6f0; }
.season-btn-secondary:disabled { opacity: .55; cursor: not-allowed; }
.season-rules { margin-top: 14px; }
.season-rules p { margin: 4px 0; }
.season-checks h3 small { font-weight: 400; color: var(--muted); font-size: 12px; margin-left: 6px; }
.season-kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 10px; margin: 14px 0 8px; }
.season-kpis div { border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px; background: var(--panel); display: flex; flex-direction: column; gap: 2px; }
.season-kpis span { color: var(--muted); font-size: 12px; }
.season-kpis strong { font-size: 20px; font-variant-numeric: tabular-nums; }
.season-kpis small { color: var(--muted); font-size: 12px; }
.season-grade-bar { display: flex; flex-wrap: wrap; gap: 8px; margin: 6px 0 16px; }
.season-grade-bar span { padding: 4px 10px; border-radius: 999px; font-size: 13px; }
.grade-S { background: #f8cbad; } .grade-A { background: #fce4d6; } .grade-B { background: #fff2cc; }
.grade-C { background: #e2efda; } .grade-D { background: #ededed; } .grade-none { background: #d9d9d9; }
.season-total th, .season-total td { font-weight: 600; background: rgba(0, 0, 0, .03); }
.season-wall-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.season-wall-tabs { margin: 10px 0; }
.season-headline { font-size: 13px; color: #8a2a1d; font-weight: 600; margin: 6px 0 10px; }
.season-wall-material { margin: 14px 0 6px; font-size: 14px; }
.season-grade-row { display: grid; grid-template-columns: 64px minmax(0, 1fr); gap: 8px; margin-bottom: 8px; align-items: stretch; }
.season-grade-label { border-radius: 8px; display: flex; flex-direction: column; align-items: center; justify-content: center; font-weight: 700; font-size: 15px; }
.season-grade-label small { font-weight: 400; font-size: 11px; }
.season-grade-items { display: flex; flex-wrap: wrap; gap: 8px; min-height: 40px; align-items: flex-start; }
.season-unproduced-toggle { align-self: center; padding: 6px 12px; border: 1px dashed var(--line); border-radius: 8px; background: #faf8f4; color: var(--muted); font-size: 12px; cursor: pointer; }
.season-unproduced-toggle:hover { border-color: var(--ink); color: var(--ink); }
.season-unproduced-toggle.is-open { flex-basis: 100%; text-align: left; margin-bottom: 2px; }
.season-style { margin: 0; width: 96px; border: 1px solid var(--line); border-radius: 8px; overflow: hidden; background: #fff; }
.season-style img, .season-noimg { width: 96px; height: 124px; object-fit: cover; display: block; background: #f3f1ec; }
.season-noimg { display: flex; align-items: center; justify-content: center; color: var(--muted); font-size: 12px; }
.season-style figcaption { padding: 4px 6px; display: flex; flex-direction: column; font-size: 11px; line-height: 1.3; }
.season-style figcaption strong { font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.season-empty { color: var(--muted); align-self: center; }
</style>
