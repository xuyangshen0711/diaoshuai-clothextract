<script setup lang="ts">
import { computed } from 'vue'
import type { AnalysisReport } from '../analyticsApi'

const props = defineProps<{ report: AnalysisReport }>()
const number = new Intl.NumberFormat('zh-CN')
const money = new Intl.NumberFormat('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
const metrics = computed(() => [
  { name: '订单件数', value: number.format(props.report.totals.quantity), unit: '件' },
  { name: '订单金额', value: money.format(props.report.totals.amount), unit: '元' },
  { name: '下单款数', value: number.format(props.report.totals.styles), unit: '款' },
  { name: '补单件数', value: number.format(props.report.totals.replenishment), unit: '件，计入总件数' },
])
const nonnegative = (value: number) => Math.max(0, value)
const maxTrend = computed(() => Math.max(1, ...props.report.trend.items.map(
  row => nonnegative(row.quantity - row.replenishment) + nonnegative(row.replenishment),
)))
const hasNegative = computed(() => props.report.trend.items.some(row => row.quantity < row.replenishment || row.replenishment < 0))
const maxCategory = computed(() => Math.max(1, ...props.report.categories.map(row => row.quantity)))
const maxHeat = computed(() => Math.max(1, ...(props.report.focus?.rows.flatMap(row => row.quantities) ?? [])))
</script>

<template>
  <article class="analytics-report" aria-label="订货分析预览">
    <header class="analytics-report-head">
      <div>
        <h2>本厂产品订货分析</h2>
        <p>{{ report.filters.start }} 至 {{ report.filters.end }} · {{ report.filters.categories.join('、') || '全部品类' }}<template v-if="report.filters.season"> · 订单季节 {{ report.filters.season }}</template></p>
      </div>
      <span>按下单日期统计</span>
    </header>

    <dl class="analytics-metrics">
      <div v-for="item in metrics" :key="item.name">
        <dt>{{ item.name }}</dt>
        <dd>{{ item.value }}</dd>
        <small>{{ item.unit }}</small>
      </div>
    </dl>

    <div v-if="report.totals.styles" class="analytics-chart-pair">
      <section>
        <div class="analytics-section-title">
          <h3>订单趋势</h3>
          <p>按{{ report.trend.unit }} · 件</p>
        </div>
        <div class="analytics-legend">
          <span><i class="analytics-green"></i>常规订单</span><span><i class="analytics-gold"></i>补单</span>
        </div>
        <div class="analytics-trend-scroll">
          <div class="analytics-trend" :style="{ gridTemplateColumns: `repeat(${report.trend.items.length}, minmax(52px, 1fr))` }">
            <div v-for="row in report.trend.items" :key="row.name" class="analytics-bar-column">
              <div class="analytics-bar-stack">
                <span>{{ number.format(row.quantity) }}</span>
                <i class="analytics-gold" :style="{ height: `${nonnegative(row.replenishment) / maxTrend * 106}px` }"></i>
                <i class="analytics-green" :style="{ height: `${nonnegative(row.quantity - row.replenishment) / maxTrend * 106}px` }"></i>
              </div>
              <small>{{ row.name }}</small>
            </div>
          </div>
        </div>
        <p v-if="hasNegative" class="analytics-note">标注为净件数，柱形仅展示正值部分。</p>
      </section>
      <section>
        <div class="analytics-section-title"><h3>品类对比</h3><p>按件数</p></div>
        <ul class="analytics-category-list">
          <li v-for="row in report.categories" :key="row.name">
            <div><span>{{ row.name }}</span><strong>{{ number.format(row.quantity) }} 件</strong></div>
            <i :style="{ width: `${nonnegative(row.quantity) / maxCategory * 100}%` }"></i>
          </li>
        </ul>
      </section>
    </div>
    <p v-else class="analytics-empty">这个范围没有带日期的订单明细。可以调整日期，或切到「已有订单范围」。</p>

    <section v-if="report.observations.length" class="analytics-observations">
      <p v-if="report.notice" class="analytics-notice" role="status">{{ report.notice }}</p>
      <div class="analytics-section-title"><h3>本期观察</h3><p>{{ report.provider === 'deepseek' ? 'DeepSeek 辅助选择重点' : '按订单汇总' }}</p></div>
      <ul><li v-for="item in report.observations" :key="item">{{ item }}</li></ul>
    </section>

    <section v-if="report.ranking.length" class="analytics-ranked">
      <div class="analytics-section-title"><h3>重点款</h3><p>订单件数前五款</p></div>
      <div class="analytics-table-scroll">
        <table class="analytics-table">
          <thead><tr><th scope="col">款号</th><th scope="col">品类</th><th scope="col">订单件数</th><th scope="col">金额（元）</th><th scope="col">补单件数</th></tr></thead>
          <tbody>
            <tr v-for="row in report.ranking" :key="row.name">
              <th scope="row"><RouterLink v-if="row.record_id" :to="{ name: 'record', params: { id: row.record_id } }">{{ row.name }}</RouterLink><template v-else>{{ row.name }}</template></th>
              <td>{{ row.categories }}</td><td>{{ number.format(row.quantity) }}</td><td>{{ money.format(row.amount) }}</td><td>{{ number.format(row.replenishment) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <section v-if="report.focus?.sizes.length" class="analytics-ranked">
      <div class="analytics-section-title"><h3>{{ report.focus.style_code }} 的颜色与尺码</h3><p>单位 件</p></div>
      <div class="analytics-table-scroll">
        <table class="analytics-table analytics-heat">
          <thead><tr><th scope="col">颜色</th><th v-for="size in report.focus.sizes" :key="size" scope="col">{{ size }}</th><th scope="col">合计</th></tr></thead>
          <tbody>
            <tr v-for="row in report.focus.rows" :key="row.color">
              <th scope="row">{{ row.color }}</th>
              <td v-for="(value, i) in row.quantities" :key="i" :style="{ backgroundColor: value > 0 ? `rgba(77, 99, 84, ${0.08 + 0.4 * value / maxHeat})` : undefined }">{{ number.format(value) }}</td>
              <td>{{ number.format(row.total) }}</td>
            </tr>
          </tbody>
          <tfoot><tr><th scope="row">合计</th><td v-for="(value, i) in report.focus.totals" :key="i">{{ number.format(value) }}</td><td>{{ number.format(report.focus.total) }}</td></tr></tfoot>
        </table>
      </div>
    </section>
    <p class="analytics-note">全库有 {{ report.coverage.summary_styles }} 个款号的历史汇总和 {{ report.coverage.undated_lines }} 条无日期明细，单独保留供参考。</p>
  </article>
</template>
