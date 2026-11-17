<script setup lang="ts">
import { computed } from 'vue'
import type { SalesImportImpact } from '../api'

const props = defineProps<{ impact: SalesImportImpact; estimated?: boolean }>()
const orders = computed(() => props.impact.row_kind === 'orders')
const primary = computed(() => orders.value ? props.impact.orders : props.impact.counted)
const countedDiffers = computed(() => orders.value && (
  props.impact.counted.delta.quantity !== props.impact.orders.delta.quantity ||
  props.impact.counted.delta.amount !== props.impact.orders.delta.amount))
function number(value: number, money = false) {
  return value.toLocaleString('zh-CN', { minimumFractionDigits: money ? 2 : 0, maximumFractionDigits: money ? 2 : 0 })
}
function change(value: number, money = false) {
  if (value === 0) return '无变化'
  return `${value > 0 ? '净增加' : '净减少'} ${money ? '¥' : ''}${number(Math.abs(value), money)}${money ? '' : ' 件'}`
}
</script>

<template>
  <section class="sales-import-impact" aria-label="本次导入净变化">
    <h4>{{ estimated ? '预计导入影响' : '本次导入实际影响' }}</h4>
    <p class="meta">{{ orders ? '订单明细净变化' : '图库计入合计净变化' }} · 全部季节</p>
    <div class="impact-values"><p><span>件数</span><strong>{{ change(primary.delta.quantity) }}</strong></p><p><span>金额</span><strong>{{ change(primary.delta.amount, true) }}</strong></p></div>
    <p v-if="countedDiffers">汇总计入范围同时发生变化，图库计入合计{{ change(impact.counted.delta.quantity) }}，金额{{ change(impact.counted.delta.amount, true) }}。</p>
    <p v-if="impact.summaries_deactivated" class="meta">{{ impact.summaries_deactivated }} 条原先计入的汇总转为核对用，原始汇总仍保留。</p>
    <p v-if="impact.summaries_reactivated" class="meta">{{ impact.summaries_reactivated }} 条汇总按现有规则计入累计。</p>
    <p class="meta">本表未涉及的 {{ number(impact.retained_rows) }} 条已有{{ orders ? '订单明细' : '汇总记录' }}继续保留（全库范围）。</p>
    <p class="meta">净变化是导入前后的差额，和 Excel 文件合计不同。{{ orders ? '这里只统计订单明细；数据分析仍按所选订单季节和下单日期统计。' : '汇总按现有规则计入图库累计，不写入订单明细，不计入数据分析。' }}</p>
    <details><summary>查看导入前后合计（全库、全部季节）</summary><p>{{ orders ? '订单明细' : '图库计入合计' }}：导入前 {{ number(primary.before.quantity) }} 件 / ¥{{ number(primary.before.amount, true) }}；导入后 {{ number(primary.after.quantity) }} 件 / ¥{{ number(primary.after.amount, true) }}。</p></details>
  </section>
</template>
