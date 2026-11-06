<script setup lang="ts">
import { computed, ref } from 'vue'
import { confirmTable, previewTable, replanTable, type TableImportResult, type TablePreview } from '../api'
const props = defineProps<{ kind: 'archive' | 'sales'; targetCode: string }>()
const emit = defineEmits<{ completed: [report: TableImportResult]; task: [kind: 'images', code: string] }>()
const preview = ref<TablePreview | null>(null)
const result = ref<TableImportResult | null>(null)
const busy = ref(false)
const error = ref('')
const acknowledge = ref(false)
const historical = ref(false)
const start = ref('')
const end = ref('')
const file = ref<File | null>(null)
const name = computed(() => props.kind === 'archive' ? '款式档案' : '销量')
const actualName = computed(() => preview.value?.kind === 'archive' ? '款式档案' : '销量')
const needsAcknowledgement = computed(() => Boolean(preview.value?.issue_count || preview.value?.meta.reconciled === false))
const rangeReady = computed(() => (preview.value?.historical_start || '') === (historical.value ? start.value : '') && (preview.value?.historical_end || '') === (historical.value ? end.value : '') && (!historical.value || !!start.value && !!end.value))
async function previewRange() {
  if (!preview.value) return
  busy.value = true; error.value = ''
  try { preview.value = await replanTable(preview.value.token, historical.value ? start.value : '', historical.value ? end.value : '') }
  catch (e) { error.value = e instanceof Error ? e.message : String(e) }
  finally { busy.value = false }
}
async function openFile(value: File) {
  file.value = value; busy.value = true; error.value = ''; result.value = null; preview.value = null; acknowledge.value = false; historical.value = false; start.value = ''; end.value = ''
  try { preview.value = await previewTable(value, props.kind) }
  catch (e) { error.value = e instanceof Error ? e.message : String(e) }
  finally { busy.value = false }
}
function choose(event: Event) {
  const input = event.target as HTMLInputElement
  const value = input.files?.[0]; input.value = ''
  if (value) openFile(value)
}
async function confirm() {
  if (!preview.value) return
  busy.value = true; error.value = ''
  try {
    result.value = await confirmTable(preview.value.token, acknowledge.value, historical.value ? start.value : '', historical.value ? end.value : '')
    emit('completed', result.value); preview.value = null
  } catch (e) { error.value = e instanceof Error ? e.message : String(e) }
  finally { busy.value = false }
}
defineExpose({ openFile })
</script>

<template>
  <section class="workspace-panel table-import-panel" aria-label="表格导入">
    <div class="panel-heading"><div><h3>导入{{ name }}</h3><p class="meta">先预览，再确认入库。{{ kind === 'archive' ? '按大货款号更新，并关联同款图片。' : '推荐使用订单明细；重复明细更新原记录。' }}</p></div><span class="stage-label">{{ result ? '已完成' : preview ? '核对并确认' : '选择 Excel' }}</span></div>
    <p v-if="targetCode" class="workspace-notice">正在为款号 <strong>{{ targetCode }}</strong> 补充资料。表格可包含其他款号，仍会逐款匹配。</p>
    <label class="table-picker" :class="{ busy }" for="workspace-table-file"><strong>{{ busy ? '正在处理表格…' : `选择${name} Excel` }}</strong><span>.xlsx / .xlsm · 表格只读取数据，不抽图、不分类</span></label>
    <input id="workspace-table-file" class="visually-hidden" type="file" accept=".xlsx,.xlsm" :disabled="busy" @change="choose" />
    <p v-if="kind === 'archive' && !preview && !result" class="meta">表头需包含“大货款号”。可一并导入客户版本、季节、颜色、尺码、成分等。</p>
    <p v-else-if="!preview && !result" class="meta">表头需包含款号、颜色、尺码、订单数量、订单金额；订单明细还需订单号。</p>
    <div v-if="preview" class="table-preview" aria-live="polite">
      <p v-if="preview.kind !== kind" class="workspace-notice">这个文件识别为{{ actualName }}。下方将按{{ actualName }}规则导入，无需重新上传。</p>
      <div class="preview-title"><strong>{{ preview.source_file }}</strong><span class="type-label">{{ preview.format_label }}</span></div>
      <p class="meta">识别依据：{{ preview.meta.evidence_columns?.join('、') }}。共 {{ preview.style_codes.length }} 个本厂款号、{{ preview.row_count }} 条有效记录。</p>
      <div v-if="rangeReady" class="preview-counts"><span>新增 <strong>{{ preview.counts.new }}</strong></span><span>更新 <strong>{{ preview.counts.updated }}</strong></span><span>无变化 <strong>{{ preview.counts.unchanged }}</strong></span><span :class="{ 'gap-text': preview.issue_count }">异常行 <strong>{{ preview.issue_count }}</strong></span></div>
      <p v-if="preview.counts.updated && preview.kind === 'archive'" class="workspace-notice">更新会覆盖表格对应字段，包括空值。请确认该表为要使用的档案版本。</p>
      <p v-if="preview.meta.parsed_quantity != null">本次解析 {{ preview.meta.parsed_quantity.toLocaleString() }} 件，金额 ¥{{ preview.meta.parsed_amount?.toLocaleString() }}。</p>
      <p v-if="preview.meta.reconcile_note" :class="preview.meta.reconciled === false ? 'error' : 'complete-note'">{{ preview.meta.reconcile_note }}</p>
      <p v-if="preview.meta.warnings?.length" class="workspace-notice">{{ preview.meta.warnings.join('；') }}</p>
      <p v-if="preview.overlap_variants.length" class="workspace-notice">{{ preview.overlap_variants.slice(0, 8).join('、') }}{{ preview.overlap_variants.length > 8 ? '等版本' : '' }}同时有汇总和订单明细。以订单明细为准，未标注历史范围的汇总保留用于核对，避免重复累加。</p>
      <div v-if="preview.kind === 'sales' && preview.format !== 'sales_lines'" class="summary-policy"><h4>汇总的使用方式</h4>
        <label><input v-model="historical" type="radio" name="summary-scope" :value="false" :disabled="busy" /> 常规汇总：已有订单明细的版本仅用于核对</label>
        <label><input v-model="historical" type="radio" name="summary-scope" :value="true" :disabled="busy" /> 历史补充：按明确日期计入，必须与现有数据不重叠</label>
        <div v-if="historical" class="toolbar"><label>开始日期 <input v-model="start" type="date" /></label><label>结束日期 <input v-model="end" type="date" /></label></div>
        <p v-if="historical" class="meta">该表所有行应属于同一日期范围。范围重复上传会更新，交叉范围会阻止导入；之后补入重叠明细时，该历史汇总将转为核对用。</p>
        <p v-else class="meta">同一客户版本、颜色、尺码的常规汇总会替换原汇总，不适合作为增量数据上传。</p>
        <button v-if="!rangeReady" class="btn" :disabled="busy || (historical && (!start || !end))" @click="previewRange">预览所选范围</button>
      </div>
      <div class="workspace-table-wrap" tabindex="0" role="region" aria-label="可横向滚动的数据预览"><table class="workspace-table"><caption>数据预览（前 {{ preview.samples.length }} 条）</caption><thead><tr><th>大货款号</th><th>本厂款号</th><th>{{ preview.kind === 'archive' ? '客户版本' : '颜色 / 尺码' }}</th><th>{{ preview.kind === 'archive' ? '季节' : '件数' }}</th><th>处理</th></tr></thead><tbody><tr v-for="(row, index) in preview.samples" :key="index"><th>{{ row.variant_code }}</th><td>{{ row.style_code }}</td><td>{{ preview.kind === 'archive' ? row.customer : `${row.color} / ${row.size_label}` }}</td><td>{{ preview.kind === 'archive' ? row.season || '—' : row.quantity }}</td><td>{{ { new: '新增', updated: '更新', unchanged: '跳过无变化' }[row.action] }}</td></tr></tbody></table></div>
      <details v-if="preview.issue_count" class="issue-details"><summary>查看 {{ preview.issue_count }} 行异常（最多显示 100 行）</summary><ul><li v-for="issue in preview.issues" :key="issue.row">第 {{ issue.row }} 行 <strong>{{ issue.style_code }}</strong>：{{ issue.message }}</li></ul></details>
      <p v-if="preview.ignored_rows" class="meta">另有 {{ preview.ignored_rows }} 行空行或合计行，不作为业务数据导入。</p>
      <p v-if="preview.missing_images.length" class="meta">导入后仍需补图片：{{ preview.missing_images.slice(0, 12).join('、') }}{{ preview.missing_images.length > 12 ? ` 等 ${preview.missing_images.length} 款` : '' }}。会加入资料待办。</p>
      <label v-if="needsAcknowledgement" class="acknowledge"><input v-model="acknowledge" type="checkbox" /> 我已核对异常与合计差异，只导入有效记录</label>
      <div class="toolbar preview-actions"><button class="btn primary" :disabled="busy || !preview.can_confirm || (needsAcknowledgement && !acknowledge) || !rangeReady" @click="confirm">{{ busy ? '正在导入…' : `确认导入${actualName}` }}</button><button class="btn" :disabled="busy || !file" @click="file && openFile(file)">重新预览</button><button class="text-button" :disabled="busy" @click="preview = null">取消</button></div>
      <p v-if="!preview.can_confirm" class="error">未找到有效数据，请修正表格后重新选择。</p>
    </div>
    <div v-if="result" class="import-success" role="status"><h4>{{ result.source_file }} 已导入</h4><p>新增 {{ result.imported }} 条，更新 {{ result.updated }} 条，{{ result.unchanged }} 条无变化。</p><p v-if="result.kind === 'sales'">这些款号当前计入 {{ result.stored_quantity?.toLocaleString() }} 件，¥{{ result.stored_amount?.toLocaleString() }}。</p><div class="toolbar"><RouterLink class="btn" :to="{ path: '/', query: { source_category: '本厂产品', q: result.style_codes.length === 1 ? result.style_codes[0] : undefined } }">查看本厂款式</RouterLink><button v-if="result.archive_without_image.length || result.sales_without_image?.length" class="btn" @click="emit('task', 'images', (result.archive_without_image[0] || result.sales_without_image?.[0])!)">继续补图片</button></div><p class="meta">结果已保存在下方导入记录中，待补资料已更新。</p></div>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
  </section>
</template>
