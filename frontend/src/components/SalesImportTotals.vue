<script setup lang="ts">
import { computed } from 'vue'
import type { TableReport } from '../api'
import SalesImportImpact from './SalesImportImpact.vue'

const props = defineProps<{ report: TableReport; historical?: boolean }>()
function valid(value: unknown): value is number { return typeof value === 'number' && Number.isFinite(value) }
const hasFileTotals = computed(() => valid(props.report.parsed_quantity) && valid(props.report.parsed_amount))
const hasStyleTotals = computed(() => valid(props.report.stored_quantity) && valid(props.report.stored_amount))
function quantity(value: number | undefined) { return value?.toLocaleString('zh-CN') }
function amount(value: number | undefined) { return value?.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) }
</script>

<template>
  <div class="sales-import-totals">
    <p v-if="hasFileTotals">本批 Excel 有效数据合计：<strong>{{ quantity(report.parsed_quantity) }} 件</strong>，金额 <strong>¥{{ amount(report.parsed_amount) }}</strong>。</p>
    <p v-else class="meta">该历史批次未保存文件合计，无法准确展示；关联款号累计不能代替本批合计。</p>
    <p v-if="hasFileTotals" class="meta">仅统计本文件的有效记录，包含新增、更新和无变化记录，不代表新增订单件数。</p>
    <p v-if="historical" class="meta">以上为该次导入时的记录，后续修改或删除订单数据不会改变这份历史合计。</p>
    <SalesImportImpact v-if="report.impact" :impact="report.impact" />
    <p v-else-if="historical" class="meta">该历史批次未记录导入前后净变化，不能用文件合计推算。</p>
    <details v-if="hasStyleTotals">
      <summary>查看关联款号累计（跨批次）</summary>
      <p>截至该次导入完成，相关款号累计计入 {{ quantity(report.stored_quantity) }} 件，金额 ¥{{ amount(report.stored_amount) }}。</p>
      <p class="meta">包括这些款号在其他文件、季节中的已计入订单件数，不能用于核对本批 Excel 合计。</p>
    </details>
  </div>
</template>
