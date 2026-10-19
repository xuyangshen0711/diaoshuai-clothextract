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
const query = ref('')
const file = ref<File | null>(null)
const items = ref<CatalogItem[]>([])
const loading = ref(false)
const error = ref('')
const searched = ref(false)

async function runSearch() {
  loading.value = true
  error.value = ''
  searched.value = true
  try {
    if (mode.value === 'text') {
      if (!query.value.trim()) throw new Error('请输入想找的款式')
      const data = await searchText(query.value.trim(), filters)
      items.value = data.items
    } else {
      if (!file.value) throw new Error('请选择一张参考图')
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
  if (file.value) runSearch()
}
</script>

<template>
  <section class="panel">
    <div class="page-head">
      <h2>搜索</h2>
      <p class="meta">{{ loading ? '检索中…' : searched ? `${items.length} 个结果` : ' ' }}</p>
    </div>

    <div class="search-box">
      <div class="mode-switch">
        <button type="button" :class="{ active: mode === 'text' }" @click="mode = 'text'">文字</button>
        <button type="button" :class="{ active: mode === 'image' }" @click="mode = 'image'">以图</button>
      </div>
      <input
        v-if="mode === 'text'"
        v-model="query"
        placeholder="描述你想找的款，例如：宽松高领毛衣"
        @keyup.enter="runSearch"
      />
      <input v-else type="file" accept="image/*" @change="onFile" />
      <button class="btn primary" :disabled="loading" @click="runSearch">搜索</button>
    </div>

    <FilterBar v-model="filters" />
    <div v-if="error" class="error">{{ error }}</div>
    <ImageGrid
      :items="items"
      :empty-text="searched ? '没有匹配结果' : '输入描述或上传图片开始搜索'"
    />
  </section>
</template>
