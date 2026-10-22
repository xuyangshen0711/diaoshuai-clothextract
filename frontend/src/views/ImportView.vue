<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { getHealth, importFiles, rebuildIndex, retag, retryJob, type ImportJob } from '../api'
import { jobs, jobsError, isActive, refreshJobs, rememberJob } from '../composables/importJobs'

const files = ref<File[]>([])
const backend = ref('openai')
const openaiReady = ref(false)
const error = ref('')
const status = ref('')
const uploading = ref(false)
const dragging = ref(false)
const retrying = ref('')
const labels: Record<ImportJob['status'], string> = {
  queued: '等待处理', extracting: '正在抽图', tagging: '正在打标', completed: '已完成',
  partial: '部分完成', failed: '处理失败', interrupted: '等待继续',
}
function duration(value: number) {
  return value < 60 ? `${Math.max(0, Math.round(value))} 秒` : `${Math.floor(value / 60)} 分 ${Math.round(value % 60)} 秒`
}
function time(value: string) { return new Date(value).toLocaleString('zh-CN', { hour12: false }) }
function selectFiles(values: File[]) {
  error.value = ''
  const allowed = values.filter(file => /\.(pdf|pptx|xlsx|xlsm)$/i.test(file.name))
  if (allowed.length !== values.length) error.value = '部分文件格式不支持，请使用 PDF、PPTX 或 Excel。'
  files.value = allowed
}
function onFiles(e: Event) { selectFiles(Array.from((e.target as HTMLInputElement).files || [])) }
function onDrop(e: DragEvent) {
  dragging.value = false
  if (!uploading.value) selectFiles(Array.from(e.dataTransfer?.files || []))
}
async function runImport() {
  error.value = ''; status.value = ''
  if (!files.value.length) { error.value = '请先选择文件'; return }
  uploading.value = true
  try {
    const job = await importFiles(files.value, backend.value)
    rememberJob(job)
    files.value = []
    const picker = document.getElementById('import-files') as HTMLInputElement | null
    if (picker) picker.value = ''
    status.value = '任务已提交，可以切换页面。完成后会提示，进度会保存在任务记录中。'
    await refreshJobs()
  } catch (e) { error.value = e instanceof Error ? e.message : String(e) }
  finally { uploading.value = false }
}
async function runRetag(overwrite: boolean) {
  if (overwrite && !window.confirm('全量重新分类会产生新的待审核建议，原人工结果会保留。确定继续吗？')) return
  uploading.value = true; error.value = ''
  try { rememberJob(await retag(backend.value, overwrite)); await refreshJobs() }
  catch (e) { error.value = e instanceof Error ? e.message : String(e) }
  finally { uploading.value = false }
}
async function retry(job: ImportJob) {
  retrying.value = job.id; error.value = ''
  try { rememberJob(await retryJob(job.id)); await refreshJobs() }
  catch (e) { error.value = e instanceof Error ? e.message : String(e) }
  finally { retrying.value = '' }
}
async function runRebuild() {
  uploading.value = true; error.value = ''
  try { await rebuildIndex(); status.value = '已刷新已审核与包含未审核的两种搜索索引' }
  catch (e) { error.value = e instanceof Error ? e.message : String(e) }
  finally { uploading.value = false }
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
    <div class="page-head"><div><h2>导入与处理任务</h2><p class="meta">分类完成后先在主页展示并标记未审核，再由人工确认。</p></div>
      <span class="meta">{{ openaiReady ? 'AI 分类已配置' : '当前可使用模拟分类' }}</span></div>
    <section class="import-upload" aria-label="上传资料">
      <div class="dropzone" :class="{ dragging }" @dragover.prevent="dragging = true" @dragleave.prevent="dragging = false" @drop.prevent="onDrop">
        <strong>拖入 PDF / PPT / Excel 资料</strong>
        <p class="meta">支持多文件，尽量保留来源图片清晰度；分类任务提交后可离开页面。</p>
        <label for="import-files" class="upload-label">选择资料文件</label>
        <input id="import-files" type="file" multiple accept=".pdf,.pptx,.xlsx,.xlsm" :disabled="uploading" @change="onFiles" />
      </div>
      <ul v-if="files.length" class="import-file-list"><li v-for="(file, index) in files" :key="index">
        <span>{{ file.name }} <small>{{ (file.size / 1024 / 1024).toFixed(1) }} MB</small></span>
        <button class="btn" :disabled="uploading" :aria-label="`移除 ${file.name}`" @click="files.splice(index, 1)">移除</button>
      </li></ul>
      <div class="toolbar import-submit"><div class="field"><label for="tag-backend">分类方式</label>
        <select id="tag-backend" v-model="backend" :disabled="uploading"><option value="openai" :disabled="!openaiReady">GPT-4o-mini</option><option value="mock">模拟分类（测试）</option></select></div>
        <button class="btn primary" :disabled="uploading" @click="runImport">{{ uploading ? '正在提交…' : '上传并开始分类' }}</button>
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
        <template v-else-if="job.total"><progress :value="job.processed" :max="job.total || 1" :aria-label="`${job.name}分类进度`" /><p class="meta">已处理 {{ job.processed }} / {{ job.total }} 张，成功 {{ job.succeeded }} 张，<template v-if="job.filtered">过滤非款式图 {{ job.filtered }} 张，</template>失败 {{ job.failed }} 张<span v-if="job.status === 'tagging'"> · {{ job.current_file }}</span></p></template>
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
