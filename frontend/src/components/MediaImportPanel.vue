<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { createUpload, transferUpload, inspectUpload, commitUpload, type InspectedFile, type ImportJob } from '../api'
import { rememberJob, refreshJobs } from '../composables/importJobs'
import { readEntry } from '../utils/importFiles'
import { fileSizeLabel } from '../utils/source'
import { batchProblem, fileLimit, fileProblem, MAX_BATCH_BYTES, MB } from '../utils/uploadGuidance'
import { trackUpload } from '../composables/uploadActivity'
const props = defineProps<{ factory: boolean; backend: string; targetCode: string }>()
const emit = defineEmits<{ submitted: [job: ImportJob]; table: [kind: 'archive' | 'sales', file: File] }>()
type Queued = { key: number; file: File; path: string; code: string; thumbnail: string }
const files = ref<Queued[]>([])
const category = ref(props.factory ? '本厂产品' : '网图趋势')
const inspected = ref<InspectedFile[] | null>(null)
const busy = ref(false)
const dragging = ref(false)
const error = ref('')
const message = ref('')
const uploadId = ref('')
const uploadedBytes = ref(0)
const totalBytes = computed(() => files.value.reduce((sum, item) => sum + item.file.size, 0))
const fileProblems = computed(() => files.value.map(item => fileProblem(item.path, item.file.size)))
const sizeProblem = computed(() => batchProblem(files.value.length, totalBytes.value))
const blocked = computed(() => Boolean(sizeProblem.value || fileProblems.value.some(Boolean)))
const nearLimit = computed(() => totalBytes.value >= MAX_BATCH_BYTES * .8 || files.value.length >= 800 || files.value.some(item => item.file.size >= fileLimit(item.path) * .8))
const largeBatch = computed(() => totalBytes.value >= 200 * MB)
const offline = ref(!navigator.onLine)
const slowUpload = ref(false)
const currentFile = ref<{ index: number; name: string } | null>(null)
const uploadPercent = computed(() => totalBytes.value ? Math.min(100, Math.floor(uploadedBytes.value / totalBytes.value * 100)) : 0)
let lastProgress = 0
let progressTimer: ReturnType<typeof setInterval> | undefined
let stopPresence: (() => void) | undefined
let transferController: AbortController | undefined
let disposed = false
const phase = ref<'uploading' | 'inspecting' | 'submitting' | ''>('')
const skipInvalid = ref(false)
const picker = ref<HTMLInputElement | null>(null)
const folder = ref<HTMLInputElement | null>(null)
let key = 0
const accepted = /\.(pdf|pptx|xlsx|xlsm|png|jpe?g|webp|gif|bmp|tiff?)$/i
const valid = computed(() => inspected.value?.filter(f => ['new', 'replacement'].includes(f.action)).length || 0)
const invalid = computed(() => inspected.value?.filter(f => ['invalid', 'route'].includes(f.action)).length || 0)
const unchanged = computed(() => inspected.value?.filter(f => f.action === 'unchanged').length || 0)
function invalidate() { inspected.value = null; skipInvalid.value = false; uploadId.value = ''; uploadedBytes.value = 0; error.value = ''; currentFile.value = null }
watch(category, invalidate)
watch(() => props.targetCode, () => { if (props.targetCode && files.value.length === 1) { files.value[0]!.code = props.targetCode; invalidate() } })
function add(values: File[]) {
  error.value = ''; message.value = ''; invalidate()
  const seen = new Set(files.value.map(f => f.path.toLowerCase()))
  let skipped = 0
  for (const file of values) {
    const path = (file.webkitRelativePath || file.name).replace(/\\/g, '/').replace(/^\/+/, '')
    if (!accepted.test(file.name) || path.split('/').some(p => p.startsWith('.') || p === '__MACOSX') || seen.has(path.toLowerCase())) { skipped++; continue }
    seen.add(path.toLowerCase())
    const stem = file.name.replace(/\.[^.]+$/, '').replace(/\s/g, '').normalize('NFKC')
    const defaultCode = stem.includes('*') ? stem : stem.toUpperCase()
    const code = props.factory && props.targetCode && values.length === 1 ? props.targetCode : defaultCode
    files.value.push({ key: key++, file, path, code, thumbnail: /^image\//.test(file.type) ? URL.createObjectURL(file) : '' })
  }
  if (skipped) message.value = `已略过 ${skipped} 个重复文件或不支持的文件。`
  if (!values.length) error.value = '文件夹为空，请选择包含资料的文件夹。'
}
function choose(event: Event) { const input = event.target as HTMLInputElement; add(Array.from(input.files || [])); input.value = '' }
async function drop(event: DragEvent) {
  dragging.value = false
  if (busy.value) return
  const entries = Array.from(event.dataTransfer?.items || []).map(i => i.webkitGetAsEntry?.()).filter((e): e is FileSystemEntry => Boolean(e))
  if (entries.length) { const values: File[] = []; for (const e of entries) values.push(...await readEntry(e)); add(values) }
  else add(Array.from(event.dataTransfer?.files || []))
}
function remove(index: number) { const file = files.value.splice(index, 1)[0]; if (file?.thumbnail) URL.revokeObjectURL(file.thumbnail); invalidate() }
function clear() { while (files.value.length) remove(0) }
async function inspect() {
  if (busy.value || blocked.value || offline.value) return
  busy.value = true; error.value = ''; inspected.value = null
  phase.value = 'uploading'; slowUpload.value = false; lastProgress = Date.now()
  progressTimer = setInterval(() => { slowUpload.value = phase.value === 'uploading' && Date.now() - lastProgress > 20000 }, 5000)
  try {
    const entries = files.value.map(item => ({
      file: item.file, name: item.path, styleCode: props.factory ? item.code : undefined,
    }))
    if (!uploadId.value) uploadId.value = (await createUpload(entries, category.value)).id
    if (disposed) return
    stopPresence = trackUpload(uploadId.value)
    transferController = new AbortController()
    await transferUpload(uploadId.value, entries, (sent, _, current) => {
      if (sent > uploadedBytes.value) { lastProgress = Date.now(); slowUpload.value = false }
      uploadedBytes.value = sent
      if (current) currentFile.value = current
    }, transferController.signal)
    stopPresence(); stopPresence = undefined
    if (disposed) return
    phase.value = 'inspecting'
    inspected.value = (await inspectUpload(uploadId.value)).items
  }
  catch (e) {
    error.value = e instanceof TypeError ? '网络连接中断或服务器暂时无法连接。请检查网络后重试。' : e instanceof Error ? e.message : String(e)
    if (error.value.includes('已过期')) { uploadId.value = ''; uploadedBytes.value = 0 }
  }
  finally { stopPresence?.(); stopPresence = undefined; clearInterval(progressTimer); busy.value = false; phase.value = ''; slowUpload.value = false }
}
function routeTable(index: number, kind: string) {
  const item = files.value[index]
  if (!item || !['archive', 'sales'].includes(kind)) return
  emit('table', kind as 'archive' | 'sales', item.file); remove(index)
}
async function submit() {
  if (!inspected.value || !valid.value || (invalid.value && !skipInvalid.value)) return
  busy.value = true; error.value = ''
  phase.value = 'submitting'
  const selected = files.value.filter((_, i) => ['new', 'replacement'].includes(inspected.value![i]!.action))
  try {
    const indices = files.value.flatMap((item, index) => selected.includes(item) ? [index] : [])
    const job = await commitUpload(uploadId.value, indices, props.backend)
    rememberJob(job); emit('submitted', job)
    files.value = files.value.filter(f => { if (!selected.includes(f)) return true; if (f.thumbnail) URL.revokeObjectURL(f.thumbnail); return false })
    invalidate(); message.value = props.backend === 'deferred' ? '原图已提交到任务，尚未进入图库。配置 AI 后可在下方继续分类。' : '任务已提交。有效 AI 建议生成后自动入库，可在下方查看进度。'
    await refreshJobs()
  } catch (e) { error.value = e instanceof Error ? e.message : String(e) }
  finally { busy.value = false; phase.value = '' }
}
function networkChanged() { offline.value = !navigator.onLine }
function beforeLeave(event: BeforeUnloadEvent) { if (busy.value) { event.preventDefault(); event.returnValue = '' } }
onMounted(() => { window.addEventListener('online', networkChanged); window.addEventListener('offline', networkChanged); window.addEventListener('beforeunload', beforeLeave) })
onUnmounted(() => {
  disposed = true; transferController?.abort(); stopPresence?.(); stopPresence = undefined; clearInterval(progressTimer)
  window.removeEventListener('online', networkChanged); window.removeEventListener('offline', networkChanged); window.removeEventListener('beforeunload', beforeLeave)
  files.value.forEach(f => f.thumbnail && URL.revokeObjectURL(f.thumbnail))
})
</script>

<template>
  <section class="workspace-panel media-import-panel" :aria-label="factory ? '本厂图片上传' : '外部资料上传'">
    <div class="panel-heading"><div><h3>{{ factory ? '上传本厂款式图片' : '添加参考资料' }}</h3><p class="meta">{{ factory ? '图片与款号一一对应，自动关联已有档案和订单数据。' : '图片、PDF、PPTX 或带图片的 Excel，支持整文件夹上传。' }}</p></div><span class="stage-label">{{ inspected ? '核对后提交' : '选择资料' }}</span></div>
    <div v-if="!factory" class="source-choices" role="group" aria-label="本批资料来源"><button v-for="source in ['网图趋势', '品牌资料包', '市调拍照']" :key="source" :aria-pressed="category === source" :disabled="busy" @click="category = source">{{ source === '网图趋势' ? '淘宝 / 网图' : source }}</button><span class="meta">本批图片自动归入所选来源</span></div>
    <p v-if="factory && targetCode" class="workspace-notice">正在补充款号 <strong>{{ targetCode }}</strong> 的图片。选择一张图片后会自动填入款号。</p>
    <div class="workspace-dropzone" :class="{ dragging }" @dragover.prevent="dragging = true" @dragleave.prevent="dragging = false" @drop.prevent="drop"><span class="upload-glyph" aria-hidden="true">＋</span><strong>{{ factory ? '拖入款式图片，或选择图片文件夹' : '把参考资料拖到这里' }}</strong><p class="meta">{{ factory ? '可保留原文件名，在下方填写完整的关联款号，例如 581188*6。' : '可包含子文件夹，抽出的图片会归入本批来源。' }}</p><div class="toolbar"><button class="btn primary" :disabled="busy" @click="picker?.click()">选择文件</button><button class="btn" :disabled="busy" @click="folder?.click()">选择文件夹</button></div>
      <input ref="picker" class="visually-hidden" aria-label="选择待导入文件" type="file" multiple :accept="factory ? 'image/*,.xlsx,.xlsm' : '.pdf,.pptx,.xlsx,.xlsm,image/*'" :disabled="busy" @change="choose" /><input ref="folder" class="visually-hidden" aria-label="选择待导入文件夹" type="file" multiple webkitdirectory :disabled="busy" @change="choose" /></div>
    <p v-if="!factory" class="meta">旧版 .ppt 请先另存为 .pptx。抽图支持组合内的图片；复杂拼贴或矢量图建议先转 PDF，并检查抽图结果。</p>
    <p class="meta">单张图片最多 100 MB，文档最多 512 MB，单批最多 2 GB / 1000 个文件。超过 200 MB 也可上传；上传期间请保持页面打开，提交任务后可离开。</p>
    <p v-if="offline" class="upload-advice warning" role="alert">当前网络已断开。请保持页面打开，恢复网络后再上传；如果上传已暂停，可点击「继续上传并检查」，接着传剩余部分。</p>
    <div v-if="files.length && !inspected && !busy" class="upload-advice" :class="{ warning: blocked || nearLimit }" role="status">
      <template v-if="blocked"><strong>本批有需要处理的文件</strong><p>{{ sizeProblem || '下方已标出需要处理的文件。压缩、拆分或移除后，即可继续上传。' }}</p></template>
      <template v-else-if="nearLimit"><strong>本批资料已接近上传上限</strong><p>仍可上传。建议减少本批文件，或先压缩文档中的图片；这样上传和检查会更快。</p></template>
      <template v-else-if="largeBatch"><strong>本批资料较大，可以继续上传</strong><p>超过 200 MB 也支持上传，耗时取决于网络。若想缩短等待，可先压缩文档中的图片，或减少本批文件。</p></template>
      <template v-else><span>可以一起上传这 {{ files.length }} 个文件，系统会逐个传输；确认后再排队识别。</span></template>
    </div>
    <div v-if="files.length && (busy || uploadedBytes || uploadId)" class="workspace-notice upload-progress" role="status" aria-live="polite">
      <strong>{{ phase === 'inspecting' ? '上传完成，正在检查文件' : phase === 'submitting' ? '正在提交识别任务' : inspected ? '文件已上传并检查完成' : busy ? `正在上传 · ${uploadPercent}%` : uploadedBytes === totalBytes ? '上传完成，等待检查' : '上传已暂停' }}</strong>
      <p v-if="phase === 'uploading' && currentFile" class="current-upload">第 {{ currentFile.index + 1 }} / {{ files.length }} 个：{{ currentFile.name }}</p>
      <progress :value="uploadedBytes" :max="totalBytes || 1" aria-label="文件上传进度" />
      <p class="meta">已上传 {{ fileSizeLabel(uploadedBytes) }} / {{ fileSizeLabel(totalBytes) }}</p>
      <p v-if="phase === 'uploading'" class="meta">请保持页面打开。上传完成后还需核对文件并确认识别。</p>
      <p v-if="phase === 'inspecting'" class="meta">正在核对文件内容，文档较大时会久一些。无需重新上传；此时还未调用 AI。</p>
      <p v-if="slowUpload && !offline" class="meta">这一段传输还未完成，暂未收到新的进度。网络较慢时可能需要更久，请保持页面打开。</p>
      <p v-if="!busy && !inspected && uploadId" class="meta">网络正常后点击「继续上传并检查」，会接着传剩余部分。刷新或关闭页面后需要重新选择文件。</p>
    </div>
    <div v-if="files.length" class="media-queue"><div class="queue-heading"><h4>本批资料 <span class="meta">{{ files.length }} 个文件 · {{ fileSizeLabel(totalBytes) }}</span></h4><button class="text-button" :disabled="busy" @click="clear">清空</button></div>
      <ul><li v-for="(item, index) in files" :key="item.key"><img v-if="item.thumbnail" :src="item.thumbnail" alt="" class="queue-thumb" /><span v-else class="queue-file-icon" aria-hidden="true">文档</span><div class="queue-file"><strong>{{ item.path }}</strong><small class="meta">{{ fileSizeLabel(item.file.size) }}</small><p v-if="fileProblems[index]" class="file-verdict gap-text">{{ fileProblems[index] }}</p><p v-if="inspected?.[index]" :class="['file-verdict', { 'gap-text': ['invalid', 'route'].includes(inspected[index]!.action) }]">{{ inspected[index]!.message }}</p></div>
        <label v-if="factory" class="queue-code">关联款号 <input v-model="item.code" :disabled="busy" @input="invalidate" /></label>
        <button v-if="inspected?.[index]?.action === 'route'" class="btn small" :disabled="busy" @click="routeTable(index, inspected[index]!.kind)">转到{{ inspected[index]!.kind === 'archive' ? '档案' : '订单数据' }}预览</button><button class="text-button" :disabled="busy" :aria-label="`移除 ${item.path}`" @click="remove(index)">移除</button></li></ul>
      <p v-if="inspected" class="workspace-notice">可导入 {{ valid }} 个，已有相同图片 {{ unchanged }} 个，需处理 {{ invalid }} 个。{{ factory ? '替换图识别成功后生效，原图和人工标签会保留。' : '' }}</p>
      <label v-if="inspected && invalid && valid" class="acknowledge"><input v-model="skipInvalid" type="checkbox" /> 只提交可导入的 {{ valid }} 个，其余保留在清单中</label>
      <div class="toolbar preview-actions"><button v-if="!inspected" class="btn primary" :disabled="busy || blocked || offline" @click="inspect">{{ busy ? phase === 'uploading' ? '正在上传文件…' : '正在检查文件…' : uploadId ? '继续上传并检查' : '上传并检查文件' }}</button><template v-else><button class="btn primary" :disabled="busy || offline || !valid || (!!invalid && !skipInvalid)" @click="submit">{{ busy ? '正在提交…' : backend === 'deferred' ? `确认保存 ${valid} 个文件` : `开始识别 ${valid} 个文件` }}</button><button class="btn" :disabled="busy || offline || blocked" @click="inspect">重新检查</button></template><span class="meta">{{ backend === 'deferred' ? '先保留原图，AI 分类前不进入图库' : '检查文件不调用 AI，确认后才开始识别' }}</span></div>
    </div>
    <p v-if="message" class="workspace-notice" role="status">{{ message }}</p><p v-if="error" class="error" role="alert">{{ error }}</p>
  </section>
</template>
