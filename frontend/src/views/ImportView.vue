<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { classifyBatch, getHealth, getTableHistory, rebuildIndex, retag, retryJob, type ImportJob, type TableImportResult } from '../api'
import { isActive, jobs, jobsError, refreshJobs, rememberJob } from '../composables/importJobs'
import FactoryCompleteness from '../components/FactoryCompleteness.vue'
import MediaImportPanel from '../components/MediaImportPanel.vue'
import TableImportPanel from '../components/TableImportPanel.vue'
import '../styles/import-workspace.css'

const route = useRoute()
const router = useRouter()
const factory = computed(() => route.query.mode === 'factory')
const task = computed(() => route.query.task === 'archive' ? 'archive' : route.query.task === 'sales' ? 'sales' : 'images')
const targetCode = computed(() => typeof route.query.style === 'string' ? route.query.style : '')
const selectedBackend = ref('auto')
const openai = ref(false)
const deepseek = ref(false)
const healthReady = ref(false)
const defaultBackend = ref('openai')
const backend = computed(() => selectedBackend.value !== 'auto' ? selectedBackend.value :
  defaultBackend.value === 'deepseek' && deepseek.value ? 'deepseek' : openai.value ? 'openai' : deepseek.value ? 'deepseek' : 'deferred')
const history = ref<TableImportResult[]>([])
const historyError = ref('')
const error = ref('')
const notice = ref('')
const busy = ref('')
const refreshNumber = ref(0)
const tablePanel = ref<InstanceType<typeof TableImportPanel> | null>(null)
const jobSignature = computed(() => jobs.value.map(j => `${j.id}:${j.status}`).join('|'))
const refreshKey = computed(() => `${refreshNumber.value}|${jobSignature.value}`)
const visibleJobs = computed(() => jobs.value.filter(j => {
  if (!j.source_categories?.length || j.kind === 'retag') return true
  return factory.value ? j.source_categories.includes('本厂产品') : j.source_categories.some(c => c !== '本厂产品')
}))
const labels: Record<ImportJob['status'], string> = { queued: '等待处理', extracting: '正在抽图', tagging: '正在分类', completed: '已完成', partial: '部分完成', failed: '处理失败', interrupted: '等待继续' }
function backendLabel(value: string) { return value === 'deferred' ? '保存待分类' : value === 'mock' ? '模拟分类（测试）' : value === 'deepseek' ? 'DeepSeek 分类' : 'AI 分类' }
function time(value: string) { return new Date(value).toLocaleString('zh-CN', { hour12: false }) }
function duration(value: number) { return value < 60 ? `${Math.round(value)} 秒` : `${Math.floor(value / 60)} 分 ${Math.round(value % 60)} 秒` }
function setMode(value: boolean) { router.replace({ path: '/import', query: value ? { mode: 'factory' } : {} }) }
async function startTask(kind: 'images' | 'archive' | 'sales', code = '') {
  await router.replace({ path: '/import', query: { mode: 'factory', task: kind, ...(code ? { style: code } : {}) } })
  await nextTick(); document.getElementById('import-work-area')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}
async function routeTable(kind: 'archive' | 'sales', file: File) { await startTask(kind); await nextTick(); tablePanel.value?.openFile(file) }
async function loadHistory() {
  try { history.value = await getTableHistory(); historyError.value = '' }
  catch (e) { historyError.value = e instanceof Error ? e.message : String(e) }
}
function tableCompleted() { refreshNumber.value++; loadHistory() }
async function run(action: () => Promise<unknown>, key: string) {
  busy.value = key; error.value = ''; notice.value = ''
  try { await action() }
  catch (e) { error.value = e instanceof Error ? e.message : String(e) }
  finally { busy.value = '' }
}
async function retry(job: ImportJob) { await run(async () => { rememberJob(await retryJob(job.id)); await refreshJobs() }, job.id) }
async function classify(job: ImportJob) { await run(async () => { rememberJob(await classifyBatch(backend.value, job.id)); await refreshJobs(); notice.value = '已为本批待分类图片创建任务。' }, job.id) }
async function retagAll(overwrite: boolean) {
  if (overwrite && !window.confirm('全量重新分类会创建新的审核建议，可能产生分类费用。确认继续？')) return
  await run(async () => { rememberJob(await retag(backend.value, overwrite)); await refreshJobs() }, 'retag')
}
watch(factory, value => { if (value) loadHistory() })
onMounted(async () => {
  try {
    const health = await getHealth(); openai.value = health.openai_ready; deepseek.value = Boolean(health.deepseek_ready); defaultBackend.value = health.default_backend || 'openai'; healthReady.value = true
  } catch { error.value = '无法读取分类服务状态，请刷新重试。' }
  refreshJobs(); if (factory.value) loadHistory()
})
</script>

<template>
  <main class="import-workspace">
    <div class="workspace-heading"><div><h2>导入资料</h2><p class="meta">按资料用途开始，系统会帮你核对关联和待补内容。</p></div><RouterLink class="text-button" :to="{ path: '/', query: factory ? { source_category: '本厂产品' } : {} }">返回图库</RouterLink></div>
    <div class="import-purpose" role="group" aria-label="资料用途"><button :aria-pressed="!factory" @click="setMode(false)"><span class="purpose-icon" aria-hidden="true">▧</span><span><strong>外部参考资料</strong><small>淘宝 / 网图、品牌资料、市调照片</small></span></button><button :aria-pressed="factory" @click="setMode(true)"><span class="purpose-icon" aria-hidden="true">▤</span><span><strong>本厂产品</strong><small>款式图片、款式档案与销量</small></span></button></div>
    <template v-if="factory"><FactoryCompleteness :refresh-key="refreshKey" @task="startTask" /></template>
    <div id="import-work-area" class="import-work-area">
      <div v-if="factory" class="factory-task-nav" role="group" aria-label="本厂导入类型"><button :aria-pressed="task === 'images'" @click="startTask('images')"><strong>款式图片</strong><small>补图片、关联款号</small></button><button :aria-pressed="task === 'archive'" @click="startTask('archive')"><strong>款式档案</strong><small>款号、颜色、尺码等资料</small></button><button :aria-pressed="task === 'sales'" @click="startTask('sales')"><strong>销量数据</strong><small>订单明细、历史汇总</small></button></div>
      <MediaImportPanel v-show="!factory" :factory="false" :backend="backend" target-code="" @table="routeTable" @submitted="refreshNumber++" />
      <MediaImportPanel v-show="factory && task === 'images'" :factory="true" :backend="backend" :target-code="targetCode" @table="routeTable" @submitted="refreshNumber++" />
      <TableImportPanel ref="tablePanel" v-show="factory && task !== 'images'" :kind="task === 'sales' ? 'sales' : 'archive'" :target-code="targetCode" @completed="tableCompleted" @task="startTask" />
    </div>
    <p v-if="healthReady && backend === 'deferred' && (!factory || task === 'images')" class="workspace-notice">当前未启用自动分类。图片可先保存为“待分类”，配置服务后可按批次继续，或直接人工审核。</p>
    <p v-if="notice" class="workspace-notice" role="status">{{ notice }}</p><p v-if="error" class="error" role="alert">{{ error }}</p>
    <section v-if="factory" class="table-history-section" aria-label="表格导入记录"><div class="section-heading"><h3>表格导入记录 <span class="meta">最近 {{ history.length }} 批</span></h3><button class="text-button" @click="loadHistory">刷新记录</button></div>
      <p v-if="historyError" class="error" role="alert">{{ historyError }}</p><p v-else-if="!history.length" class="workspace-empty">确认导入后，结果会保存在这里。</p>
      <details v-for="report in history" :key="report.id" class="table-history-row"><summary><span><strong>{{ report.source_file }}</strong><small>{{ time(report.created_at) }} · {{ report.kind === 'archive' ? '款式档案' : '销量' }}</small></span><span>新增 {{ report.imported }} / 更新 {{ report.updated }} / 无变化 {{ report.unchanged }}</span></summary><div><p>关联款号：{{ report.style_codes.slice(0, 20).join('、') }}{{ report.style_codes.length > 20 ? ` 等 ${report.style_codes.length} 款` : '' }}</p><p v-if="report.kind === 'sales'">该次导入完成时，这些款号计入 {{ report.stored_quantity?.toLocaleString() }} 件，¥{{ report.stored_amount?.toLocaleString() }}。</p><p v-if="report.issue_count" class="meta">跳过 {{ report.issue_count }} 行异常。</p><p class="meta">待补资料请以上方实时清单为准。</p></div></details>
    </section>
    <section class="workspace-jobs" aria-label="图片处理任务"><div class="section-heading"><h3>图片处理任务 <span class="meta">{{ visibleJobs.length }} 批</span></h3><button class="text-button" @click="refreshJobs">刷新进度</button></div>
      <p v-if="jobsError" class="error" role="alert">{{ jobsError }}</p><p v-if="!visibleJobs.length" class="workspace-empty">上传后的处理进度会显示在这里，切换页面也会继续。</p>
      <article v-for="job in visibleJobs" :id="`job-${job.id}`" :key="job.id" class="workspace-job"><div class="task-heading"><strong>{{ job.name }}</strong><span class="task-state" :class="job.status">{{ labels[job.status] }}</span></div><p class="meta">{{ time(job.created_at) }} · {{ backendLabel(job.backend) }} · 已用 {{ duration(job.elapsed_seconds) }}<span v-if="job.eta_seconds"> · 预计剩余 {{ duration(job.eta_seconds) }}</span></p>
        <progress v-if="isActive(job)" :value="job.status === 'extracting' ? job.files_processed : job.processed" :max="job.status === 'extracting' ? job.files_total || 1 : job.total || 1" aria-label="图片处理进度" />
        <p v-if="isActive(job)" class="meta">{{ job.status === 'queued' ? '已排队，无需重复提交。' : job.current_file }}</p><p v-else>{{ job.message }}</p><p v-if="job.total" class="meta">处理 {{ job.processed }} / {{ job.total }} 张，保留 {{ job.succeeded }} 张<span v-if="job.skipped">，跳过相同图 {{ job.skipped }} 张</span><span v-if="job.filtered">，过滤 {{ job.filtered }} 张</span><span v-if="job.failed">，失败 {{ job.failed }} 张</span></p>
        <div class="toolbar"><RouterLink v-if="job.succeeded && !isActive(job)" class="btn small" :to="{ path: '/review', query: job.kind === 'import' ? { batch: job.id } : {} }">审核本批图片</RouterLink><button v-if="job.backend === 'deferred' && job.succeeded && !isActive(job) && backend !== 'deferred' && backend !== 'mock'" class="btn small" :disabled="!!busy" @click="classify(job)">继续分类本批</button><button v-if="['partial', 'failed', 'interrupted'].includes(job.status)" class="btn small" :disabled="!!busy" @click="retry(job)">重试未完成项</button></div><details v-if="job.errors.length"><summary>查看 {{ job.errors.length }} 项异常</summary><ul><li v-for="(item, index) in job.errors" :key="index">{{ item.name }}：{{ item.message }}</li></ul></details>
      </article>
    </section>
    <details class="workspace-advanced"><summary>高级设置与维护</summary><p class="meta">正常导入使用自动分类。模拟分类仅用于测试，不会生成真实识别结果。</p><label>分类方式 <select v-model="selectedBackend"><option value="auto">自动选择可用服务</option><option value="deferred">仅保存，稍后分类</option><option value="openai" :disabled="!openai">OpenAI{{ openai ? '' : '（未配置）' }}</option><option value="deepseek" :disabled="!deepseek">DeepSeek{{ deepseek ? '' : '（未配置）' }}</option><option value="mock">模拟分类（测试）</option></select></label><div class="toolbar"><button class="btn" :disabled="!!busy || backend === 'deferred'" @click="retagAll(false)">补充待分类图片</button><button class="btn" :disabled="!!busy || backend === 'deferred'" @click="retagAll(true)">全量重新分类</button><button class="btn" :disabled="!!busy" @click="run(async () => { await rebuildIndex(); notice = '检索索引已更新。' }, 'index')">刷新检索索引</button></div></details>
  </main>
</template>
