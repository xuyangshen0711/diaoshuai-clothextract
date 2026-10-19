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
    if (!files.value.length) throw new Error('请先选择文件')
    const data = await importFiles(files.value, backend.value)
    status.value = `已处理 ${data.extracted} 张，等待人工审核；目录共 ${data.catalog_count} 张`
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
    status.value = overwrite ? `已生成新的待审核分类，共 ${data.catalog_count} 张` : `已更新待审核分类`
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
    status.value = `索引已更新（${data.catalog_count} 款）`
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <section class="panel">
    <div class="page-head">
      <h2>导入</h2>
      <p class="meta">{{ openaiReady ? 'GPT-4o-mini' : 'Mock 模式' }}</p>
    </div>

    <p class="meta">AI 分类完成后进入待审核，审核通过才会出现在正式图库。全量重打会重新提交审核，并保留上次人工分类。</p>

    <div class="dropzone">
      <strong>上传 PDF / PPT / Excel</strong>
      <p style="margin: 8px 0 14px">把企划或商品资料拖进来，或点击选择文件</p>
      <input type="file" multiple accept=".pdf,.pptx,.xlsx,.xlsm" @change="onFiles" />
      <p v-if="files.length" style="margin: 12px 0 0">已选 {{ files.length }} 个文件</p>
    </div>

    <div class="toolbar">
      <div class="field" style="min-width: 160px">
        <label>打标方式</label>
        <select v-model="backend">
          <option value="openai">GPT-4o-mini</option>
          <option value="mock">Mock</option>
        </select>
      </div>
    </div>

    <div class="toolbar">
      <button class="btn primary" :disabled="loading" @click="runImport">抽取并打标</button>
      <button class="btn" :disabled="loading" @click="runRetag(false)">补打标签</button>
      <button class="btn" :disabled="loading" @click="runRetag(true)">全量重打</button>
      <button class="btn" :disabled="loading" @click="runRebuild">重建索引</button>
    </div>

    <div v-if="status" class="status" role="status">{{ status }} <RouterLink to="/review" class="review-text-link">前往人工审核</RouterLink></div>
    <div v-if="error" class="error">{{ error }}</div>
  </section>
</template>
