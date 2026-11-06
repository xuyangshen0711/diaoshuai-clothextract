<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { importFiles, inspectImport, type InspectedFile, type ImportJob } from '../api'
import { rememberJob, refreshJobs } from '../composables/importJobs'
import { readEntry } from '../utils/importFiles'
import { fileSizeLabel } from '../utils/source'
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
const skipInvalid = ref(false)
const picker = ref<HTMLInputElement | null>(null)
const folder = ref<HTMLInputElement | null>(null)
let key = 0
const accepted = /\.(pdf|pptx|xlsx|xlsm|png|jpe?g|webp|gif|bmp|tiff?)$/i
const valid = computed(() => inspected.value?.filter(f => ['new', 'replacement'].includes(f.action)).length || 0)
const invalid = computed(() => inspected.value?.filter(f => ['invalid', 'route'].includes(f.action)).length || 0)
const unchanged = computed(() => inspected.value?.filter(f => f.action === 'unchanged').length || 0)
function invalidate() { inspected.value = null; skipInvalid.value = false }
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
    files.value.push({ key: key++, file, path, code: props.factory && props.targetCode && values.length === 1 ? props.targetCode : file.name.replace(/\.[^.]+$/, '').replace(/\s/g, '').toUpperCase(), thumbnail: /^image\//.test(file.type) ? URL.createObjectURL(file) : '' })
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
function uploadName(item: Queued) { return props.factory ? `${item.code.trim().toUpperCase()}${item.file.name.match(/\.[^.]+$/)?.[0] || ''}` : item.path }
async function inspect() {
  busy.value = true; error.value = ''; inspected.value = null
  try { inspected.value = (await inspectImport(files.value.map(f => ({ file: f.file, name: uploadName(f) })), category.value)).items }
  catch (e) { error.value = e instanceof Error ? e.message : String(e) }
  finally { busy.value = false }
}
function routeTable(index: number, kind: string) {
  const item = files.value[index]
  if (!item || !['archive', 'sales'].includes(kind)) return
  emit('table', kind as 'archive' | 'sales', item.file); remove(index)
}
async function submit() {
  if (!inspected.value || !valid.value || (invalid.value && !skipInvalid.value)) return
  busy.value = true; error.value = ''
  const selected = files.value.filter((_, i) => ['new', 'replacement'].includes(inspected.value![i]!.action))
  try {
    const job = await importFiles(selected.map(f => ({ file: f.file, relativePath: uploadName(f), category: category.value })), props.backend)
    rememberJob(job); emit('submitted', job)
    files.value = files.value.filter(f => { if (!selected.includes(f)) return true; if (f.thumbnail) URL.revokeObjectURL(f.thumbnail); return false })
    invalidate(); message.value = '任务已提交。可在下方查看进度，完成后继续审核或补齐资料。'
    await refreshJobs()
  } catch (e) { error.value = e instanceof Error ? e.message : String(e) }
  finally { busy.value = false }
}
onUnmounted(() => files.value.forEach(f => f.thumbnail && URL.revokeObjectURL(f.thumbnail)))
</script>

<template>
  <section class="workspace-panel media-import-panel" :aria-label="factory ? '本厂图片上传' : '外部资料上传'">
    <div class="panel-heading"><div><h3>{{ factory ? '上传本厂款式图片' : '添加参考资料' }}</h3><p class="meta">{{ factory ? '图片与款号一一对应，自动关联已有档案和销量。' : '图片、PDF、PPT 或带图片的 Excel，支持整文件夹上传。' }}</p></div><span class="stage-label">{{ inspected ? '核对后提交' : '选择资料' }}</span></div>
    <div v-if="!factory" class="source-choices" role="group" aria-label="本批资料来源"><button v-for="source in ['网图趋势', '品牌资料包', '市调拍照']" :key="source" :aria-pressed="category === source" :disabled="busy" @click="category = source">{{ source === '网图趋势' ? '淘宝 / 网图' : source }}</button><span class="meta">本批图片自动归入所选来源</span></div>
    <p v-if="factory && targetCode" class="workspace-notice">正在补充款号 <strong>{{ targetCode }}</strong> 的图片。选择一张图片后会自动填入款号。</p>
    <div class="workspace-dropzone" :class="{ dragging }" @dragover.prevent="dragging = true" @dragleave.prevent="dragging = false" @drop.prevent="drop"><span class="upload-glyph" aria-hidden="true">＋</span><strong>{{ factory ? '拖入款式图片，或选择图片文件夹' : '把参考资料拖到这里' }}</strong><p class="meta">{{ factory ? '建议以款号命名，例如 516083.jpg；也可以在下方修改关联款号。' : '可包含子文件夹，抽出的图片会归入本批来源。' }}</p><div class="toolbar"><button class="btn primary" :disabled="busy" @click="picker?.click()">选择文件</button><button class="btn" :disabled="busy" @click="folder?.click()">选择文件夹</button></div>
      <input ref="picker" class="visually-hidden" aria-label="选择待导入文件" type="file" multiple :accept="factory ? 'image/*,.xlsx,.xlsm' : '.pdf,.pptx,.xlsx,.xlsm,image/*'" :disabled="busy" @change="choose" /><input ref="folder" class="visually-hidden" aria-label="选择待导入文件夹" type="file" multiple webkitdirectory :disabled="busy" @change="choose" /></div>
    <div v-if="files.length" class="media-queue"><div class="queue-heading"><h4>本批资料 <span class="meta">{{ files.length }} 个文件</span></h4><button class="text-button" :disabled="busy" @click="clear">清空</button></div>
      <ul><li v-for="(item, index) in files" :key="item.key"><img v-if="item.thumbnail" :src="item.thumbnail" alt="" class="queue-thumb" /><span v-else class="queue-file-icon" aria-hidden="true">文档</span><div class="queue-file"><strong>{{ item.path }}</strong><small class="meta">{{ fileSizeLabel(item.file.size) }}</small><p v-if="inspected?.[index]" :class="['file-verdict', { 'gap-text': ['invalid', 'route'].includes(inspected[index]!.action) }]">{{ inspected[index]!.message }}</p></div>
        <label v-if="factory" class="queue-code">关联款号 <input v-model="item.code" :disabled="busy" @input="invalidate" /></label>
        <button v-if="inspected?.[index]?.action === 'route'" class="btn small" :disabled="busy" @click="routeTable(index, inspected[index]!.kind)">转到{{ inspected[index]!.kind === 'archive' ? '档案' : '销量' }}预览</button><button class="text-button" :disabled="busy" :aria-label="`移除 ${item.path}`" @click="remove(index)">移除</button></li></ul>
      <p v-if="inspected" class="workspace-notice">可导入 {{ valid }} 个，已有相同图片 {{ unchanged }} 个，需处理 {{ invalid }} 个。{{ factory ? '替换图将在审核确认后生效。' : '' }}</p>
      <label v-if="inspected && invalid && valid" class="acknowledge"><input v-model="skipInvalid" type="checkbox" /> 只提交可导入的 {{ valid }} 个，其余保留在清单中</label>
      <div class="toolbar preview-actions"><button v-if="!inspected" class="btn primary" :disabled="busy" @click="inspect">{{ busy ? '正在识别文件…' : '检查文件并预览' }}</button><template v-else><button class="btn primary" :disabled="busy || !valid || (!!invalid && !skipInvalid)" @click="submit">{{ busy ? '正在上传…' : backend === 'deferred' ? `确认保存 ${valid} 个文件` : `确认上传并分类 ${valid} 个文件` }}</button><button class="btn" :disabled="busy" @click="inspect">重新检查</button></template><span class="meta">{{ backend === 'deferred' ? '先保存待分类，可稍后继续' : '自动分类，结果由人工审核确认' }}</span></div>
    </div>
    <p v-if="message" class="workspace-notice" role="status">{{ message }}</p><p v-if="error" class="error" role="alert">{{ error }}</p>
  </section>
</template>
