<script setup lang="ts">
import { ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { getRecord, getSimilarRecords, searchImage, searchText } from '../api'
import { searchMode as mode, searchQuery as query, searchFile as file, searchItems as items,
  searchPerformed as searched, referenceId, referenceUrl, referenceTitle } from '../composables/searchState'
const route = useRoute()
const router = useRouter()
const loading = ref(false)
const error = ref('')
let sequence = 0
async function runSearch() {
  const seq = ++sequence
  loading.value = true; error.value = ''; searched.value = true
  try {
    let data
    if (mode.value === 'text') {
      if (!query.value.trim()) throw new Error('请输入想找的款式')
      data = await searchText(query.value.trim(), {})
      await router.replace({ query: { mode: 'text', q: query.value.trim() } })
    } else if (referenceId.value && !file.value) {
      data = await getSimilarRecords(referenceId.value)
    } else {
      if (!file.value) throw new Error('请选择一张参考图')
      data = await searchImage(file.value, {})
      await router.replace({ query: {} })
    }
    if (seq === sequence) items.value = data.items.filter(item => item.source.source_type !== 'sample')
  } catch (e) { if (seq === sequence) { error.value = e instanceof Error ? e.message : String(e); items.value = [] } }
  finally { if (seq === sequence) loading.value = false }
}
function onFile(e: Event) {
  const selected = (e.target as HTMLInputElement).files?.[0]
  if (!selected) return
  if (referenceUrl.value.startsWith('blob:')) URL.revokeObjectURL(referenceUrl.value)
  file.value = selected; referenceId.value = ''; referenceTitle.value = selected.name
  referenceUrl.value = URL.createObjectURL(selected)
  runSearch()
}
watch(() => route.query.record, async value => {
  if (typeof value !== 'string' || (value === referenceId.value && items.value.length && mode.value === 'image' && !file.value)) return
  const seq = ++sequence
  loading.value = true; error.value = ''; searched.value = true; mode.value = 'image'
  try {
    const [record, results] = await Promise.all([getRecord(value), getSimilarRecords(value)])
    if (seq !== sequence) return
    if (referenceUrl.value.startsWith('blob:')) URL.revokeObjectURL(referenceUrl.value)
    referenceId.value = value; referenceUrl.value = record.image_url
    referenceTitle.value = record.tags.一句话描述 || record.tags.品类; file.value = null
    items.value = results.items
  } catch (e) { if (seq === sequence) { error.value = e instanceof Error ? e.message : String(e); items.value = [] } }
  finally { if (seq === sequence) loading.value = false }
}, { immediate: true })
</script>
<template>
  <main class="panel search-page">
    <div class="page-head"><h2>{{ mode === 'image' ? '以图搜图' : '文字找款' }}</h2><p class="meta">{{ loading ? '检索中…' : searched ? `${items.length} 个结果` : '仅搜索审核通过的款式' }}</p></div>
    <div class="search-box"><div class="mode-switch" role="group" aria-label="检索方式"><button type="button" :class="{ active: mode === 'image' }" :aria-pressed="mode === 'image'" @click="mode = 'image'">以图</button><button type="button" :class="{ active: mode === 'text' }" :aria-pressed="mode === 'text'" @click="mode = 'text'">文字</button></div>
      <input v-if="mode === 'text'" v-model="query" aria-label="款式描述" placeholder="例如：宽松高领毛衣" @keyup.enter="runSearch" /><input v-else type="file" accept="image/*" aria-label="上传参考图" @change="onFile" /><button class="btn primary" :disabled="loading" @click="runSearch">搜索</button></div>
    <div v-if="referenceUrl && mode === 'image'" class="search-reference"><img :src="referenceUrl" alt="本次检索参考图" width="90" height="120" /><div><strong>参考款式</strong><p class="meta">{{ referenceTitle }}</p><RouterLink v-if="referenceId" class="review-text-link" :to="{ name: 'record', params: { id: referenceId } }">查看参考款详情</RouterLink></div></div>
    <p v-if="error" class="error" role="alert">{{ error }}</p><p v-if="!items.length && !loading" class="empty">{{ searched ? '没有匹配的已审核款式，可调整描述或先审核更多图片。' : '上传一张参考图，或输入款式描述。' }}</p>
    <div v-else class="grid"><article v-for="item in items" :key="item.id" class="card"><RouterLink class="card-link" :to="{ name: 'record', params: { id: item.id }, query: { from: route.fullPath } }"><img :src="item.thumbnail_url || item.image_url" :alt="item.tags.一句话描述 || item.tags.品类" loading="lazy" width="180" height="240" /><div class="card-body"><h3>{{ item.tags.品类 }}</h3><p class="sub">{{ item.tags.颜色主色调 }} · {{ item.tags.风格 }}</p></div></RouterLink></article></div>
  </main>
</template>
