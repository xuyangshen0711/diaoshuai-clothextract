<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { getHealth, importFiles, rebuildIndex, retag } from '../api'

const files = ref<File[]>([])
const backend = ref('openai')
const openaiReady = ref(false)
const status = ref('')
const error = ref('')
const loading = ref(false)

onMounted(async () => {
  const health = await getHealth()
  openaiReady.value = health.openai_ready
  if (!health.openai_ready) backend.value = 'mock'
})

function onFiles(e: Event) {
  const input = e.target as HTMLInputElement
  files.value = input.files ? Array.from(input.files) : []
}

async function runImport() {
  loading.value = true
  error.value = ''
  status.value = ''
  try {
    if (!files.value.length) throw new Error('请先选择 PDF / PPTX / Excel 文件')
    const data = await importFiles(files.value, backend.value)
    status.value = `抽取并打标 ${data.extracted} 张，目录共 ${data.catalog_count} 条（${data.backend}）`
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

async function runRetag(overwrite: boolean) {
  loading.value = true
  error.value = ''
  status.value = ''
  try {
    const data = await retag(backend.value, overwrite)
    status.value = `${overwrite ? '全量' : '增量'}打标完成，目录 ${data.catalog_count} 条`
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

async function runRebuild() {
  loading.value = true
  error.value = ''
  try {
    const data = await rebuildIndex()
    status.value = `索引已重建（${data.catalog_count} 条）`
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <section class="panel">
    <p class="meta">
      支持 PDF / PPTX / XLSX。当前 API Key：
      {{ openaiReady ? '已配置（可用 GPT-4o-mini）' : '未配置（仅 Mock）' }}
    </p>

    <div class="toolbar">
      <div class="field">
        <label>打标后端</label>
        <select v-model="backend">
          <option value="openai">GPT-4o-mini</option>
          <option value="mock">Mock</option>
        </select>
      </div>
      <div class="field">
        <label>上传文件</label>
        <input type="file" multiple accept=".pdf,.pptx,.xlsx,.xlsm" @change="onFiles" />
      </div>
    </div>

    <div class="toolbar">
      <button class="btn primary" :disabled="loading" @click="runImport">抽取并打标</button>
      <button class="btn" :disabled="loading" @click="runRetag(false)">重打未确认项</button>
      <button class="btn" :disabled="loading" @click="runRetag(true)">全量重打标</button>
      <button class="btn" :disabled="loading" @click="runRebuild">重建索引</button>
    </div>

    <div v-if="status" class="status">{{ status }}</div>
    <div v-if="error" class="error">{{ error }}</div>
  </section>
</template>
