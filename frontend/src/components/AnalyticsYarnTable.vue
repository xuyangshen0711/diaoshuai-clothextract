<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import {
  exportYarnTable,
  previewYarnTable,
  yarnTableOptions,
  type YarnFilterOptions,
  type YarnPeriodOption,
  type YarnPreviewResponse,
  type YarnReportFilters,
} from '../analyticsApi'

const props = defineProps<{ active: boolean }>()
const emit = defineEmits<{ 'update:busy': [value: boolean] }>()

const PART_LABELS = ['上旬', '中旬', '下旬'] as const
const intFmt = new Intl.NumberFormat('zh-CN')
const moneyFmt = new Intl.NumberFormat('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
const decFmt = new Intl.NumberFormat('zh-CN', { minimumFractionDigits: 1, maximumFractionDigits: 1 })

const options = ref<YarnFilterOptions | null>(null)
const extraPeriods = ref<YarnPeriodOption[]>([])
const loadingOptions = ref(false)
const preparing = ref(false)
const downloading = ref(false)
const error = ref('')

const snapStartInput = ref('')
const snapEndInput = ref('')
const snapHint = ref('')

const preview = ref<YarnPreviewResponse | null>(null)
const acknowledgedForPreview = ref(false)
const lastSubmittedSignature = ref('')
const selectedMaterials = ref<string[]>([])

const shanghaiNow = new Date(Date.now() + 8 * 60 * 60 * 1000)
const todayIso = new Date(
  Date.UTC(shanghaiNow.getUTCFullYear(), shanghaiNow.getUTCMonth(), shanghaiNow.getUTCDate()),
).toISOString().slice(0, 10)

function defaultFilters(): YarnReportFilters {
  return {
    seasons: [],
    time_mode: 'period',
    period_start: '',
    period_end: '',
    start_date: null,
    end_date: todayIso,
    compare_mode: 'none',
    compare_period_start: '',
    compare_period_end: '',
    compare_start_date: null,
    compare_end_date: null,
  }
}

const filters = ref<YarnReportFilters>(defaultFilters())

const busy = computed(() => loadingOptions.value || preparing.value || downloading.value)
watch(busy, val => emit('update:busy', val))

function periodFromDate(isoDate: string): YarnPeriodOption | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec((isoDate || '').trim())
  if (!match) return null
  const year = Number(match[1])
  const month = Number(match[2])
  const day = Number(match[3])
  if (!year || month < 1 || month > 12 || day < 1 || day > 31) return null
  const part = day <= 10 ? 1 : day <= 20 ? 2 : 3
  const key = `${String(year).padStart(4, '0')}-${String(month).padStart(2, '0')}-${part}`
  const label = `${year}年${month}月${PART_LABELS[part - 1]}`
  const lastDay = new Date(Date.UTC(year, month, 0)).getUTCDate()
  const startDay = part === 1 ? '01' : part === 2 ? '11' : '21'
  const endDay = part === 1 ? '10' : part === 2 ? '20' : String(lastDay).padStart(2, '0')
  const ym = `${String(year).padStart(4, '0')}-${String(month).padStart(2, '0')}`
  return {
    key,
    label,
    start: `${ym}-${startDay}`,
    end: `${ym}-${endDay}`,
    has_orders: Boolean(options.value?.periods.some(p => p.key === key && p.has_orders)),
  }
}

const allPeriods = computed<YarnPeriodOption[]>(() => {
  const map = new Map<string, YarnPeriodOption>()
  for (const p of options.value?.periods ?? []) map.set(p.key, p)
  for (const p of extraPeriods.value) {
    if (!map.has(p.key)) map.set(p.key, p)
  }
  const todayPeriod = periodFromDate(todayIso)
  if (todayPeriod && !map.has(todayPeriod.key)) map.set(todayPeriod.key, todayPeriod)
  return [...map.values()].sort((a, b) => a.key.localeCompare(b.key))
})

function ensurePeriodOption(isoDate: string): YarnPeriodOption | null {
  const item = periodFromDate(isoDate)
  if (!item) return null
  if (!allPeriods.value.some(p => p.key === item.key)) {
    extraPeriods.value = [...extraPeriods.value, item]
  }
  return item
}

function snapDate(target: 'start' | 'end', value: string) {
  if (target === 'start') snapStartInput.value = value
  else snapEndInput.value = value
  if (!value) return
  const period = ensurePeriodOption(value)
  if (!period) return
  if (target === 'start') {
    filters.value.period_start = period.key
    snapHint.value = `起始日 ${value} 已自动归入「${period.label}」`
  } else {
    filters.value.period_end = period.key
    snapHint.value = `截止日 ${value} 已自动归入「${period.label}」`
  }
}

const endDatePeriodLabel = computed(() => {
  if (!filters.value.end_date) return ''
  return periodFromDate(filters.value.end_date)?.label ?? ''
})

const startDatePeriodLabel = computed(() => {
  if (!filters.value.start_date) return ''
  return periodFromDate(filters.value.start_date)?.label ?? ''
})

function currentFilterSignature() {
  return JSON.stringify(filters.value)
}

const filtersChanged = computed(() =>
  Boolean(preview.value && lastSubmittedSignature.value !== currentFilterSignature()),
)

function message(reason: unknown) {
  return reason instanceof Error ? reason.message : '操作暂时无法完成，请稍后再试。'
}

async function loadOptions() {
  if (options.value || loadingOptions.value) return
  loadingOptions.value = true
  error.value = ''
  try {
    const loaded = await yarnTableOptions()
    options.value = loaded
    if (loaded.periods.length) {
      const latest = loaded.periods[loaded.periods.length - 1]
      if (latest) filters.value.period_end = latest.key
      filters.value.period_start = ''
      if (loaded.periods.length >= 2) {
        const prev = loaded.periods[loaded.periods.length - 2]
        if (prev) filters.value.compare_period_end = prev.key
      }
    } else {
      const fallback = ensurePeriodOption(todayIso)
      if (fallback) filters.value.period_end = fallback.key
    }
    if (loaded.last_order_date) {
      filters.value.end_date = loaded.last_order_date
      filters.value.start_date = loaded.first_order_date
    }
  } catch (reason) {
    error.value = message(reason)
  } finally {
    loadingOptions.value = false
  }
}

watch(
  () => props.active,
  isActive => {
    if (isActive) void loadOptions()
  },
  { immediate: true },
)

function toggleSeason(season: string) {
  if (season === '全部') {
    filters.value.seasons = []
    return
  }
  const idx = filters.value.seasons.indexOf(season)
  if (idx >= 0) {
    filters.value.seasons = filters.value.seasons.filter(s => s !== season)
  } else {
    filters.value.seasons = [...filters.value.seasons, season]
  }
}

function applyPeriodPreset(mode: 'cutoff_latest' | 'full_range' | 'single_latest') {
  const list = allPeriods.value
  if (!list.length) return
  const first = list[0]
  const latest = list[list.length - 1]
  if (!first || !latest) return
  snapHint.value = ''
  snapStartInput.value = ''
  snapEndInput.value = ''
  if (mode === 'cutoff_latest') {
    filters.value.period_start = ''
    filters.value.period_end = latest.key
  } else if (mode === 'full_range') {
    filters.value.period_start = first.key
    filters.value.period_end = latest.key
  } else {
    filters.value.period_start = latest.key
    filters.value.period_end = latest.key
  }
}

function applyDatePreset(mode: 'cutoff_latest' | 'available' | 'recent90') {
  if (mode === 'cutoff_latest' && options.value?.last_order_date) {
    filters.value.start_date = null
    filters.value.end_date = options.value.last_order_date
  } else if (mode === 'available' && options.value?.first_order_date && options.value.last_order_date) {
    filters.value.start_date = options.value.first_order_date
    filters.value.end_date = options.value.last_order_date
  } else {
    const endMs = Date.parse(todayIso + 'T00:00:00Z')
    const startIso = new Date(endMs - 89 * 86_400_000).toISOString().slice(0, 10)
    filters.value.start_date = startIso
    filters.value.end_date = todayIso
  }
}

function validateBeforeSubmit(): boolean {
  if (filters.value.time_mode === 'period') {
    if (!filters.value.period_end) {
      error.value = '请选择截止旬周期。'
      return false
    }
    if (filters.value.period_start && filters.value.period_start > filters.value.period_end) {
      error.value = '起始旬不能晚于截止旬。'
      return false
    }
  } else {
    if (!filters.value.end_date) {
      error.value = '请选择截止日期。'
      return false
    }
    if (filters.value.start_date && filters.value.start_date > filters.value.end_date) {
      error.value = '开始日期不能晚于截止日期。'
      return false
    }
  }

  if (filters.value.compare_mode === 'custom_period') {
    if (!filters.value.compare_period_end) {
      error.value = '请选择对比基期的截止旬。'
      return false
    }
    if (
      filters.value.compare_period_start &&
      filters.value.compare_period_start > filters.value.compare_period_end
    ) {
      error.value = '对比起始旬不能晚于对比截止旬。'
      return false
    }
  } else if (filters.value.compare_mode === 'custom_date') {
    if (!filters.value.compare_end_date) {
      error.value = '请选择对比基期的截止日期。'
      return false
    }
    if (
      filters.value.compare_start_date &&
      filters.value.compare_start_date > filters.value.compare_end_date
    ) {
      error.value = '对比开始日期不能晚于对比截止日期。'
      return false
    }
  }
  return true
}

async function runPreview(acknowledgeMissing = false) {
  if (preparing.value || !validateBeforeSubmit()) return
  error.value = ''
  preparing.value = true
  try {
    const payload: YarnReportFilters = {
      ...filters.value,
      start_date: filters.value.start_date || null,
      end_date: filters.value.end_date || null,
      compare_start_date: filters.value.compare_start_date || null,
      compare_end_date: filters.value.compare_end_date || null,
    }
    const sig = currentFilterSignature()
    const result = await previewYarnTable(payload, acknowledgeMissing)
    preview.value = result
    acknowledgedForPreview.value = acknowledgeMissing
    lastSubmittedSignature.value = sig
    if (result.report) {
      selectedMaterials.value = [...result.report.main_materials]
    }
  } catch (reason) {
    error.value = message(reason)
  } finally {
    preparing.value = false
  }
}

function selectMaterialPreset(mode: 'main' | 'all' | 'none') {
  const report = preview.value?.report
  if (!report) return
  if (mode === 'main') {
    selectedMaterials.value = [...report.main_materials]
  } else if (mode === 'all') {
    selectedMaterials.value = report.materials_summary.map(m => m.material)
  } else {
    selectedMaterials.value = []
  }
}

function toggleMaterial(material: string) {
  if (selectedMaterials.value.includes(material)) {
    selectedMaterials.value = selectedMaterials.value.filter(m => m !== material)
  } else {
    selectedMaterials.value = [...selectedMaterials.value, material]
  }
}

const report = computed(() => preview.value?.report ?? null)
const compareEnabled = computed(() => report.value?.scope.compare_mode !== 'none')

const visibleColorBlocks = computed(() => {
  if (!report.value) return []
  const chosen = new Set(selectedMaterials.value)
  return report.value.color_blocks.filter(b => chosen.has(b.material))
})

function pct(val: number | null | undefined) {
  if (val === null || val === undefined || Number.isNaN(val)) return '—'
  return `${(val * 100).toFixed(2)}%`
}

function signedInt(val: number | null | undefined) {
  if (val === null || val === undefined || Number.isNaN(val)) return '—'
  if (val > 0) return `+${intFmt.format(val)}`
  return intFmt.format(val)
}

function signedPct(val: number | null | undefined) {
  if (val === null || val === undefined || Number.isNaN(val)) return '—'
  const prefix = val > 0 ? '+' : ''
  return `${prefix}${(val * 100).toFixed(2)}%`
}

function growthClass(val: number | null | undefined) {
  if (val === null || val === undefined || val === 0) return ''
  return val > 0 ? 'yarn-growth-up' : 'yarn-growth-down'
}

async function downloadExcel() {
  if (downloading.value || !report.value || filtersChanged.value) return
  downloading.value = true
  error.value = ''
  try {
    const payload: YarnReportFilters = {
      ...filters.value,
      start_date: filters.value.start_date || null,
      end_date: filters.value.end_date || null,
      compare_start_date: filters.value.compare_start_date || null,
      compare_end_date: filters.value.compare_end_date || null,
    }
    const blob = await exportYarnTable(
      payload,
      report.value.snapshot,
      acknowledgedForPreview.value,
      selectedMaterials.value,
    )
    const seasonPart = report.value.scope.seasons.length
      ? report.value.scope.seasons.slice(0, 3).join('_')
      : '全部季节'
    const endPart = report.value.scope.effective_end_date
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `本厂纱线与色卡表-${seasonPart}-${endPart}.xlsx`
    document.body.append(link)
    link.click()
    link.remove()
    window.setTimeout(() => URL.revokeObjectURL(url), 10_000)
  } catch (reason) {
    error.value = message(reason)
  } finally {
    downloading.value = false
  }
}

function reset() {
  options.value = null
  extraPeriods.value = []
  preview.value = null
  acknowledgedForPreview.value = false
  lastSubmittedSignature.value = ''
  selectedMaterials.value = []
  error.value = ''
  snapHint.value = ''
  snapStartInput.value = ''
  snapEndInput.value = ''
  filters.value = defaultFilters()
}

defineExpose({ reset })
</script>

<template>
  <div class="analytics-layout yarn-layout">
    <form class="analytics-controls yarn-controls" @submit.prevent="runPreview(false)">
      <div class="analytics-section-title">
        <h2>纱线与色卡范围</h2>
      </div>

      <!-- 1. 季节多选标签 -->
      <fieldset :disabled="preparing || loadingOptions">
        <legend>订单年度 / 季节 <small>可多选标签</small></legend>
        <div v-if="options?.seasons.length" class="yarn-season-chips" role="group" aria-label="订单年度与季节标签">
          <button
            type="button"
            class="yarn-chip"
            :class="{ 'is-active': filters.seasons.length === 0 }"
            :aria-pressed="filters.seasons.length === 0"
            @click="toggleSeason('全部')"
          >全部</button>
          <button
            v-for="season in options.seasons"
            :key="season"
            type="button"
            class="yarn-chip"
            :class="{ 'is-active': filters.seasons.includes(season) }"
            :aria-pressed="filters.seasons.includes(season)"
            @click="toggleSeason(season)"
          >{{ season }}</button>
        </div>
        <p class="analytics-note">
          {{ filters.seasons.length ? `已选 ${filters.seasons.length} 个标签：${filters.seasons.join('、')}` : '当前统计全部季节' }}
        </p>
      </fieldset>

      <!-- 2. 时间选择模式：按旬 / 按日期 -->
      <fieldset :disabled="preparing || loadingOptions">
        <legend>统计时间 <small>只统计截止当期</small></legend>
        <div class="yarn-mode-switch" role="tablist" aria-label="时间筛选方式">
          <button
            type="button"
            :class="{ 'is-active': filters.time_mode === 'period' }"
            :aria-selected="filters.time_mode === 'period'"
            @click="filters.time_mode = 'period'"
          >按旬选择</button>
          <button
            type="button"
            :class="{ 'is-active': filters.time_mode === 'date' }"
            :aria-selected="filters.time_mode === 'date'"
            @click="filters.time_mode = 'date'"
          >按日期选择</button>
        </div>

        <template v-if="filters.time_mode === 'period'">
          <div class="analytics-presets">
            <button type="button" @click="applyPeriodPreset('cutoff_latest')">截止最新一旬</button>
            <button type="button" @click="applyPeriodPreset('full_range')">已有全部旬</button>
            <button type="button" @click="applyPeriodPreset('single_latest')">仅最新一旬</button>
          </div>

          <label class="analytics-field">
            起始旬（留空代表累计截止当期）
            <select v-model="filters.period_start" aria-label="起始旬">
              <option value="">最早订单起（累计至截止旬）</option>
              <option v-for="p in allPeriods" :key="'s-' + p.key" :value="p.key">
                {{ p.label }}{{ p.has_orders ? '' : '（无单）' }}
              </option>
            </select>
          </label>

          <label class="analytics-field">
            截止当期（旬）
            <select v-model="filters.period_end" required aria-label="截止旬">
              <option v-for="p in allPeriods" :key="'e-' + p.key" :value="p.key">
                {{ p.label }}{{ p.has_orders ? '' : '（无单）' }}
              </option>
            </select>
          </label>

          <details class="yarn-date-snapper">
            <summary>选具体日期自动归入旬（如 10.29 → 10月下旬）</summary>
            <div class="yarn-snap-grid">
              <label>
                归入起始旬
                <input
                  :value="snapStartInput"
                  type="date"
                  aria-label="选日期归入起始旬"
                  @input="snapDate('start', ($event.target as HTMLInputElement).value)"
                />
              </label>
              <label>
                归入截止旬
                <input
                  :value="snapEndInput"
                  type="date"
                  aria-label="选日期归入截止旬"
                  @input="snapDate('end', ($event.target as HTMLInputElement).value)"
                />
              </label>
            </div>
            <p class="analytics-note">规则：1–10 日上旬、11–20 日中旬、21 日至月底下旬。</p>
            <p v-if="snapHint" class="yarn-snap-hint">{{ snapHint }}</p>
          </details>
        </template>

        <template v-else>
          <div class="analytics-presets">
            <button v-if="options?.last_order_date" type="button" @click="applyDatePreset('cutoff_latest')">截止最新日</button>
            <button v-if="options?.last_order_date" type="button" @click="applyDatePreset('available')">已有订单范围</button>
            <button type="button" @click="applyDatePreset('recent90')">最近 90 天</button>
          </div>
          <label class="analytics-field">
            <span>开始日期 <small class="yarn-inline-clear" v-if="filters.start_date" @click.prevent="filters.start_date = null">清空（改为累计截止）</small></span>
            <input v-model="filters.start_date" type="date" aria-label="开始日期" />
            <small v-if="startDatePeriodLabel" class="analytics-note">归属：{{ startDatePeriodLabel }}</small>
            <small v-else class="analytics-note">未填开始日期时，统计截止日期及此前全部订单</small>
          </label>
          <label class="analytics-field">
            截止日期
            <input v-model="filters.end_date" type="date" required aria-label="截止日期" />
            <small v-if="endDatePeriodLabel" class="analytics-note">自动归旬：{{ endDatePeriodLabel }}</small>
          </label>
        </template>

        <p v-if="options?.last_order_date" class="analytics-note">
          当前库内带日期明细：{{ options.first_order_date }} 至 {{ options.last_order_date }}
        </p>
      </fieldset>

      <!-- 3. 增长率对比基准 -->
      <fieldset :disabled="preparing || loadingOptions">
        <legend>增长率计算 <small>可选</small></legend>
        <label class="analytics-field">
          对比基准
          <select v-model="filters.compare_mode" aria-label="增长率对比基准">
            <option value="none">不计算增长率</option>
            <option value="previous_period">自动对比上一旬（顺推前一旬）</option>
            <option value="custom_period">自定义对比旬范围</option>
            <option value="custom_date">自定义对比日期范围</option>
          </select>
        </label>

        <template v-if="filters.compare_mode === 'custom_period'">
          <label class="analytics-field">
            对比起始旬（可选）
            <select v-model="filters.compare_period_start" aria-label="对比起始旬">
              <option value="">与本期起点一致 / 最早起</option>
              <option v-for="p in allPeriods" :key="'cs-' + p.key" :value="p.key">{{ p.label }}</option>
            </select>
          </label>
          <label class="analytics-field">
            对比截止旬
            <select v-model="filters.compare_period_end" required aria-label="对比截止旬">
              <option value="" disabled>请选择对比截止旬</option>
              <option v-for="p in allPeriods" :key="'ce-' + p.key" :value="p.key">{{ p.label }}</option>
            </select>
          </label>
        </template>

        <template v-else-if="filters.compare_mode === 'custom_date'">
          <label class="analytics-field">
            对比开始日（可选）
            <input v-model="filters.compare_start_date" type="date" aria-label="对比开始日期" />
          </label>
          <label class="analytics-field">
            对比截止日
            <input v-model="filters.compare_end_date" type="date" required aria-label="对比截止日期" />
          </label>
        </template>

        <p class="analytics-note">
          {{ filters.compare_mode === 'none' ? '增长数量与增长率列留空，需要对比时可随时开启。' : '增长率 =（本期数量 - 对比基期数量）÷ 对比基期数量。' }}
        </p>
      </fieldset>

      <button type="submit" class="analytics-primary" :disabled="preparing || loadingOptions || !options">
        {{ preparing ? '正在核对并生成…' : '生成纱线与色卡表' }}
      </button>
    </form>

    <!-- 右侧预览与导出工作区 -->
    <div class="analytics-workspace">
      <div v-if="report" class="analytics-export">
        <p v-if="filtersChanged">筛选条件已调整，请重新点击「生成纱线与色卡表」后再导出。</p>
        <p v-else>
          将导出《颜色对比汇总》（含当前勾选的 {{ selectedMaterials.length }} 类材质）与《纱线汇总》两张表，均附带空白备注列供手填。
        </p>
        <button
          type="button"
          class="analytics-primary"
          :disabled="downloading || preparing || filtersChanged"
          @click="downloadExcel"
        >
          {{ downloading ? '正在生成 Excel…' : '导出 Excel (.xlsx)' }}
        </button>
      </div>

      <p v-if="error" class="analytics-error" role="alert">{{ error }}</p>

      <!-- 缺失数据拦截提醒卡片 -->
      <section
        v-if="preview?.requires_confirmation && preview.missing.has_warnings"
        class="yarn-missing-gate"
        role="region"
        aria-label="数据完整性提醒"
      >
        <div class="yarn-missing-head">
          <div>
            <span class="yarn-badge-warn">产出前核对提醒</span>
            <h2>检测到 {{ preview.missing.warning_count }} 项数据缺漏，是否继续产出？</h2>
            <p>
              筛选范围：{{ preview.scope.window_label }} · {{ preview.scope.season_label }}（匹配带日期明细 {{ intFmt.format(preview.scope.order_line_count) }} 条）
            </p>
          </div>
        </div>

        <ul class="yarn-missing-list">
          <li v-for="w in preview.missing.warnings" :key="w.code" :class="'is-' + w.severity">
            <strong>{{ w.title }}</strong>
            <p>{{ w.detail }}</p>
            <details v-if="w.sample_codes.length">
              <summary>查看涉及款号示例（显示前 {{ w.sample_codes.length }} 个）</summary>
              <div class="yarn-code-list">
                <code v-for="code in w.sample_codes" :key="code">{{ code }}</code>
              </div>
            </details>
          </li>
        </ul>

        <div class="yarn-missing-actions">
          <button
            type="button"
            class="analytics-primary"
            :disabled="preparing"
            @click="runPreview(true)"
          >
            {{ preparing ? '正在产出…' : '已知晓缺失，继续产出报表' }}
          </button>
          <RouterLink to="/import" class="analytics-quiet yarn-import-link">前往数据导入补齐资料 →</RouterLink>
        </div>
      </section>

      <!-- 正式报表预览 -->
      <article v-else-if="report" class="analytics-report yarn-report" aria-label="纱线与色卡表预览">
        <header class="analytics-report-head">
          <div>
            <h2>本厂纱线与色卡统计表</h2>
            <p>
              {{ report.scope.window_label }} · {{ report.scope.season_label }}
              <template v-if="compareEnabled"> · 对比基期：{{ report.scope.compare_label }}</template>
            </p>
          </div>
          <span>只统计所选截止当期</span>
        </header>

        <details v-if="preview?.missing.has_warnings" class="yarn-ack-banner">
          <summary>
            含 {{ preview.missing.warning_count }} 项已确认的数据缺漏提示（未匹配原料的款号已单独归入「未匹配原料」）
          </summary>
          <ul>
            <li v-for="w in preview.missing.warnings" :key="w.code">
              <strong>{{ w.title }}：</strong>{{ w.detail }}
            </li>
          </ul>
        </details>

        <dl class="analytics-metrics">
          <div>
            <dt>订单总数量</dt>
            <dd>{{ intFmt.format(report.materials_totals.quantity) }}</dd>
            <small>
              件
              <template v-if="compareEnabled && report.materials_totals.growth_rate !== null">
                · 较基期 {{ signedPct(report.materials_totals.growth_rate) }}
              </template>
            </small>
          </div>
          <div>
            <dt>订单总金额</dt>
            <dd>{{ moneyFmt.format(report.materials_totals.amount) }}</dd>
            <small>元</small>
          </div>
          <div>
            <dt>计数项:款号</dt>
            <dd>{{ intFmt.format(report.materials_totals.style_count) }}</dd>
            <small>其中品牌款 {{ intFmt.format(report.materials_totals.brand_style_count) }} 款</small>
          </div>
          <div>
            <dt>订货客户 / 材质数</dt>
            <dd>{{ intFmt.format(report.materials_totals.customer_count) }}</dd>
            <small>家客户 · {{ intFmt.format(report.materials_summary.length) }} 类材质</small>
          </div>
        </dl>

        <p v-if="report.materials_summary.length === 0" class="analytics-empty">
          当前筛选的时间段与季节内没有订单明细，请调整左侧时间或季节标签。
        </p>

        <template v-else>
          <!-- 表一：分材质颜色对比汇总 -->
          <section class="analytics-ranked yarn-section">
            <div class="analytics-section-title">
              <div>
                <h3>一、分材质「颜色对比汇总」</h3>
                <p>默认自动展开主力材质；可自由勾选需要查看和导出到 Excel 的材质大类</p>
              </div>
              <div class="analytics-presets">
                <button type="button" @click="selectMaterialPreset('main')">仅主力材质</button>
                <button type="button" @click="selectMaterialPreset('all')">全选材质</button>
                <button type="button" @click="selectMaterialPreset('none')">清空勾选</button>
              </div>
            </div>

            <div class="yarn-material-checks" role="group" aria-label="勾选要展开的材质大类">
              <label
                v-for="item in report.materials_summary"
                :key="item.material"
                class="yarn-mat-pill"
                :class="{
                  'is-checked': selectedMaterials.includes(item.material),
                  'is-unmatched': item.is_unmatched,
                }"
              >
                <input
                  type="checkbox"
                  :checked="selectedMaterials.includes(item.material)"
                  @change="toggleMaterial(item.material)"
                />
                <span class="yarn-mat-name">{{ item.material }}</span>
                <small>{{ item.style_count }}款 · {{ pct(item.quantity_share) }}</small>
                <em v-if="item.is_main" class="yarn-tag-main">主力</em>
                <em v-else-if="item.is_unmatched" class="yarn-tag-warn">缺资料</em>
              </label>
            </div>

            <p v-if="!visibleColorBlocks.length" class="analytics-empty">
              尚未勾选要展开的材质大类，请在上方勾选，或直接查看下方「二、全盘纱线汇总」。
            </p>

            <div v-for="block in visibleColorBlocks" :key="block.material" class="yarn-color-block">
              <div class="yarn-block-head">
                <strong>{{ block.material }}</strong>
                <span>
                  共 {{ block.totals.style_count }} 款（品牌 {{ block.totals.brand_style_count }} 款） ·
                  {{ block.totals.customer_count }} 家客户 ·
                  {{ intFmt.format(block.totals.quantity) }} 件
                </span>
              </div>
              <div class="analytics-table-scroll">
                <table class="analytics-table yarn-table">
                  <thead>
                    <tr>
                      <th scope="col">序号</th>
                      <th scope="col" class="is-left">材质大类</th>
                      <th scope="col" class="is-left">颜色大类</th>
                      <th scope="col">计数项:款号</th>
                      <th scope="col">品牌款式数量</th>
                      <th scope="col">计数项:客户</th>
                      <th scope="col">订单总数量</th>
                      <th scope="col">订单总金额</th>
                      <th scope="col">占比</th>
                      <th scope="col">增长数量</th>
                      <th scope="col">增长率</th>
                      <th scope="col" class="is-left">备注</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="row in block.rows" :key="row.color_group">
                      <td>{{ row.rank }}</td>
                      <td class="is-left">{{ row.material }}</td>
                      <th scope="row" class="is-left">{{ row.color_group }}</th>
                      <td>{{ intFmt.format(row.style_count) }}</td>
                      <td>{{ intFmt.format(row.brand_style_count) }}</td>
                      <td>{{ intFmt.format(row.customer_count) }}</td>
                      <td>{{ intFmt.format(row.quantity) }}</td>
                      <td>{{ moneyFmt.format(row.amount) }}</td>
                      <td>{{ pct(row.share) }}</td>
                      <td :class="growthClass(row.growth_quantity)">{{ signedInt(row.growth_quantity) }}</td>
                      <td :class="growthClass(row.growth_rate)">{{ signedPct(row.growth_rate) }}</td>
                      <td class="is-left yarn-manual-col"></td>
                    </tr>
                  </tbody>
                  <tfoot>
                    <tr>
                      <td></td>
                      <td class="is-left">{{ block.material }}</td>
                      <th scope="row" class="is-left">合计</th>
                      <td>{{ intFmt.format(block.totals.style_count) }}</td>
                      <td>{{ intFmt.format(block.totals.brand_style_count) }}</td>
                      <td>{{ intFmt.format(block.totals.customer_count) }}</td>
                      <td>{{ intFmt.format(block.totals.quantity) }}</td>
                      <td>{{ moneyFmt.format(block.totals.amount) }}</td>
                      <td>{{ pct(block.totals.share) }}</td>
                      <td :class="growthClass(block.totals.growth_quantity)">{{ signedInt(block.totals.growth_quantity) }}</td>
                      <td :class="growthClass(block.totals.growth_rate)">{{ signedPct(block.totals.growth_rate) }}</td>
                      <td class="is-left yarn-manual-col"></td>
                    </tr>
                  </tfoot>
                </table>
              </div>
            </div>
          </section>

          <!-- 表二：全盘纱线/材质大类汇总 -->
          <section class="analytics-ranked yarn-section">
            <div class="analytics-section-title">
              <div>
                <h3>二、全盘「纱线/材质大类汇总」</h3>
                <p>按订单数量降序排列；品牌款根据款号后缀与专属标识自动识别，拼色按首色自动归类</p>
              </div>
              <p>共 {{ report.materials_summary.length }} 类材质</p>
            </div>

            <div class="analytics-table-scroll">
              <table class="analytics-table yarn-table">
                <thead>
                  <tr>
                    <th scope="col">序号</th>
                    <th scope="col" class="is-left">材质大类</th>
                    <th scope="col">计数项:款号</th>
                    <th scope="col">品牌款号数量</th>
                    <th scope="col">计数项:客户</th>
                    <th scope="col">订货客户数量占比</th>
                    <th scope="col">求和项:订单数量</th>
                    <th scope="col">求和项:订单金额</th>
                    <th scope="col">订单量占比</th>
                    <th scope="col">平均单款量</th>
                    <th scope="col">增长数量</th>
                    <th scope="col">增长率</th>
                    <th scope="col" class="is-left">备注</th>
                  </tr>
                </thead>
                <tbody>
                  <tr
                    v-for="item in report.materials_summary"
                    :key="item.material"
                    :class="{ 'yarn-unmatched-row': item.is_unmatched }"
                  >
                    <td>{{ item.rank }}</td>
                    <th scope="row" class="is-left">
                      {{ item.material }}
                      <em v-if="item.is_main" class="yarn-tag-main">主力</em>
                    </th>
                    <td>{{ intFmt.format(item.style_count) }}</td>
                    <td>{{ intFmt.format(item.brand_style_count) }}</td>
                    <td>{{ intFmt.format(item.customer_count) }}</td>
                    <td>{{ pct(item.customer_share) }}</td>
                    <td>{{ intFmt.format(item.quantity) }}</td>
                    <td>{{ moneyFmt.format(item.amount) }}</td>
                    <td>{{ pct(item.quantity_share) }}</td>
                    <td>{{ decFmt.format(item.avg_per_style) }}</td>
                    <td :class="growthClass(item.growth_quantity)">{{ signedInt(item.growth_quantity) }}</td>
                    <td :class="growthClass(item.growth_rate)">{{ signedPct(item.growth_rate) }}</td>
                    <td class="is-left yarn-manual-col"></td>
                  </tr>
                </tbody>
                <tfoot>
                  <tr>
                    <td></td>
                    <th scope="row" class="is-left">总计</th>
                    <td>{{ intFmt.format(report.materials_totals.style_count) }}</td>
                    <td>{{ intFmt.format(report.materials_totals.brand_style_count) }}</td>
                    <td>{{ intFmt.format(report.materials_totals.customer_count) }}</td>
                    <td>{{ pct(report.materials_totals.customer_share) }}</td>
                    <td>{{ intFmt.format(report.materials_totals.quantity) }}</td>
                    <td>{{ moneyFmt.format(report.materials_totals.amount) }}</td>
                    <td>{{ pct(report.materials_totals.quantity_share) }}</td>
                    <td>{{ decFmt.format(report.materials_totals.avg_per_style) }}</td>
                    <td :class="growthClass(report.materials_totals.growth_quantity)">{{ signedInt(report.materials_totals.growth_quantity) }}</td>
                    <td :class="growthClass(report.materials_totals.growth_rate)">{{ signedPct(report.materials_totals.growth_rate) }}</td>
                    <td class="is-left yarn-manual-col"></td>
                  </tr>
                </tfoot>
              </table>
            </div>
          </section>
        </template>
      </article>

      <!-- 初始引导页 -->
      <div v-else class="analytics-paper-preview">
        <span>本厂产品 / 纱线与色卡表</span>
        <h2>按季节与旬周期，一键产出纱线及色卡统计表</h2>
        <p>无需再手工维护大体积透视总表：系统自动匹配商品资料原料、识别品牌专款、归并拼色大类，并在存在资料缺漏时提前提醒确认。</p>
        <ol>
          <li>多选季节与旬区间</li>
          <li>缺漏款号自动预警</li>
          <li>主力材质色卡对比</li>
          <li>一键导出 Excel 双表</li>
        </ol>
      </div>
    </div>
  </div>
</template>
