<script setup lang="ts">
import { reactive, ref } from 'vue'
import { searchImage, searchText, type CatalogItem } from '../api'
import FilterBar from '../components/FilterBar.vue'
import ImageGrid from '../components/ImageGrid.vue'

const filters = reactive({
  category: '全部',
  style: '全部',
  season: '全部',
  color: '全部',
  fit: '全部',
})
const mode = ref<'text' | 'image'>('text')
const query = ref('宽松高领毛衣 复古')
const file = ref<File | null>(null)
const items = ref<CatalogItem[]>([])
const loading = ref(false)
const error = ref('')

async function runSearch() {
  loading.value = true
  error.value = ''
  try {
    if (mode.value === 'text') {
      if (!query.value.trim()) throw new Error('请输入搜索描述')
      const data = await searchText(query.value.trim(), filters)
      items.value = data.items
    } else {
      if (!file.value) throw new Error('请上传参考图')
      const data = await searchImage(file.value, filters)
      items.value = data.items
    }
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
    items.value = []
  } finally {
    loading.value = false
  }
}

function onFile(e: Event) {
  const input = e.target as HTMLInputElement
  file.value = input.files?.[0] || null
}
</script>

<template>
  <section class="panel">
    <FilterBar v-model="filters" />
    <div class="toolbar">
      <button class="btn" :class="{ primary: mode === 'text' }" @click="mode = 'text'">文字搜索</button>
      <button class="btn" :class="{ primary: mode === 'image' }" @click="mode = 'image'">以图搜图</button>
    </div>

    <div v-if="mode === 'text'" class="toolbar">
      <div class="field" style="flex: 1; min-width: 220px">
        <label>描述</label>
        <input v-model="query" placeholder="例如：宽松高领毛衣 复古感" @keyup.enter="runSearch" />
      </div>
      <button class="btn primary" :disabled="loading" @click="runSearch">搜索</button>
    </div>
    <div v-else class="toolbar">
      <div class="field">
        <label>参考图</label>
        <input type="file" accept="image/*" @change="onFile" />
      </div>
      <button class="btn primary" :disabled="loading" @click="runSearch">以图搜图</button>
    </div>

    <p class="meta">{{ loading ? '检索中…' : `结果 ${items.length} 条` }}</p>
    <div v-if="error" class="error">{{ error }}</div>
    <ImageGrid :items="items" empty-text="没有匹配结果" />
  </section>
</template>
