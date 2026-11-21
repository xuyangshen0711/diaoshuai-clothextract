<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import SalesImportTotals from './SalesImportTotals.vue'
import SalesImportImpact from './SalesImportImpact.vue'
import { confirmTable, previewTable, replanTable, type TableImportResult, type TableKind, type TablePreview } from '../api'
const props = defineProps<{ kind: TableKind; targetCode: string }>()
const emit = defineEmits<{ completed: [report: TableImportResult]; task: [kind: 'images', code: string] }>()
const KIND_NAMES: Record<TableKind, string> = { archive: '商品资料表', sales: '订单数据', production: '生产数据', guide: '款式指示书' }
const preview = ref<TablePreview | null>(null)
const result = ref<TableImportResult | null>(null)
const busy = ref(false)
const error = ref('')
const acknowledge = ref(false)
const historical = ref(false)
const replaceSeason = ref(true)
const start = ref('')
const end = ref('')
const file = ref<File | null>(null)
const issuePageSize = 20
const issuePage = ref(1)
const issuePageCount = computed(() => Math.max(1, Math.ceil((preview.value?.issues.length || 0) / issuePageSize)))
const issueStart = computed(() => (issuePage.value - 1) * issuePageSize)
const visibleIssues = computed(() => preview.value?.issues.slice(issueStart.value, issueStart.value + issuePageSize) || [])
watch(preview, value => { issuePage.value = 1; replaceSeason.value = Boolean(value?.season_replace?.default) })
const name = computed(() => KIND_NAMES[props.kind])
const actualName = computed(() => preview.value ? KIND_NAMES[preview.value.kind] : name.value)
const isLines = computed(() => preview.value?.kind === 'sales' && preview.value.format === 'sales_lines')
const isSeasonSource = computed(() => preview.value?.kind === 'production' || preview.value?.kind === 'guide')
const shownImpact = computed(() => isLines.value && replaceSeason.value && preview.value?.season_replace?.impact ? preview.value.season_replace.impact : preview.value?.impact)
const needsAcknowledgement = computed(() => Boolean(preview.value?.issue_count || preview.value?.meta.reconciled === false))
const rangeReady = computed(() => (preview.value?.historical_start || '') === (historical.value ? start.value : '') && (preview.value?.historical_end || '') === (historical.value ? end.value : '') && (!historical.value || !!start.value && !!end.value))
const intro = computed(() => ({
  archive: '按大货款号更新，并关联同款图片。',
  sales: '推荐使用订单明细；同季节可整季覆盖旧明细。',
  production: '用于生产数、生产比和等级表的「无产量」。同季节整体覆盖。',
  guide: '用于设计师、工艺师表的「设计款式数量」。同季节整体覆盖。',
}[props.kind]))
const n = (value: number | string | undefined) => Number(value || 0).toLocaleString()
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
  if (isLines.value && replaceSeason.value && preview.value.season_replace?.removed_rows &&
      !window.confirm(`整季覆盖会移除 ${preview.value.season_replace.removed_rows} 行新表里已经没有的旧明细（${n(preview.value.season_replace.removed_quantity)} 件）。移除前会自动备份，可在「管理订单数据 → 删除记录」恢复。确认导入？`)) return
  busy.value = true; error.value = ''
  try {
    result.value = await confirmTable(preview.value.token, acknowledge.value, historical.value ? start.value : '', historical.value ? end.value : '', isLines.value && replaceSeason.value)
    emit('completed', result.value); preview.value = null
  } catch (e) { error.value = e instanceof Error ? e.message : String(e) }
  finally { busy.value = false }
}
defineExpose({ openFile })
</script>

<template>
  <section class="workspace-panel table-import-panel" aria-label="表格导入">
    <div class="panel-heading"><div><h3>导入{{ name }}</h3><p class="meta">先预览，再确认入库。{{ intro }}</p></div><span class="stage-label">{{ result ? '已完成' : preview ? '核对并确认' : '选择 Excel' }}</span></div>
    <p v-if="targetCode" class="workspace-notice">正在为款号 <strong>{{ targetCode }}</strong> 补充资料。表格可包含其他款号，仍会逐款匹配。</p>
    <label class="table-picker" :class="{ busy }" for="workspace-table-file"><strong>{{ busy ? '正在处理表格…' : `选择${name} Excel` }}</strong><span>.xlsx / .xlsm · 按表头读取数据，不抽图、不调用 AI</span></label>
    <input id="workspace-table-file" class="visually-hidden" type="file" accept=".xlsx,.xlsm" :disabled="busy" @change="choose" />
    <template v-if="!preview && !result">
      <p v-if="kind === 'archive'" class="meta">使用商品资料表范本的 11 列：季节、大货款号、设计款号、颜色、尺码、针型、一级品类、设计师、工艺师、原料名称、二级品类。按列名对应，列顺序可调整；不需要“系列”列。一级品类和原料名称会用于季度款式分析，请尽量填齐。</p>
      <div v-else-if="kind === 'sales'" class="meta">
        <p>表头需包含款号、颜色、尺码、订单数量、订单金额；订单明细还需订单号。</p>
        <p>系统会自动识别：填了「备货数」的行是<strong>备单</strong>（单独保存，只用于订单与备单对比，不计销量；1 件的小备单忽略）；有数量但没有金额的行是<strong>发样</strong>（后期退回，不计数量）。</p>
      </div>
      <div v-else-if="kind === 'production'" class="meta">
        <p>新入口：ERP 导出的生产数据，表头需包含 款号、颜色、尺码、计划（设计款号可选）。表尾合计行自动忽略。</p>
        <p>生产数据没有季节列，系统按商品资料中这些款号的季节整体覆盖同季旧数据。</p>
      </div>
      <div v-else class="meta">
        <p>新入口：款式指示书，表头需包含 季节、大货款号、设计师（设计款号、颜色、原料名称、工艺师可选）。</p>
        <p>同一款号的多行版次会合并为一个设计款；按表内季节整体覆盖同季旧数据。</p>
      </div>
    </template>
    <div v-if="preview" class="table-preview" aria-live="polite">
      <p v-if="preview.kind !== kind" class="workspace-notice">这个文件识别为{{ actualName }}。下方将按{{ actualName }}规则导入，无需重新上传。</p>
      <div class="preview-title"><strong>{{ preview.source_file }}</strong><span class="type-label">{{ preview.format_label }}</span></div>
      <p class="meta">识别依据：{{ preview.meta.evidence_columns?.join('、') }}。共 {{ preview.style_codes.length }} 个本厂款号、{{ preview.row_count }} 条有效记录。</p>
      <p v-if="preview.kind === 'archive'" class="meta">一级、二级品类按表格内容导入，空白单元格保留为空；不会由 AI 猜测或补填。</p>
      <div v-if="rangeReady" class="preview-counts"><span>新增 <strong>{{ preview.counts.new }}</strong></span><span>更新 <strong>{{ preview.counts.updated }}</strong></span><span>无变化 <strong>{{ preview.counts.unchanged }}</strong></span><span :class="{ 'gap-text': preview.issue_count }">异常行 <strong>{{ preview.issue_count }}</strong></span></div>
      <p v-if="preview.counts.updated && preview.kind === 'archive'" class="workspace-notice">更新会覆盖表格对应字段，包括空值。请确认该表为要使用的档案版本。</p>
      <p v-if="preview.meta.parsed_quantity != null && preview.kind === 'sales'">本次计入销量 {{ preview.meta.parsed_quantity.toLocaleString() }} 件，金额 ¥{{ preview.meta.parsed_amount?.toLocaleString() }}。</p>
      <p v-if="preview.meta.reconcile_note" :class="preview.meta.reconciled === false ? 'error' : 'complete-note'">{{ preview.meta.reconcile_note }}</p>
      <p v-if="preview.meta.warnings?.length" class="workspace-notice">{{ preview.meta.warnings.join('；') }}</p>
      <p v-for="item in preview.meta.notes || []" :key="item" class="meta">{{ item }}</p>
      <div v-if="preview.exclusions && (preview.exclusions.stock_rows || preview.exclusions.sample_rows || preview.exclusions.tiny_stock_rows)" class="workspace-notice">
        <strong>以下行按规则不计入销量：</strong>
        <ul class="exclusion-list">
          <li v-if="preview.exclusions.stock_rows">备单 {{ n(preview.exclusions.stock_rows) }} 行、{{ n(preview.exclusions.stock_quantity) }} 件（{{ preview.exclusions.stock_style_count }} 款）：单独保存，用于「订单与备单的对比」。</li>
          <li v-if="preview.exclusions.tiny_stock_rows">1 件的小备单 {{ n(preview.exclusions.tiny_stock_rows) }} 行：忽略。</li>
          <li v-if="preview.exclusions.sample_rows">无金额发样 {{ n(preview.exclusions.sample_rows) }} 行、{{ n(preview.exclusions.sample_quantity) }} 件：后期退回，不计数量。</li>
        </ul>
      </div>
      <div v-if="isLines && preview.season_replace" class="workspace-notice season-replace">
        <label v-if="preview.season_replace.available" class="acknowledge"><input v-model="replaceSeason" type="checkbox" :disabled="busy" /> <strong>整季覆盖（推荐）：以本表为准替换 {{ preview.season_replace.seasons.join('、') }} 的订单明细</strong></label>
        <p v-else><strong>表中没有年度 / 季节列，只能合并导入。</strong></p>
        <template v-if="replaceSeason && preview.season_replace.available">
          <p v-if="preview.season_replace.removed_rows">将移除 <strong>{{ n(preview.season_replace.removed_rows) }}</strong> 行新表里已经没有的旧明细（{{ n(preview.season_replace.removed_quantity) }} 件、¥{{ n(preview.season_replace.removed_amount) }}，涉及 {{ preview.season_replace.removed_style_count }} 款），多为 ERP 里删单或改单。移除前自动备份，可在「管理订单数据 → 删除记录」恢复。</p>
          <p v-else>同季节没有需要移除的旧明细，新表会更新已有明细并补充新明细。</p>
          <details v-if="preview.season_replace.removed_samples.length"><summary>查看将移除的旧明细（前 {{ preview.season_replace.removed_samples.length }} 行）</summary>
            <ul><li v-for="(row, index) in preview.season_replace.removed_samples" :key="index">{{ row.order_date }} {{ row.order_no }} · {{ row.variant_code }} · {{ row.color }} / {{ row.size_label }} · {{ row.quantity }} 件</li></ul>
          </details>
          <p v-if="preview.season_replace.blank_season_rows" class="meta">另有 {{ preview.season_replace.blank_season_rows }} 行没有季节，只合并导入。</p>
        </template>
        <p v-else-if="preview.season_replace.available" class="meta">合并导入：匹配的明细更新，新明细增加；新表没有出现的旧明细继续保留。</p>
      </div>
      <div v-else-if="preview.kind === 'sales'" class="workspace-notice">
        <strong>本次为合并导入，不会整季替换。</strong>
        <p>按大货款号（含客户版本）＋颜色＋尺码＋汇总范围匹配：匹配的汇总更新，新汇总增加；新表没有出现的旧汇总继续保留。</p>
      </div>
      <div v-if="isSeasonSource && preview.season_replace" class="workspace-notice season-replace">
        <strong>整季覆盖：{{ preview.season_replace.seasons.join('、') || '（商品资料中未找到这些款号的季节）' }}</strong>
        <p v-if="preview.season_replace.removed_rows">同季节旧数据中有 {{ n(preview.season_replace.removed_rows) }} 行本表没有，导入后会移除。</p>
        <p v-else>同季节没有需要移除的旧数据。</p>
      </div>
      <p v-if="preview.overlap_variants.length" class="workspace-notice">{{ preview.overlap_variants.slice(0, 8).join('、') }}{{ preview.overlap_variants.length > 8 ? '等版本' : '' }}同时有汇总和订单明细。以订单明细为准，未标注历史范围的汇总保留用于核对，避免重复累加。</p>
      <div v-if="preview.kind === 'sales' && preview.format !== 'sales_lines'" class="summary-policy"><h4>汇总的使用方式</h4>
        <label><input v-model="historical" type="radio" name="summary-scope" :value="false" :disabled="busy" /> 常规汇总：已有订单明细的版本仅用于核对</label>
        <label><input v-model="historical" type="radio" name="summary-scope" :value="true" :disabled="busy" /> 历史补充：按明确日期计入，必须与现有数据不重叠</label>
        <div v-if="historical" class="toolbar"><label>开始日期 <input v-model="start" type="date" /></label><label>结束日期 <input v-model="end" type="date" /></label></div>
        <p v-if="historical" class="meta">该表所有行应属于同一日期范围。范围重复上传会更新，交叉范围会阻止导入；之后补入重叠明细时，该历史汇总将转为核对用。</p>
        <p v-else class="meta">同一客户版本、颜色、尺码的常规汇总会替换原汇总，不适合作为增量数据上传。</p>
        <button v-if="!rangeReady" class="btn" :disabled="busy || (historical && (!start || !end))" @click="previewRange">预览所选范围</button>
      </div>
      <SalesImportImpact v-if="preview.kind === 'sales' && shownImpact && rangeReady" :impact="shownImpact" estimated />
      <p v-else-if="preview.kind === 'sales'" class="workspace-notice">请重新预览所选范围，查看最新的导入影响。</p>
      <div class="workspace-table-wrap" tabindex="0" role="region" aria-label="可横向滚动的数据预览">
        <table class="workspace-table">
          <caption>数据预览（前 {{ preview.samples.length }} 条）</caption>
          <thead><tr>
            <th>大货款号</th><th>本厂款号</th>
            <template v-if="preview.kind === 'archive'"><th>客户版本</th><th>季节</th><th>一级品类</th><th>二级品类</th><th>设计款号</th></template>
            <template v-else-if="preview.kind === 'sales'"><th>颜色 / 尺码</th><th>订单数据</th></template>
            <template v-else-if="preview.kind === 'production'"><th>颜色 / 尺码</th><th>计划数</th><th>设计款号</th></template>
            <template v-else><th>季节</th><th>设计师</th><th>工艺师</th><th>原料</th></template>
            <th>处理</th>
          </tr></thead>
          <tbody><tr v-for="(row, index) in preview.samples" :key="index">
            <th>{{ row.variant_code }}</th><td>{{ row.style_code }}</td>
            <template v-if="preview.kind === 'archive'"><td>{{ row.customer }}</td><td>{{ row.season || '—' }}</td><td>{{ row.category || '未填写' }}</td><td>{{ row.subcategory || '未填写' }}</td><td>{{ row.design_code || '—' }}</td></template>
            <template v-else-if="preview.kind === 'sales'"><td>{{ row.color }} / {{ row.size_label }}</td><td>{{ row.quantity }}</td></template>
            <template v-else-if="preview.kind === 'production'"><td>{{ row.color }} / {{ row.size_label }}</td><td>{{ row.quantity }}</td><td>{{ row.design_code || '—' }}</td></template>
            <template v-else><td>{{ row.season }}</td><td>{{ row.designer || '未填写' }}</td><td>{{ row.craftsman || '未填写' }}</td><td>{{ row.material || '—' }}</td></template>
            <td>{{ { new: '新增', updated: '更新', unchanged: '跳过无变化' }[row.action] }}</td>
          </tr></tbody>
        </table>
      </div>
      <details v-if="preview.issue_count" class="issue-details">
        <summary>查看全部 {{ preview.issue_count }} 行异常</summary>
        <p class="meta">每页 {{ issuePageSize }} 条，行号对应原 Excel，方便回到表格修改。</p>
        <div :key="issuePage" class="issue-list" tabindex="0" role="region" aria-label="本页异常明细，可上下滚动">
          <ul><li v-for="issue in visibleIssues" :key="issue.row">第 {{ issue.row }} 行 <strong>{{ issue.style_code }}</strong>：{{ issue.message }}</li></ul>
        </div>
        <nav class="issue-pagination" aria-label="异常明细分页">
          <span role="status">第 {{ issueStart + 1 }}–{{ issueStart + visibleIssues.length }} 条，共 {{ preview.issue_count }} 条 · 第 {{ issuePage }} / {{ issuePageCount }} 页</span>
          <div v-if="issuePageCount > 1" class="issue-page-buttons">
            <button type="button" class="btn small" :disabled="issuePage === 1" @click="issuePage = 1">首页</button>
            <button type="button" class="btn small" :disabled="issuePage === 1" @click="issuePage--">上一页</button>
            <button type="button" class="btn small" :disabled="issuePage === issuePageCount" @click="issuePage++">下一页</button>
            <button type="button" class="btn small" :disabled="issuePage === issuePageCount" @click="issuePage = issuePageCount">末页</button>
          </div>
        </nav>
      </details>
      <p v-if="preview.ignored_rows" class="meta">另有 {{ preview.ignored_rows }} 行空行或合计行，不作为业务数据导入。</p>
      <p v-if="preview.missing_images.length" class="meta">导入后仍需补图片：{{ preview.missing_images.slice(0, 12).join('、') }}{{ preview.missing_images.length > 12 ? ` 等 ${preview.missing_images.length} 款` : '' }}。会加入资料待办。</p>
      <label v-if="needsAcknowledgement" class="acknowledge"><input v-model="acknowledge" type="checkbox" /> 我已核对异常与合计差异，只导入有效记录</label>
      <div class="toolbar preview-actions"><button class="btn primary" :disabled="busy || !preview.can_confirm || (needsAcknowledgement && !acknowledge) || !rangeReady || (preview.kind === 'sales' && !shownImpact)" @click="confirm">{{ busy ? '正在导入…' : `确认导入${actualName}` }}</button><button class="btn" :disabled="busy || !file" @click="file && openFile(file)">重新预览</button><button class="text-button" :disabled="busy" @click="preview = null">取消</button></div>
      <p v-if="!preview.can_confirm" class="error">未找到有效数据，请修正表格后重新选择。</p>
    </div>
    <div v-if="result" class="import-success" role="status"><h4>{{ result.source_file }} 已导入</h4><p>新增 {{ result.imported }} 条，更新 {{ result.updated }} 条，{{ result.unchanged }} 条无变化。</p>
      <p v-if="result.season_replace?.enabled">已整季覆盖 {{ result.season_replace.seasons.join('、') }}<span v-if="result.season_replace.removed_rows">，移除旧行 {{ result.season_replace.removed_rows }} 条{{ result.season_replace.backup_id ? '（已备份，可恢复）' : '' }}</span>。</p>
      <SalesImportTotals v-if="result.kind === 'sales'" :report="result" />
      <div class="toolbar">
        <RouterLink v-if="result.kind === 'production' || result.kind === 'guide'" class="btn" :to="{ path: '/analytics', query: { tab: 'season' } }">去看季度款式分析</RouterLink>
        <RouterLink v-else class="btn" :to="{ path: '/', query: { source_category: '本厂产品', q: result.style_codes.length === 1 ? result.style_codes[0] : undefined } }">查看本厂款式</RouterLink>
        <button v-if="result.archive_without_image?.length || result.sales_without_image?.length" class="btn" @click="emit('task', 'images', (result.archive_without_image[0] || result.sales_without_image?.[0])!)">继续补图片</button>
      </div><p class="meta">结果已保存在下方导入记录中，待补资料已更新。</p></div>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
  </section>
</template>

<style scoped>
.exclusion-list { margin: .35rem 0 0; padding-left: 1.2rem; }
.season-replace details ul { margin: .35rem 0 0; padding-left: 1.2rem; font-size: .85em; }
</style>
