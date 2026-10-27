<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { getHealth, importFiles, importSalesSheet, importStyleArchive, rebuildIndex, retag, retryJob, type ImportJob, type TableReport } from '../api'
import { jobs, jobsError, isActive, refreshJobs, rememberJob } from '../composables/importJobs'

const SOURCE_CATEGORIES = ['本厂产品', '网图趋势', '品牌资料包', '市调拍照']
const ACCEPT = /\.(pdf|pptx|xlsx|xlsm|png|jpe?g|webp|gif|bmp|tiff?)$/i
type Queued = { key: string; file: File; relativePath: string; category: string }
const queued = ref<Queued[]>([])
const batchCategory = ref('')
const backend = ref('openai')
const openaiReady = ref(false)
const error = ref('')
const status = ref('')
const uploading = ref(false)
const dragging = ref(false)
const retrying = ref('')
const archiveReport = ref<TableReport | null>(null)
const salesReport = ref<TableReport | null>(null)
const tableError = ref('')
const tableBusy = ref('')
let queueKey = 0
const labels: Record<ImportJob['status'], string> = {
  queued: '等待处理', extracting: '正在抽图', tagging: '正在打标', completed: '已完成',
  partial: '部分完成', failed: '处理失败', interrupted: '等待继续',
}
function duration(value: number) {
  return value < 60 ? `${Math.max(0, Math.round(value))} 秒` : `${Math.floor(value / 60)} 分 ${Math.round(value % 60)} 秒`
}
function time(value: string) { return new Date(value).toLocaleString('zh-CN', { hour12: false }) }
function relativePathOf(file: File) {
  const relative = (file as File & { webkitRelativePath?: string }).webkitRelativePath
  return (relative || file.name).replace(/\\/g, '/').replace(/^\/+/, '')
}
function addFiles(files: File[]) {
  error.value = ''
  const skipped = files.filter(file => !ACCEPT.test(file.name) || file.name.startsWith('.') || relativePathOf(file).includes('__MACOSX'))
  const allowed = files.filter(file => !skipped.includes(file))
  if (skipped.length) error.value = `已跳过 ${skipped.length} 个不支持的文件，请使用 PDF、PPT、Excel 或图片。`
  const existing = new Set(queued.value.map(item => item.relativePath.toLowerCase()))
  for (const file of allowed) {
    const relativePath = relativePathOf(file)
    if (existing.has(relativePath.toLowerCase())) continue
    existing.add(relativePath.toLowerCase())
    queued.value.push({ key: `${queueKey++}`, file, relativePath, category: batchCategory.value })
  }
}
function onFiles(event: Event) { addFiles(Array.from((event.target as HTMLInputElement).files || [])) }
function onFolder(event: Event) { addFiles(Array.from((event.target as HTMLInputElement).files || [])) }
function readEntry(entry: FileSystemEntry): Promise<File[]> {
  return new Promise(resolve => {
    if (entry.isFile) {
      (entry as FileSystemFileEntry).file(file => {
        const path = entry.fullPath.replace(/^\//, '') || file.name
        Object.defineProperty(file, 'webkitRelativePath', { value: path })
        resolve([file])
      }, () => resolve([]))
      return
    }
    if (!entry.isDirectory) { resolve([]); return }
    const reader = (entry as FileSystemDirectoryEntry).createReader()
    const children: FileSystemEntry[] = []
    const readBatch = () => reader.readEntries(async batch => {
      if (!batch.length) {
        const nested: File[] = []
        for (const child of children) nested.push(...await readEntry(child))
        resolve(nested)
        return
      }
      children.push(...batch)
      readBatch()
    }, () => resolve([]))
    readBatch()
  })
}
async function onDrop(event: DragEvent) {
  dragging.value = false
  if (uploading.value) return
  const items = Array.from(event.dataTransfer?.items || [])
  const entries = items.map(item => item.webkitGetAsEntry?.()).filter((entry): entry is FileSystemEntry => Boolean(entry))
  if (entries.length) {
    const files: File[] = []
    for (const entry of entries) files.push(...await readEntry(entry))
    addFiles(files)
    return
  }
  addFiles(Array.from(event.dataTransfer?.files || []))
}
function applyBatchCategory() {
  if (!batchCategory.value) { error.value = '请先选择要套用的资料大类'; return }
  error.value = ''
  queued.value.forEach(item => { item.category = batchCategory.value })
}
async function runImport() {
  error.value = ''; status.value = ''
  if (!queued.value.length) { error.value = '请先选择文件'; return }
  if (queued.value.some(item => !item.category)) { error.value = '请为每个文件选择资料大类，或使用「全部设为」。'; return }
  uploading.value = true
  try {
    const job = await importFiles(queued.value.map(item => ({ file: item.file, relativePath: item.relativePath, category: item.category })), backend.value)
    rememberJob(job)
    queued.value = []
    for (const id of ['import-files', 'import-folder']) {
      const picker = document.getElementById(id) as HTMLInputElement | null
      if (picker) picker.value = ''
    }
    status.value = '任务已提交，可以切换页面。完成后会提示，进度会保存在任务记录中。'
    await refreshJobs()
  } catch (reason) { error.value = reason instanceof Error ? reason.message : String(reason) }
  finally { uploading.value = false }
}
async function uploadTable(kind: 'archive' | 'sales', event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0]
  ;(event.target as HTMLInputElement).value = ''
  if (!file) return
  tableError.value = ''; tableBusy.value = kind
  try {
    const report = kind === 'archive' ? await importStyleArchive(file) : await importSalesSheet(file)
    if (kind === 'archive') archiveReport.value = report
    else salesReport.value = report
  } catch (reason) { tableError.value = reason instanceof Error ? reason.message : String(reason) }
  finally { tableBusy.value = '' }
}
async function runRetag(overwrite: boolean) {
  if (overwrite && !window.confirm('全量重新分类会产生新的待审核建议，原人工结果会保留。确定继续吗？')) return
  uploading.value = true; error.value = ''
  try { rememberJob(await retag(backend.value, overwrite)); await refreshJobs() }
  catch (reason) { error.value = reason instanceof Error ? reason.message : String(reason) }
  finally { uploading.value = false }
}
async function retry(job: ImportJob) {
  retrying.value = job.id; error.value = ''
  try { rememberJob(await retryJob(job.id)); await refreshJobs() }
  catch (reason) { error.value = reason instanceof Error ? reason.message : String(reason) }
  finally { retrying.value = '' }
}
async function runRebuild() {
  uploading.value = true; error.value = ''
  try { await rebuildIndex(); status.value = '已刷新已审核与包含未审核的两种搜索索引' }
  catch (reason) { error.value = reason instanceof Error ? reason.message : String(reason) }
  finally { uploading.value = false }
}
function preview(values?: string[]) {
  if (!values?.length) return '无'
  return values.length > 12 ? `${values.slice(0, 12).join('、')} 等 ${values.length} 个` : values.join('、')
}
onMounted(async () => {
  try {
    const health = await getHealth()
    openaiReady.value = health.openai_ready
    if (!health.openai_ready) backend.value = 'mock'
    await refreshJobs()
  } catch { error.value = '无法连接服务，请稍后刷新重试' }
})
</script>

<template>
  <main class="import-page">
    <div class="page-head"><div><h2>导入与处理任务</h2><p class="meta">分类完成后先在主页展示并标记未审核，再由人工确认。每个文件单独选择资料大类，抽出来的图片会继承该文件的大类。</p></div>
      <span class="meta">{{ openaiReady ? 'AI 分类已配置' : '当前可使用模拟分类' }}</span></div>
    <section class="import-upload" aria-label="上传资料">
      <div class="dropzone" :class="{ dragging }" @dragover.prevent="dragging = true" @dragleave.prevent="dragging = false" @drop.prevent="onDrop">
        <strong>拖入 PDF / PPT / Excel，或图片、整个文件夹</strong>
        <p class="meta">支持子文件夹。本厂产品的图片请直接用款号命名，例如 516083.jpg，一个款号只保留一张图。</p>
        <div class="toolbar import-pickers">
          <label for="import-files" class="btn">选择文件</label>
          <label for="import-folder" class="btn">选择文件夹</label>
        </div>
        <input id="import-files" type="file" multiple accept=".pdf,.pptx,.xlsx,.xlsm,.jpg,.jpeg,.png,.webp,.gif,.bmp,.tif,.tiff" :disabled="uploading" @change="onFiles" />
        <input id="import-folder" type="file" multiple webkitdirectory :disabled="uploading" @change="onFolder" />
      </div>
      <div class="toolbar import-batch">
        <div class="field"><label for="batch-category">全部设为</label>
          <select id="batch-category" v-model="batchCategory" :disabled="uploading">
            <option value="">请选择资料大类</option>
            <option v-for="item in SOURCE_CATEGORIES" :key="item" :value="item">{{ item }}</option>
          </select>
        </div>
        <button class="btn" type="button" :disabled="uploading || !queued.length" @click="applyBatchCategory">应用到全部文件</button>
      </div>
      <ul v-if="queued.length" class="import-file-list"><li v-for="(item, index) in queued" :key="item.key">
        <span>{{ item.relativePath }} <small>{{ (item.file.size / 1024 / 1024).toFixed(1) }} MB</small></span>
        <select :aria-label="`${item.relativePath} 的资料大类`" v-model="item.category" :disabled="uploading">
          <option value="">选择大类</option>
          <option v-for="category in SOURCE_CATEGORIES" :key="category" :value="category">{{ category }}</option>
        </select>
        <button class="btn" :disabled="uploading" :aria-label="`移除 ${item.relativePath}`" @click="queued.splice(index, 1)">移除</button>
      </li></ul>
      <div class="toolbar import-submit"><div class="field"><label for="tag-backend">分类方式</label>
        <select id="tag-backend" v-model="backend" :disabled="uploading"><option value="openai" :disabled="!openaiReady">GPT-4o-mini</option><option value="mock">模拟分类（测试）</option></select></div>
        <button class="btn primary" :disabled="uploading" @click="runImport">{{ uploading ? '正在提交…' : '上传并开始分类' }}</button>
      </div>
    </section>
    <section class="import-upload factory-import" aria-label="本厂款式档案与销量">
      <h3>本厂款式档案与销量</h3>
      <p class="meta">这两类 Excel 不抽图、不打标。款式档案按大货款号更新；销量按款号、颜色、尺码覆盖，不会重复累计。请不要把它们和图片放在上面的抽图任务里。</p>
      <div class="toolbar">
        <label class="btn" for="archive-file">{{ tableBusy === 'archive' ? '正在导入档案…' : '导入款式档案' }}</label>
        <input id="archive-file" type="file" accept=".xlsx,.xlsm" :disabled="tableBusy !== ''" @change="uploadTable('archive', $event)" />
        <label class="btn" for="sales-file">{{ tableBusy === 'sales' ? '正在导入销量…' : '导入销量' }}</label>
        <input id="sales-file" type="file" accept=".xlsx,.xlsm" :disabled="tableBusy !== ''" @change="uploadTable('sales', $event)" />
      </div>
      <p v-if="tableError" class="error" role="alert">{{ tableError }}</p>
      <div v-if="archiveReport" class="import-report" role="status">
        <strong>款式档案 {{ archiveReport.source_file }}</strong>
        <p>新增 {{ archiveReport.imported }} 条，更新 {{ archiveReport.updated }} 条，跳过 {{ archiveReport.skipped_rows }} 行，涉及款号 {{ archiveReport.style_codes.length }} 个。</p>
        <p v-if="archiveReport.warnings?.length" class="meta">{{ archiveReport.warnings.join('；') }}</p>
        <p>有档案、还没有图片的款号：{{ preview(archiveReport.archive_without_image) }}</p>
        <p>已有本厂图片、还没有档案的款号：{{ preview(archiveReport.image_without_archive) }}</p>
      </div>
      <div v-if="salesReport" class="import-report" role="status">
        <strong>销量 {{ salesReport.source_file }}</strong>
        <p>写入 {{ salesReport.row_count }} 行（新增 {{ salesReport.imported }}，覆盖 {{ salesReport.updated }}），跳过 {{ salesReport.skipped_rows }} 行。</p>
        <p>本次款号合计 {{ salesReport.stored_quantity?.toLocaleString('zh-CN') }} 件 / {{ salesReport.stored_amount?.toLocaleString('zh-CN') }} 元。</p>
        <p>有档案、还没有图片的款号：{{ preview(salesReport.archive_without_image) }}</p>
        <p>已有本厂图片、还没有档案的款号：{{ preview(salesReport.image_without_archive) }}</p>
      </div>
    </section>
    <p v-if="status" class="review-notice" role="status">{{ status }}</p>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <section class="import-tasks" aria-label="处理任务">
      <div class="page-head"><h3>任务记录 <span class="meta">{{ jobs.length }} 批</span></h3><button class="btn" @click="refreshJobs">刷新进度</button></div>
      <p v-if="jobsError" class="error" role="alert">{{ jobsError }}</p>
      <p v-if="!jobs.length && !jobsError" class="empty">暂无任务记录。上方上传资料后，可在这里查看抽图和打标进度。更新前的资料保留在图库和审核队列中。</p>
      <article v-for="job in jobs" :id="`job-${job.id}`" :key="job.id" class="import-task" :class="{ 'task-active': isActive(job) }">
        <div class="task-heading"><strong>{{ job.name }}</strong><span class="task-state" :class="job.status">{{ labels[job.status] }}</span></div>
        <p class="meta">{{ time(job.created_at) }} · {{ job.backend === 'mock' ? '模拟分类' : 'AI 分类' }} · 已用 {{ duration(job.elapsed_seconds) }}<span v-if="job.eta_seconds !== null && job.eta_seconds > 0"> · 预计剩余约 {{ duration(job.eta_seconds) }}</span></p>
        <template v-if="job.status === 'queued'"><p class="meta">前序任务完成后自动开始，无需重复提交。</p></template>
        <template v-else-if="job.status === 'extracting'"><progress :value="job.files_processed" :max="job.files_total || 1" aria-label="文件抽图进度" /><p class="meta">抽图 {{ job.files_processed }} / {{ job.files_total }} 份资料 · {{ job.current_file }}</p></template>
        <template v-else-if="job.total"><progress :value="job.processed" :max="job.total || 1" :aria-label="`${job.name}分类进度`" /><p class="meta">已处理 {{ job.processed }} / {{ job.total }} 张，成功 {{ job.succeeded }} 张，<template v-if="job.filtered">过滤非款式图 {{ job.filtered }} 张，</template><template v-if="job.skipped">跳过相同图片 {{ job.skipped }} 张，</template>失败 {{ job.failed }} 张<span v-if="job.status === 'tagging'"> · {{ job.current_file }}</span></p></template>
        <p v-if="!job.total && !isActive(job)" class="meta">未生成可分类的图片。</p>
        <p v-if="!isActive(job)" class="task-message" role="status">{{ job.message }}</p>
        <div class="toolbar"><RouterLink v-if="job.succeeded" class="btn" :to="{ path: '/review', query: job.kind === 'import' ? { batch: job.id } : {} }">审核本批图片</RouterLink>
          <button v-if="['partial', 'failed', 'interrupted'].includes(job.status)" class="btn" :disabled="retrying === job.id" @click="retry(job)">{{ retrying === job.id ? '提交中…' : '继续处理未完成项' }}</button></div>
        <details v-if="job.errors.length"><summary>查看 {{ job.errors.length }} 项异常</summary><ul class="task-errors"><li v-for="(item, index) in job.errors" :key="index"><strong>{{ item.name }}</strong><span>{{ item.message }}</span></li></ul></details>
      </article>
    </section>
    <details class="import-admin"><summary>分类与检索管理</summary><p class="meta">全量重打会重新提交审核，保留此前人工分类。已完成的历史任务不受影响。</p><div class="toolbar">
      <button class="btn" :disabled="uploading" @click="runRetag(false)">补打未确认标签</button><button class="btn" :disabled="uploading" @click="runRetag(true)">全量重新分类</button><button class="btn" :disabled="uploading" @click="runRebuild">刷新检索索引</button></div></details>
  </main>
</template>
