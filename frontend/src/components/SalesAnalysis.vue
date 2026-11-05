<script setup lang="ts">
import { computed } from 'vue'
import type { SalesSummary } from '../api'

const props = defineProps<{ sales: SalesSummary }>()
const emit = defineEmits<{ orders: [] }>()
const number = (value: number) => value.toLocaleString('zh-CN')
const palette: Record<string, string> = {
  灰: '#99918c',
  灰色: '#99918c',
  红: '#873d49',
  红色: '#873d49',
  米白: '#dfcfb8',
  米色: '#dfcfb8',
  棕咖: '#706052',
  紫: '#795a87',
  黑: '#383632',
  白: '#e6e1d9',
  蓝: '#647d92',
  绿: '#708573',
  黄: '#c0a45e',
  橙: '#b87d54',
  粉: '#be919d',
}
const fallback = ['#99918c', '#873d49', '#dfcfb8', '#706052', '#795a87', '#647d92', '#708573']
const colors = computed(() => {
  const total = props.sales.by_color.reduce((sum, row) => sum + Math.max(0, row.quantity), 0)
  let start = 0
  return props.sales.by_color.map((row, index) => {
    const share = total ? (Math.max(0, row.quantity) / total) * 100 : 0
    const result = {
      ...row,
      share,
      start,
      color: palette[row.family || row.name] || fallback[index % fallback.length],
    }
    start += share
    return result
  })
})
const hasNegativeColors = computed(() => colors.value.some((row) => row.quantity < 0))
const colorTotal = computed(() => props.sales.by_color.reduce((sum, row) => sum + row.quantity, 0))
const sizeLabel = (name: string) => (name.includes('/') ? name.split('/').at(-1)! : name)
const sizeOrder = ['XXS', 'XS', 'S', 'M', 'L', 'XL', '2XL', '3XL', '4XL', '5XL', '6XL']
const sizes = computed(() =>
  [...props.sales.by_size].sort((a, b) => {
    const ia = sizeOrder.indexOf(sizeLabel(a.name).toUpperCase().replace('XXL', '2XL'))
    const ib = sizeOrder.indexOf(sizeLabel(b.name).toUpperCase().replace('XXL', '2XL'))
    if (ia >= 0 && ib >= 0) return ia - ib
    if (ia >= 0 || ib >= 0) return ia >= 0 ? -1 : 1
    return a.name.localeCompare(b.name, 'zh-CN', { numeric: true })
  }),
)
const sizeMax = computed(() => Math.max(1, ...sizes.value.map((row) => row.quantity)))
const months = computed(() => {
  const source = [...(props.sales.monthly || [])]
    .filter((row) => /^\d{4}-\d{2}$/.test(row.month))
    .sort((a, b) => a.month.localeCompare(b.month))
  if (!source.length) return []
  const toIndex = (s: string) => Number(s.slice(0, 4)) * 12 + Number(s.slice(5, 7)) - 1
  const first = toIndex(source[0]!.month)
  const last = toIndex(source.at(-1)!.month)
  const lookup = new Map(source.map((row) => [row.month, row]))
  return Array.from({ length: last - first + 1 }, (_, i) => {
    const index = first + i
    const month = `${Math.floor(index / 12)}-${String((index % 12) + 1).padStart(2, '0')}`
    return lookup.get(month) || { month, order_quantity: 0, replenishment_quantity: 0 }
  })
})
const monthTotal = computed(() =>
  months.value.reduce((sum, row) => sum + row.order_quantity + row.replenishment_quantity, 0),
)
const monthMax = computed(() => {
  const max = Math.max(
    1,
    ...months.value.map((row) => row.order_quantity + row.replenishment_quantity),
  )
  const step = 10 ** Math.floor(Math.log10(max))
  return Math.ceil(max / step) * step
})
const heatMax = computed(() =>
  Math.max(1, ...(props.sales.heatmap?.rows || []).flatMap((row) => row.quantities)),
)
const customers = computed(() =>
  [...(props.sales.top_customers || [])].sort((a, b) => b.quantity - a.quantity).slice(0, 5),
)
const people = computed(() =>
  [...(props.sales.salespeople || [])].sort((a, b) => b.quantity - a.quantity),
)
function heatStyle(value: number) {
  return value > 0
    ? { backgroundColor: `rgba(168, 139, 94, ${0.09 + (0.49 * value) / heatMax.value})` }
    : {}
}
function share(value: number, max: number) {
  return `${(Math.max(0, value) / Math.max(1, max)) * 100}%`
}
</script>

<template>
  <div class="sales-analysis">
    <div class="analysis-grid">
      <section class="analytics-card monthly-card" aria-labelledby="monthly-title">
        <header>
          <h3 id="monthly-title">月度下单走势</h3>
          <div class="chart-legend">
            <span><i class="order-key"></i>首单</span><span><i class="gold-key"></i>补单</span>
          </div>
        </header>
        <template v-if="months.length">
          <p class="chart-note">
            有日期的订单明细 · {{ number(monthTotal) }} 件<template
              v-if="monthTotal !== sales.total_quantity"
              >，历史汇总未计入走势</template
            >
          </p>
          <div class="chart-scroll" tabindex="0" aria-label="月度下单图表，可横向滚动">
            <div
              class="monthly-chart"
              :style="{ minWidth: `${Math.max(400, months.length * 44)}px` }"
            >
              <div class="chart-y-axis">
                <span>{{ number(monthMax) }}</span
                ><span>{{ number(monthMax / 2) }}</span
                ><span>0</span>
              </div>
              <div
                class="month-columns"
                :style="{ gridTemplateColumns: `repeat(${months.length}, 1fr)` }"
                role="img"
                :aria-label="`月度下单走势，共 ${number(monthTotal)} 件，明细见下方数据表`"
              >
                <div
                  v-for="(row, index) in months"
                  :key="row.month"
                  class="plot-column"
                  :title="`${row.month}：首单 ${number(row.order_quantity)} 件，补单 ${number(row.replenishment_quantity)} 件`"
                >
                  <div
                    class="stacked-column"
                    :style="{
                      height: share(row.order_quantity + row.replenishment_quantity, monthMax),
                    }"
                  >
                    <span
                      class="replenish-column"
                      :style="{ flex: Math.max(0, row.replenishment_quantity) }"
                    ></span>
                    <span
                      class="order-column"
                      :style="{ flex: Math.max(0, row.order_quantity) }"
                    ></span>
                  </div>
                  <span class="month-label"
                    >{{ row.month.slice(5) }}月<small
                      v-if="index === 0 || row.month.endsWith('-01')"
                      >{{ row.month.slice(0, 4) }}</small
                    ></span
                  >
                </div>
              </div>
            </div>
          </div>
          <p class="mobile-chart-hint">左右滑动查看完整月份</p>
          <details class="chart-data">
            <summary>查看月度数据</summary>
            <div class="chart-scroll">
              <table>
                <caption class="visually-hidden">
                  每月首单与补单件数
                </caption>
                <thead>
                  <tr>
                    <th scope="col">月份</th>
                    <th scope="col">首单</th>
                    <th scope="col">补单</th>
                    <th scope="col">合计</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="row in months" :key="row.month">
                    <th scope="row">{{ row.month }}</th>
                    <td>{{ number(row.order_quantity) }}</td>
                    <td>{{ number(row.replenishment_quantity) }}</td>
                    <td>{{ number(row.order_quantity + row.replenishment_quantity) }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </details>
        </template>
        <p v-else class="chart-empty">暂无带日期的订单，导入订单明细后可查看走势。</p>
      </section>

      <section class="analytics-card" aria-labelledby="colors-title">
        <header>
          <h3 id="colors-title">颜色销量占比</h3>
          <span class="chart-note">按件数</span>
        </header>
        <div v-if="colors.length && colorTotal" class="color-chart">
          <div v-if="!hasNegativeColors" class="donut-container">
            <svg
              viewBox="0 0 180 180"
              role="img"
              :aria-label="`颜色销量占比，总计 ${number(colorTotal)} 件，比例详见右侧列表`"
            >
              <circle
                cx="90"
                cy="90"
                r="65"
                fill="none"
                stroke="var(--divider)"
                stroke-width="23"
              />
              <g transform="rotate(-90 90 90)">
                <circle
                  v-for="row in colors"
                  :key="row.name"
                  cx="90"
                  cy="90"
                  r="65"
                  fill="none"
                  :stroke="row.color"
                  stroke-width="23"
                  pathLength="100"
                  :stroke-dasharray="`${row.share} ${100 - row.share}`"
                  :stroke-dashoffset="-row.start"
                >
                  <title>
                    {{ row.name }}：{{ number(row.quantity) }} 件，{{ row.share.toFixed(1) }}%
                  </title>
                </circle>
              </g>
            </svg>
            <div class="donut-total" aria-hidden="true">
              <strong>{{ number(colorTotal) }}</strong
              ><span>总件数</span>
            </div>
          </div>
          <p v-else class="chart-note">包含负数销量，请以明细为准。</p>
          <ul class="color-legend">
            <li v-for="row in colors" :key="row.name">
              <span class="color-label"
                ><i :style="{ background: row.color }"></i>{{ row.name }}</span
              ><span>{{ hasNegativeColors ? '—' : `${row.share.toFixed(1)}%` }}</span
              ><strong>{{ number(row.quantity) }}</strong>
            </li>
          </ul>
        </div>
        <p v-else class="chart-empty">暂无颜色销量数据。</p>
      </section>

      <section class="analytics-card" aria-labelledby="heat-title">
        <header>
          <h3 id="heat-title">颜色 × 尺码</h3>
          <span class="chart-note">颜色越深，件数越多</span>
        </header>
        <div
          v-if="sales.heatmap?.rows.length"
          class="chart-scroll"
          tabindex="0"
          aria-label="颜色与尺码销量表，可横向滚动"
        >
          <table class="sales-heatmap">
            <caption class="visually-hidden">
              各颜色和尺码的销量件数
            </caption>
            <thead>
              <tr>
                <th scope="col">颜色</th>
                <th v-for="size in sales.heatmap.sizes" :key="size" scope="col" :title="size">
                  {{ sizeLabel(size) }}
                </th>
                <th scope="col">合计</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in sales.heatmap.rows" :key="row.color">
                <th scope="row">
                  <i
                    class="heat-swatch"
                    :style="{ background: palette[row.family || row.color] || '#99918c' }"
                  ></i
                  >{{ row.color }}
                </th>
                <td v-for="(value, i) in row.quantities" :key="i" :style="heatStyle(value)">
                  {{ value ? number(value) : '—' }}
                </td>
                <td>{{ number(row.total) }}</td>
              </tr>
            </tbody>
            <tfoot>
              <tr>
                <th scope="row">合计</th>
                <td v-for="(value, i) in sales.heatmap.totals" :key="i">{{ number(value) }}</td>
                <td>{{ number(sales.heatmap.total) }}</td>
              </tr>
            </tfoot>
          </table>
        </div>
        <p v-if="sales.heatmap?.rows.length" class="mobile-chart-hint">左右滑动查看完整尺码</p>
        <p v-if="!sales.heatmap?.rows.length" class="chart-empty">暂无颜色与尺码的交叉数据。</p>
      </section>

      <section class="analytics-card" aria-labelledby="sizes-title">
        <header>
          <h3 id="sizes-title">尺码销量</h3>
          <span class="chart-note">按尺码顺序</span>
        </header>
        <div
          v-if="sizes.length"
          class="chart-scroll"
          tabindex="0"
          aria-label="尺码销量，可横向滚动"
        >
          <div
            class="size-columns"
            :style="{ gridTemplateColumns: `repeat(${sizes.length}, minmax(38px, 1fr))` }"
          >
            <div
              v-for="row in sizes"
              :key="row.name"
              class="size-column"
              :title="`${row.name}：${number(row.quantity)} 件`"
            >
              <span class="size-quantity">{{ number(row.quantity) }}</span
              ><span
                class="size-column-fill"
                :style="{ height: share(row.quantity, sizeMax) }"
              ></span
              ><span class="size-label">{{ sizeLabel(row.name) }}</span>
            </div>
          </div>
        </div>
        <p v-else class="chart-empty">暂无尺码销量数据。</p>
      </section>
    </div>

    <div class="rankings-grid">
      <section class="analytics-card" aria-labelledby="customers-title">
        <header>
          <h3 id="customers-title">下单客户 TOP {{ customers.length }}</h3>
          <button type="button" class="chart-link" @click="emit('orders')">查看订单</button>
        </header>
        <ol v-if="customers.length" class="ranking-list">
          <li v-for="(row, index) in customers" :key="row.customer">
            <span class="rank-index" :class="{ 'top-rank': index < 3 }">{{ index + 1 }}</span
            ><span class="rank-name" :title="row.customer">{{ row.customer }}</span
            ><span class="rank-track"
              ><span :style="{ width: share(row.quantity, customers[0]!.quantity) }"></span></span
            ><span class="rank-value">{{ number(row.quantity) }}<small> 件</small></span>
          </li>
        </ol>
        <p v-else class="chart-empty">暂无客户订单明细。</p>
      </section>
      <section class="analytics-card" aria-labelledby="people-title">
        <header>
          <h3 id="people-title">业务员排名</h3>
          <span class="chart-note">按件数</span>
        </header>
        <ol v-if="people.length" class="ranking-list">
          <li v-for="(row, index) in people" :key="row.name">
            <span class="rank-index" :class="{ 'top-rank': index < 3 }">{{ index + 1 }}</span
            ><span class="rank-name" :title="row.name">{{ row.name }}</span
            ><span class="rank-track"
              ><span :style="{ width: share(row.quantity, people[0]!.quantity) }"></span></span
            ><span class="rank-value">{{ number(row.quantity) }}<small> 件</small></span>
          </li>
        </ol>
        <p v-else class="chart-empty">暂无业务员订单明细。</p>
      </section>
    </div>
  </div>
</template>

<style scoped>
.mobile-chart-hint {
  display: none;
}
@media (max-width: 600px) {
  .mobile-chart-hint {
    display: block;
    font-size: 11px;
    color: var(--muted);
    margin: 8px 0 0;
  }
}
.sales-analysis {
  font-variant-numeric: tabular-nums;
}
.analysis-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.35fr) minmax(0, 1fr);
  gap: 18px;
  align-items: stretch;
}
.analytics-card {
  min-width: 0;
  padding: 22px 24px;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
}
.analytics-card header {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  align-items: center;
  margin-bottom: 20px;
  flex-wrap: wrap;
}
.analytics-card h3 {
  font-size: 16px;
  font-weight: 600;
  margin: 0;
  color: var(--ink);
}
.chart-note {
  color: var(--muted);
  font-size: 12px;
  margin: 0;
}
.monthly-card > .chart-note {
  margin: -10px 0 18px;
}
.chart-legend,
.chart-legend span {
  display: flex;
  align-items: center;
  gap: 7px;
  font-size: 12px;
  color: var(--muted);
}
.chart-legend {
  gap: 18px;
}
.chart-legend i {
  width: 10px;
  height: 10px;
  border-radius: 2px;
}
.order-key {
  background: var(--ink);
}
.gold-key {
  background: var(--gold);
}
.chart-scroll {
  overflow-x: auto;
  padding-bottom: 3px;
  scrollbar-width: thin;
}
.monthly-chart {
  display: grid;
  grid-template-columns: 38px 1fr;
  padding: 0 0 42px;
}
.chart-y-axis {
  height: 155px;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  font-size: 11px;
  color: var(--muted);
  padding-right: 8px;
  text-align: right;
}
.month-columns {
  height: 155px;
  display: grid;
  gap: 10px;
  border-bottom: 1px solid var(--line);
  background: repeating-linear-gradient(
    to top,
    transparent 0,
    transparent calc(50% - 1px),
    var(--divider) calc(50% - 1px),
    var(--divider) 50%
  );
}
.plot-column {
  height: 100%;
  position: relative;
  display: flex;
  align-items: end;
  justify-content: center;
}
.stacked-column {
  display: flex;
  flex-direction: column;
  width: min(100%, 32px);
  border-radius: 2px 2px 0 0;
  overflow: hidden;
}
.order-column {
  background: var(--ink);
}
.replenish-column {
  background: var(--gold);
}
.month-label {
  position: absolute;
  top: calc(100% + 8px);
  width: 100%;
  text-align: center;
  font-size: 11px;
  color: var(--muted);
  white-space: nowrap;
}
.month-label small {
  display: block;
  font-size: 10px;
  color: var(--muted-2);
}
.chart-data {
  color: var(--muted);
  font-size: 12px;
  margin-top: 4px;
}
.chart-data summary {
  cursor: pointer;
  width: fit-content;
}
.chart-data table {
  width: 100%;
  border-collapse: collapse;
  margin-top: 10px;
}
.chart-data th,
.chart-data td {
  text-align: right;
  padding: 6px;
  border-bottom: 1px solid var(--divider);
  white-space: nowrap;
}
.chart-data th:first-child {
  text-align: left;
}
.color-chart {
  display: flex;
  align-items: center;
  gap: 20px;
  min-height: 218px;
}
.donut-container {
  position: relative;
  flex: 0 1 180px;
  min-width: 130px;
}
.donut-container svg {
  display: block;
  width: 100%;
}
.donut-total {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-direction: column;
  pointer-events: none;
}
.donut-total strong {
  font-size: 25px;
  font-weight: 650;
  line-height: 1.3;
}
.donut-total span {
  font-size: 11px;
  color: var(--muted);
}
.color-legend {
  padding: 0;
  margin: 0;
  list-style: none;
  flex: 1;
  min-width: 0;
}
.color-legend li {
  display: grid;
  grid-template-columns: 1fr auto auto;
  gap: 10px;
  padding: 8px 0;
  border-bottom: 1px solid var(--divider);
  font-size: 12px;
  align-items: center;
}
.color-legend strong {
  font-weight: 500;
  min-width: 36px;
  text-align: right;
}
.color-legend li > span:nth-child(2) {
  color: var(--muted);
}
.color-label {
  display: flex;
  align-items: center;
  gap: 7px;
  white-space: nowrap;
}
.color-label i,
.heat-swatch {
  width: 9px;
  height: 9px;
  border-radius: 2px;
  display: inline-block;
  flex-shrink: 0;
  box-shadow: inset 0 0 0 1px #0000000d;
}
.heat-swatch {
  margin-right: 7px;
}
.sales-heatmap {
  width: 100%;
  min-width: 480px;
  border-collapse: collapse;
  font-size: 12px;
}
.sales-heatmap th,
.sales-heatmap td {
  text-align: right;
  white-space: nowrap;
  padding: 10px 8px;
  border-bottom: 1px solid var(--divider);
}
.sales-heatmap th {
  font-weight: 500;
}
.sales-heatmap th:first-child {
  text-align: left;
  position: sticky;
  left: 0;
  background: var(--panel);
}
.sales-heatmap thead th {
  color: var(--muted);
  font-size: 11px;
}
.sales-heatmap tfoot {
  font-weight: 600;
  background: var(--search);
}
.sales-heatmap tfoot th {
  background: var(--search);
}
.size-columns {
  display: grid;
  gap: 10px;
  height: 225px;
  padding: 30px 3px 30px;
  align-items: end;
  border-bottom: 1px solid var(--line);
  background: repeating-linear-gradient(
    to top,
    transparent 0,
    transparent calc(25% - 1px),
    var(--divider) calc(25% - 1px),
    var(--divider) 25%
  );
}
.size-column {
  height: 100%;
  display: flex;
  flex-direction: column;
  justify-content: end;
  align-items: center;
  position: relative;
}
.size-column-fill {
  width: min(100%, 42px);
  background: var(--gold);
  border-radius: 2px 2px 0 0;
  flex-shrink: 0;
}
.size-quantity {
  margin-bottom: 5px;
  font-size: 11px;
}
.size-label {
  position: absolute;
  top: calc(100% + 8px);
  font-size: 11px;
  color: var(--muted);
}
.rankings-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 18px;
  margin-top: 18px;
  align-items: stretch;
}
.ranking-list {
  list-style: none;
  padding: 0;
  margin: 0;
}
.ranking-list li {
  display: grid;
  grid-template-columns: 20px minmax(65px, 100px) 1fr 64px;
  align-items: center;
  gap: 10px;
  margin: 12px 0;
  font-size: 13px;
}
.rank-index {
  width: 18px;
  height: 18px;
  border-radius: 50%;
  display: grid;
  place-items: center;
  font-size: 10px;
  background: var(--placeholder);
  color: var(--muted);
}
.top-rank {
  background: #dbc49f;
  color: #503c20;
}
.rank-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.rank-track {
  height: 7px;
  background: var(--placeholder);
  border-radius: 4px;
  overflow: hidden;
}
.rank-track > span {
  display: block;
  background: var(--gold);
  border-radius: inherit;
  height: 100%;
}
.rank-value {
  text-align: right;
  white-space: nowrap;
}
.rank-value small {
  font-size: 11px;
  color: var(--muted);
}
.chart-link {
  color: var(--link);
  padding: 0;
  background: none;
  border: none;
  cursor: pointer;
  font-size: 12px;
}
.chart-link:hover {
  text-decoration: underline;
}
.chart-empty {
  font-size: 13px;
  color: var(--muted);
  padding: 36px 0;
  margin: 0;
}
@media (max-width: 1100px) {
  .color-chart {
    gap: 10px;
  }
  .analytics-card {
    padding: 18px;
  }
  .donut-container {
    min-width: 105px;
  }
  .donut-total strong {
    font-size: 21px;
  }
  .color-legend li {
    gap: 6px;
  }
}
@media (max-width: 900px) {
  .analysis-grid {
    grid-template-columns: 1fr;
  }
  .color-chart {
    min-height: 180px;
    gap: 32px;
  }
  .donut-container {
    flex-basis: 180px;
  }
  .ranking-list li {
    grid-template-columns: 18px minmax(55px, 85px) 1fr 60px;
    gap: 7px;
  }
}
@media (max-width: 600px) {
  .rankings-grid {
    grid-template-columns: 1fr;
  }
  .analytics-card {
    padding: 18px 14px;
  }
  .color-chart {
    gap: 14px;
  }
  .donut-container {
    flex-basis: 145px;
    min-width: 110px;
  }
  .color-legend li {
    font-size: 11px;
    gap: 5px;
  }
  .analytics-card header {
    gap: 8px;
  }
  .sales-heatmap {
    min-width: 460px;
  }
}
</style>
