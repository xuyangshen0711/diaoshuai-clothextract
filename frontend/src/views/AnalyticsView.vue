<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import {
  analysisLogin, analysisLogout, analysisOptions, analysisSession, exportReport, previewReport,
  type AnalysisFilters, type AnalysisOptions, type AnalysisReport, type AnalysisSession,
} from '../analyticsApi'
import AnalyticsChat from '../components/AnalyticsChat.vue'
import AnalyticsReport from '../components/AnalyticsReport.vue'
import '../styles/analytics.css'

const route = useRoute()
const tab = ref(route.query.tab === 'questions' ? 'questions' : 'report')
const session = ref<AnalysisSession | null>(null)
const options = ref<AnalysisOptions | null>(null)
const loading = ref(true)
const unlocking = ref(false)
const password = ref('')
const error = ref('')
const report = ref<AnalysisReport | null>(null)
const preparing = ref(false)
const downloading = ref(false)
const useDeepseek = ref(false)
const asking = ref(false)
const returnToLibrary = computed(() => typeof route.query.source === 'string' && route.query.source !== '全部'
  ? { name: 'library', query: { source_category: route.query.source } } : '/')

// Shanghai is UTC+8. Avoid depending on the browser's optional Intl time-zone data.
const shanghaiNow = new Date(Date.now() + 8 * 60 * 60 * 1000)
const today = new Date(Date.UTC(shanghaiNow.getUTCFullYear(), shanghaiNow.getUTCMonth(), shanghaiNow.getUTCDate()))
function iso(value: Date) {
  return value.toISOString().slice(0, 10)
}
const filters = ref<AnalysisFilters>({
  start: iso(new Date(today.valueOf() - 89 * 86_400_000)), end: iso(today), categories: [], season: null,
})
const filtersChanged = computed(() => Boolean(report.value && JSON.stringify(report.value.filters) !== JSON.stringify(filters.value)))

async function switchTab(value: string) {
  error.value = ''
  tab.value = value
  if (value === 'report') await loadReportOptions()
}

function preset(value: 'recent' | 'quarter' | 'available') {
  if (value === 'recent') {
    filters.value.start = iso(new Date(today.valueOf() - 89 * 86_400_000))
    filters.value.end = iso(today)
  } else if (value === 'quarter') {
    const month = Math.floor(today.getUTCMonth() / 3) * 3
    filters.value.start = iso(new Date(Date.UTC(today.getUTCFullYear(), month - 3, 1)))
    filters.value.end = iso(new Date(Date.UTC(today.getUTCFullYear(), month, 0)))
  } else if (options.value?.first_order_date && options.value.last_order_date) {
    filters.value.start = options.value.first_order_date
    filters.value.end = options.value.last_order_date
  }
}

function message(reason: unknown) {
  return reason instanceof Error ? reason.message : '操作暂时无法完成，请稍后再试。'
}

async function loadReportOptions() {
  if (options.value) return
  try {
    options.value = await analysisOptions()
  } catch (reason) {
    error.value = message(reason)
  }
}

async function refreshSession() {
  try {
    session.value = await analysisSession()
    if (session.value.authenticated && tab.value === 'report') await loadReportOptions()
  } catch (reason) {
    error.value = message(reason)
  } finally {
    loading.value = false
  }
}
onMounted(refreshSession)

async function unlock() {
  if (unlocking.value) return
  error.value = ''
  unlocking.value = true
  try {
    await analysisLogin(password.value)
    await refreshSession()
  } catch (reason) {
    error.value = message(reason)
  } finally {
    password.value = ''
    unlocking.value = false
  }
}

async function lock() {
  if (preparing.value || downloading.value || asking.value) return
  try {
    await analysisLogout()
    session.value = { ...session.value, configured: true, authenticated: false, deepseek_configured: false }
    report.value = null
    options.value = null
    error.value = ''
  } catch (reason) {
    error.value = message(reason)
  }
}

function validDates() {
  if (filters.value.start && filters.value.end && filters.value.start <= filters.value.end) return true
  error.value = '请选择有效的下单日期范围。'
  return false
}

async function prepare() {
  if (preparing.value || !validDates()) return
  error.value = ''
  preparing.value = true
  try {
    report.value = await previewReport(filters.value, useDeepseek.value)
  } catch (reason) {
    error.value = message(reason)
  } finally {
    preparing.value = false
  }
}

async function download() {
  if (downloading.value || !report.value || filtersChanged.value) return
  downloading.value = true
  error.value = ''
  try {
    const { start, end } = report.value.filters
    const blob = await exportReport(report.value)
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `本厂产品订货分析-${start}-${end}.pdf`
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
</script>

<template>
  <main class="analytics-page">
    <RouterLink :to="tab === 'report' ? { name: 'library', query: { source_category: '本厂产品' } } : returnToLibrary" class="analytics-back">← 返回图库</RouterLink>
    <header class="analytics-hero">
      <div>
        <p>貂帅服装 / 业务资料</p>
        <h1>数据分析</h1>
        <p>从本厂订单生成报告，也可以直接查询款式与图库数据。</p>
      </div>
      <button v-if="session?.authenticated" type="button" class="analytics-quiet" :disabled="preparing || downloading || asking" @click="lock">锁定分析</button>
    </header>

    <p v-if="session?.unencrypted_connection" class="analytics-note" role="note">当前使用 HTTP 兼容访问，密码和数据传输未加密。请在可信网络使用；配置 HTTPS 后可关闭此模式。</p>
    <p v-if="loading" class="analytics-empty" role="status">正在检查分析权限…</p>
    <section v-else-if="!session?.authenticated" class="analytics-lock">
      <h2>解锁数据分析</h2>
      <p>订单金额和内部款式数据需要单独的分析权限。</p>
      <form v-if="session?.configured" @submit.prevent="unlock">
        <label for="analytics-password">分析访问密码</label>
        <input id="analytics-password" v-model="password" type="password" autocomplete="current-password" maxlength="256" required />
        <button type="submit" class="analytics-primary" :disabled="unlocking">{{ unlocking ? '正在解锁…' : '解锁分析' }}</button>
      </form>
      <p v-else-if="session">请管理员设置分析访问密码后再进入。</p>
      <p v-if="error" class="analytics-error" role="alert">{{ error }}</p>
    </section>

    <template v-else>
      <nav class="analytics-tabs" aria-label="分析方式">
        <button type="button" :aria-pressed="tab === 'report'" @click="switchTab('report')">生成报告</button>
        <button type="button" :aria-pressed="tab === 'questions'" @click="switchTab('questions')">问数据</button>
      </nav>

      <div v-if="tab === 'report'" class="analytics-layout">
        <form class="analytics-controls" @submit.prevent="prepare">
          <div class="analytics-section-title"><h2>报告范围</h2></div>
          <fieldset :disabled="preparing">
            <legend>下单日期</legend>
            <div class="analytics-presets">
              <button type="button" @click="preset('recent')">最近 90 天</button>
              <button type="button" @click="preset('quarter')">上季度</button>
              <button v-if="options?.last_order_date" type="button" @click="preset('available')">已有订单范围</button>
            </div>
            <div class="analytics-dates">
              <label>开始 <input v-model="filters.start" type="date" required aria-label="开始日期" /></label>
              <label>结束 <input v-model="filters.end" type="date" required aria-label="结束日期" /></label>
            </div>
            <p v-if="options?.last_order_date" class="analytics-note">明细覆盖 {{ options.first_order_date }} 至 {{ options.last_order_date }}</p>
          </fieldset>
          <fieldset v-if="options?.categories.length" :disabled="preparing">
            <legend>订单品类 <small>可多选</small></legend>
            <div class="analytics-options">
              <label v-for="item in options.categories" :key="item"><input v-model="filters.categories" type="checkbox" :value="item" /> {{ item }}</label>
            </div>
            <p class="analytics-note">{{ filters.categories.length ? `已选 ${filters.categories.length} 个品类` : '当前统计全部品类' }}</p>
          </fieldset>
          <label v-if="options?.seasons.length" class="analytics-field">订单年度 / 季节
            <select v-model="filters.season" :disabled="preparing">
              <option :value="null">全部季节</option>
              <option v-for="item in options.seasons" :key="item" :value="item">{{ item }}</option>
            </select>
          </label>
          <p class="analytics-note">季节按 Excel 订单中的年度、季节统计，并同时受下单日期范围限制。核对整份表格时，请先选择「已有订单范围」。春夏、秋冬作为独立季节保留。</p>
          <label class="analytics-check"><input v-model="useDeepseek" type="checkbox" :disabled="!session.deepseek_configured || preparing" /> 让 DeepSeek 选择报告重点</label>
          <p v-if="useDeepseek" class="analytics-note">仅发送预定义的汇总特征。</p>
          <button type="submit" class="analytics-primary" :disabled="preparing || !options">{{ preparing ? '正在生成…' : '预览报告' }}</button>
        </form>

        <div class="analytics-workspace">
          <div v-if="report" class="analytics-export">
            <p v-if="filtersChanged">筛选已调整，请重新预览后下载。</p>
            <p v-else>预览与 PDF 使用同一份统计口径。</p>
            <button type="button" class="analytics-primary" :disabled="downloading || preparing || filtersChanged" @click="download">{{ downloading ? '正在准备…' : '下载 PDF' }}</button>
          </div>
          <p v-if="error" class="analytics-error" role="alert">{{ error }}</p>
          <AnalyticsReport v-if="report" :report="report" />
          <div v-else class="analytics-paper-preview">
            <span>本厂产品 / 订货报告</span>
            <h2>选一个时间段，查看订单表现</h2>
            <p>报告从订单总览和品类对比开始，再展开到重点款及颜色、尺码分布。</p>
            <ol><li>订单总览</li><li>品类对比</li><li>重点款与配码</li></ol>
          </div>
        </div>

      </div>

      <AnalyticsChat v-show="tab === 'questions'" v-model:busy="asking" :deepseek-configured="session.deepseek_configured" />
    </template>
  </main>
</template>
