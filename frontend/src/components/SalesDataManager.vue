<script setup lang="ts">
import { nextTick, ref } from 'vue'
import { deleteSalesData, restoreSalesData, previewSalesRestoration, type SalesRestorePreview, getSalesData, getSalesDeletions, previewSalesDeletion, type SalesDeletePreview, type SalesDeletionLog, type SalesRows, type SalesSelection, type TableImportResult } from '../api'
const props = defineProps<{ history: TableImportResult[] }>()
const emit = defineEmits<{ changed: [] }>()
const opened = ref(false)
const heading = ref<HTMLElement | null>(null)
const batch = ref('')
const query = ref('')
const page = ref(1)
const data = ref<SalesRows | null>(null)
const selected = ref(new Set<string>())
const pending = ref<{ selection: SalesSelection; plan: SalesDeletePreview } | null>(null)
const acknowledged = ref(false)
const logs = ref<SalesDeletionLog[]>([])
const restorePending = ref<SalesRestorePreview | null>(null)
const restoreHeading = ref<HTMLElement | null>(null)
const logPage = ref(1)
const moreLogs = ref(false)
const logsLoading = ref(false)
const busy = ref(false)
const loading = ref(false)
const error = ref('')
const logError = ref('')
const notice = ref('')
let latest = 0
function message(e: unknown) { return e instanceof Error ? e.message : String(e) }
function money(value: number) { return value.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) }
function time(value: string) { return new Date(value).toLocaleString('zh-CN', { hour12: false }) }
function backupUrl(id: string) { return `/api/import/sales-deletions/${encodeURIComponent(id)}/backup` }
async function loadLogs(more = false) {
  if (logsLoading.value) return
  logsLoading.value = true
  const nextPage = more ? logPage.value + 1 : 1
  try {
    const result = await getSalesDeletions(nextPage)
    logs.value = more ? [...logs.value, ...result] : result
    logPage.value = nextPage; moreLogs.value = result.length === 20; logError.value = ''
  } catch (e) { logError.value = message(e) }
  finally { logsLoading.value = false }
}
async function load(reset = false) {
  if (reset) { page.value = 1; selected.value = new Set() }
  const sequence = ++latest
  loading.value = true; error.value = ''; pending.value = null
  try {
    const result = await getSalesData(batch.value, query.value, page.value)
    if (sequence === latest) data.value = result
  } catch (e) { if (sequence === latest) { data.value = null; error.value = message(e) } }
  finally { if (sequence === latest) loading.value = false }
}
async function open(batchId = '') {
  if (busy.value) return
  opened.value = true; batch.value = batchId; query.value = ''; notice.value = ''; restorePending.value = null
  void loadLogs(); void load(true)
  await nextTick(); heading.value?.focus(); heading.value?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}
function close() { opened.value = false; latest++; pending.value = null; restorePending.value = null }
function toggle(id: string) {
  const next = new Set(selected.value)
  if (next.has(id)) next.delete(id); else next.add(id)
  selected.value = next; pending.value = null
}
function selectPage() {
  selected.value = new Set([...selected.value, ...(data.value?.items.map(row => row.id) || [])])
  pending.value = null
}
async function preview(scope: SalesSelection['scope']) {
  const selection = { scope, batch_id: scope === 'all' ? '' : batch.value, ids: scope === 'selected' ? [...selected.value] : [] }
  busy.value = true; error.value = ''; notice.value = ''; pending.value = null; restorePending.value = null; acknowledged.value = false
  try { pending.value = { selection, plan: await previewSalesDeletion(selection) } }
  catch (e) { error.value = message(e) }
  finally { busy.value = false }
}
async function confirm() {
  if (!pending.value || !acknowledged.value) return
  busy.value = true; error.value = ''
  try {
    const clearingAll = pending.value.selection.scope === 'all'
    const result = await deleteSalesData(pending.value.selection, pending.value.plan.token)
    pending.value = null; acknowledged.value = false
    if (clearingAll) { batch.value = ''; query.value = '' }
    notice.value = clearingAll
      ? `已清空全部 ${result.deleted} 条订单明细与历史汇总，可重新上传完整 Excel。图片和款式档案已保留，删除备份可在下方恢复或下载。`
      : `已删除 ${result.deleted} 条订单记录。删除备份已保存，可在下方一键恢复或下载。`
    emit('changed'); await Promise.all([load(true), loadLogs()])
  } catch (e) { error.value = message(e); pending.value = null; acknowledged.value = false }
  finally { busy.value = false }
}
async function previewRestore(id: string) {
  busy.value = true; error.value = ''; notice.value = ''; pending.value = null; restorePending.value = null
  try {
    restorePending.value = await previewSalesRestoration(id)
    await nextTick(); restoreHeading.value?.focus(); restoreHeading.value?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  } catch (e) { error.value = message(e) }
  finally { busy.value = false }
}
async function confirmRestore() {
  if (!restorePending.value || busy.value) return
  busy.value = true; error.value = ''
  try {
    const result = await restoreSalesData(restorePending.value.backup_id, restorePending.value.token)
    restorePending.value = null
    notice.value = result.already_restored ? '这份备份已恢复，无需重复操作。' : `已恢复 ${result.restored} 条订单记录，保留 ${result.skipped_existing} 条现有记录。`
    emit('changed'); await Promise.all([load(true), loadLogs()])
  } catch (e) { error.value = message(e); restorePending.value = null }
  finally { busy.value = false }
}
defineExpose({ open })
</script>

<template>
  <section v-if="opened" class="sales-manager workspace-panel" aria-label="管理订单数据">
    <div class="section-heading"><h3 ref="heading" tabindex="-1">管理订单数据</h3><button class="text-button" :disabled="busy" @click="close">收起</button></div>
    <p class="meta">可按导入批次删除，或搜索订单、款号后勾选明细。图片、标签和款式档案会保留。</p>
    <form class="sales-filters" @submit.prevent="load(true)">
      <label>导入批次<select v-model="batch" :disabled="busy || loading" @change="load(true)"><option value="">全部订单数据</option><option v-for="item in props.history.filter(item => item.kind === 'sales')" :key="item.id" :value="item.id">{{ item.source_file }} · {{ time(item.created_at) }}</option></select></label>
      <label>查找记录<input v-model="query" :disabled="busy || loading" type="search" placeholder="订单号、款号、客户或文件名" /></label>
      <button class="btn" :disabled="busy || loading">查询 / 刷新</button>
    </form>
    <p v-if="data?.batch?.protected" class="workspace-notice">本批有 {{ data.batch.protected }} 条记录已更新、已删除或无法准确关联，这些记录不会随本批删除。</p>
    <p v-if="error" class="error" role="alert">{{ error }}</p><p v-if="notice" class="workspace-notice" role="status">{{ notice }}</p>
    <p v-if="loading" role="status">正在读取订单记录…</p>
    <template v-else-if="data">
      <div class="toolbar"><span class="meta">匹配 {{ data.total }} 条，已选 {{ selected.size }} 条</span><button class="btn small" :disabled="busy || !data.items.length" @click="selectPage">选择本页</button><button class="text-button" :disabled="busy || !selected.size" @click="selected = new Set(); pending = null">清空选择</button></div>
      <div class="sales-table-scroll">
        <table class="sales-table"><thead><tr><th>选择</th><th>订单 / 类型</th><th>款号 / 客户</th><th>颜色 / 尺码</th><th>数量</th><th>金额</th><th>来源文件</th></tr></thead>
          <tbody><tr v-for="row in data.items" :key="row.id"><td><input type="checkbox" :checked="selected.has(row.id)" :disabled="busy" :aria-label="`选择 ${row.order_no || '汇总'} ${row.variant_code} ${row.color} ${row.size_label}`" @change="toggle(row.id)" /></td><td>{{ row.order_no || '历史汇总' }}<small>{{ row.order_date || [row.year, row.period].filter(Boolean).join(' ') }}</small></td><td>{{ row.variant_code }}<small>{{ row.customer }}</small></td><td>{{ row.color }} / {{ row.size_label }}</td><td>{{ row.quantity.toLocaleString() }}</td><td>¥{{ money(row.amount) }}</td><td>{{ row.source_file }}</td></tr></tbody>
        </table>
      </div>
      <p v-if="!data.total" class="workspace-empty">没有匹配的可删记录。本批未变化或已被后续导入更新的数据不会出现在这里。</p>
      <div class="toolbar"><button class="btn small" :disabled="busy || page <= 1" @click="page--; load()">上一页</button><span class="meta">{{ page }} / {{ Math.max(1, Math.ceil(data.total / data.page_size)) }}</span><button class="btn small" :disabled="busy || page * data.page_size >= data.total" @click="page++; load()">下一页</button></div>
      <div class="toolbar sales-actions"><button class="btn media-delete" :disabled="busy || !selected.size" @click="preview('selected')">预览删除已选 {{ selected.size }} 条</button><button v-if="batch" class="btn media-delete" :disabled="busy" @click="preview('batch')">预览删除本批数据</button></div>
    </template>
    <section class="sales-reset" aria-label="重新导入全部订单数据">
      <h4>重新导入全部订单数据</h4>
      <p class="meta">准备从完整 Excel 重新开始时，可先清空所有年份、季节和客户的订单明细与历史汇总。图片、标签、款式档案和导入历史会保留，删除前会自动保存恢复备份。</p>
      <button class="btn media-delete" :disabled="busy || loading" @click="preview('all')">预览清空全部订单数据</button>
    </section>
    <section v-if="pending" class="sales-confirm" aria-label="删除影响预览">
      <h4>{{ pending.selection.scope === 'all' ? '清空全部订单数据 · 影响预览' : '删除影响预览' }}</h4>
      <p v-if="pending.selection.scope === 'all'" class="workspace-notice">范围是全库订单明细与历史汇总，包含所有年份、季节和客户，不受上方批次、搜索条件或分页限制。</p>
      <p>将删除 {{ pending.plan.row_count }} 条记录：订单明细 {{ pending.plan.order_rows }} 条，历史汇总 {{ pending.plan.summary_rows }} 条。涉及 {{ pending.plan.style_codes.length }} 个款号。</p>
      <p class="meta">{{ pending.plan.style_codes.slice(0, 15).join('、') }}{{ pending.plan.style_codes.length > 15 ? ' 等' : '' }}</p>
      <p>相关款式计入订单件数：<strong>{{ pending.plan.before.quantity.toLocaleString() }} → {{ pending.plan.after.quantity.toLocaleString() }} 件</strong><br />计入金额：<strong>¥{{ money(pending.plan.before.amount) }} → ¥{{ money(pending.plan.after.amount) }}</strong></p>
      <p v-if="pending.plan.reactivated_summary_rows" class="workspace-notice">删除后有 {{ pending.plan.reactivated_summary_rows }} 条历史汇总重新参与统计，上面的结果已包含这些汇总。</p>
      <p v-if="pending.plan.batch?.protected" class="meta">本批另有 {{ pending.plan.batch.protected }} 条已变化或无法关联的记录，会跳过。</p>
      <p class="meta">会保存被删记录的备份。此操作删除当前订单数据，不会自动恢复被早前导入覆盖的旧数值。</p>
      <label class="sales-ack"><input v-model="acknowledged" type="checkbox" :disabled="busy" />{{ pending.selection.scope === 'all' ? '我已备份，确认清空全部订单明细与历史汇总（所有年份、季节和客户）。' : '我已核对以上范围和统计变化，确认删除这些订单记录。' }}</label>
      <div class="toolbar"><button class="btn media-delete" :disabled="busy || loading || !acknowledged" @click="confirm">{{ busy ? '正在删除…' : pending.selection.scope === 'all' ? `确认清空全部 ${pending.plan.row_count} 条` : `确认删除 ${pending.plan.row_count} 条` }}</button><button class="btn" :disabled="busy" @click="pending = null">取消</button></div>
    </section>
    <details class="sales-backups">
      <summary>删除记录与恢复（已加载 {{ logs.length }} 条）</summary>
      <p class="meta">可恢复通过订单管理删除并保存备份的数据。每份备份成功恢复一次；已有订单或汇总会保留，不会覆盖。</p>
      <p v-if="logError" class="error">{{ logError }}</p>
      <button class="text-button" :disabled="busy || logsLoading" @click="loadLogs()">刷新删除记录</button>
      <p v-if="!logs.length && !logsLoading" class="meta">尚无删除记录。</p>
      <ul><li v-for="item in logs" :key="item.id">
        <span>{{ time(item.created_at) }} · {{ item.source_file }} · {{ item.row_count }} 条
          <small v-if="item.restored_at">{{ time(item.restored_at) }} 已恢复 {{ item.restored_rows }} 条，保留现有 {{ item.skipped_rows }} 条</small>
        </span>
        <div class="toolbar"><button v-if="!item.restored_at" class="btn small" :disabled="busy || loading" @click="previewRestore(item.id)">一键恢复</button><span v-else class="meta">已恢复</span><a :href="backupUrl(item.id)" class="text-button" download>下载删除备份</a></div>
      </li></ul>
      <button v-if="moreLogs" class="btn small" :disabled="busy || logsLoading" @click="loadLogs(true)">加载更早记录</button>
      <section v-if="restorePending" class="sales-confirm" aria-label="恢复影响预览">
        <h4 ref="restoreHeading" tabindex="-1">恢复影响预览</h4>
        <p>备份共 {{ restorePending.row_count }} 条，可恢复 <strong>{{ restorePending.restore_count }} 条</strong>；已存在 {{ restorePending.skipped_existing }} 条，将保留现有数据。</p>
        <p v-if="restorePending.conflicting_rows" class="workspace-notice">{{ restorePending.conflicting_rows }} 条现有记录与备份不同，可能已重新导入或修改，这些记录不会被覆盖。</p>
        <p v-if="restorePending.conflicts.length" class="meta">保留：{{ restorePending.conflicts.map(row => [row.order_no || '汇总', row.variant_code, row.color, row.size_label].filter(Boolean).join(' / ')).join('；') }}{{ restorePending.conflicting_rows > 10 ? ' 等' : '' }}</p>
        <p>相关款式计入订单件数：<strong>{{ restorePending.before.quantity.toLocaleString() }} → {{ restorePending.after.quantity.toLocaleString() }} 件</strong><br />计入金额：<strong>¥{{ money(restorePending.before.amount) }} → ¥{{ money(restorePending.after.amount) }}</strong></p>
        <p v-if="restorePending.suppressed_summary_rows" class="meta">恢复订单后，{{ restorePending.suppressed_summary_rows }} 条历史汇总转为参考，不再重复计入订单件数。上面的结果已包含这一变化。</p>
        <p class="meta">图片、标签和款式档案保留不变。</p>
        <div class="toolbar"><button class="btn primary" :disabled="busy || !restorePending.restore_count" @click="confirmRestore">{{ busy ? '正在恢复…' : `确认恢复 ${restorePending.restore_count} 条` }}</button><button class="btn" :disabled="busy" @click="restorePending = null">取消</button></div>
      </section>
    </details>
  </section>
</template>

<style scoped>
.sales-manager { margin: 26px 0; scroll-margin-top: 20px; }
.sales-filters { display: flex; flex-wrap: wrap; align-items: end; gap: 12px; margin: 20px 0; }
.sales-filters label { display: grid; gap: 7px; flex: 1 1 220px; min-width: 0; font-size: 13px; }
.sales-filters select, .sales-filters input { width: 100%; min-width: 0; padding: 9px; border: 1px solid var(--line); border-radius: 4px; background: var(--panel); color: var(--ink); }
.sales-table-scroll { overflow-x: auto; margin: 16px 0; }
.sales-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.sales-table th, .sales-table td { padding: 12px 10px; text-align: left; border-bottom: 1px solid var(--line); vertical-align: top; }
.sales-table th { color: var(--muted); font-weight: 500; white-space: nowrap; }
.sales-table td { min-width: 70px; }
.sales-table td:first-child { min-width: 24px; }
.sales-table small { display: block; margin-top: 4px; color: var(--muted); }
.sales-actions { margin-top: 20px; }
.sales-reset { margin-top: 24px; padding-top: 20px; border-top: 1px solid var(--line); }
.sales-reset h4 { margin: 0 0 10px; }
.sales-reset p { line-height: 1.8; }
.sales-confirm { margin: 24px 0; padding: 20px; border: 1px solid #cba997; border-radius: 6px; background: #faf3ed; }
.sales-confirm h4 { margin: 0 0 12px; }
.sales-ack { display: flex; align-items: baseline; gap: 8px; line-height: 1.6; }
.sales-backups { margin-top: 24px; }
.sales-backups small { display: block; color: var(--muted); margin-top: 6px; }
.sales-backups ul { padding: 0; list-style: none; }
.sales-backups li { display: flex; flex-wrap: wrap; gap: 14px; justify-content: space-between; padding: 10px 0; border-bottom: 1px solid var(--line); font-size: 13px; }
</style>
