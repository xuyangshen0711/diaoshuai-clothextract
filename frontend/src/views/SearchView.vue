<script setup lang="ts">
import { ref } from 'vue'
import { searchImage, searchText, type CatalogItem } from '../api'

const mode = ref<'text' | 'image'>('image')
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
      const data = await searchText(query.value.trim(), {})
      items.value = data.items.filter((x) => x.source.source_type !== 'sample')
    } else {
      if (!file.value) throw new Error('请选择一张参考图')
      const data = await searchImage(file.value, {})
      items.value = data.items.filter((x) => x.source.source_type !== 'sample')
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
      <h2>以图搜图</h2>
      <p class="meta">{{ loading ? '检索中…' : searched ? `${items.length} 个结果` : '' }}</p>
    </div>

    <div class="search-box">
      <div class="mode-switch">
        <button type="button" :class="{ active: mode === 'image' }" @click="mode = 'image'">以图</button>
        <button type="button" :class="{ active: mode === 'text' }" @click="mode = 'text'">文字</button>
      </div>
      <input
        v-if="mode === 'text'"
        v-model="query"
        placeholder="例如：宽松高领毛衣"
        @keyup.enter="runSearch"
      />
      <input v-else type="file" accept="image/*" @change="onFile" />
      <button class="btn primary" :disabled="loading" @click="runSearch">搜索</button>
    </div>

    <div v-if="error" class="error">{{ error }}</div>
    <div v-if="!items.length && !loading" class="empty">
      {{ searched ? '没有匹配结果' : '上传一张图，找相似款' }}
    </div>
    <div v-else class="grid">
      <article v-for="item in items" :key="item.id" class="card">
        <img :src="item.image_url" :alt="item.tags.品类" loading="lazy" />
        <div class="card-body">
          <h3>{{ item.tags.品类 }}</h3>
          <p class="sub">{{ item.tags.颜色主色调 }} · {{ item.tags.风格 }}</p>
        </div>
      </article>
    </div>
  </section>
</template>
