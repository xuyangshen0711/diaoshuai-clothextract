<script setup lang="ts">
import { onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { getRecord, getSimilarRecords, searchImage, searchText, type SearchInfo } from '../api'
import { searchMode as mode, searchQuery as query, searchFile as file, searchItems as items,
  searchIncludePending as includePending, searchPerformed as searched, referenceId, referenceUrl,
  referenceTitle } from '../composables/searchState'
const route = useRoute()
const router = useRouter()
const loading = ref(false)
const error = ref('')
const info = ref<SearchInfo | null>(null)
const examples = ['秋日法式，奶油色，松弛感', '极简通勤的羊毛外套', '周末休闲，不要红色']
let sequence = 0
onUnmounted(() => { sequence += 1 })

async function executeSearch() {
  const seq = ++sequence
  loading.value = true; error.value = ''; searched.value = true; info.value = null
  try {
    let data
    if (mode.value === 'text') {
      if (!query.value.trim()) throw new Error('请描述想找的风格、场景或款式')
      const response = await searchText(query.value.trim(), { include_pending: includePending.value })
      data = response
      if (seq === sequence) info.value = response.search_info
    } else if (referenceId.value && !file.value) {
      data = await getSimilarRecords(referenceId.value, includePending.value)
    } else {
      if (!file.value) throw new Error('请选择一张参考图')
      data = await searchImage(file.value, { include_pending: includePending.value })
    }
    if (seq === sequence) items.value = data.items
  } catch (e) {
    if (seq === sequence) { error.value = e instanceof Error ? e.message : String(e); items.value = [] }
  } finally { if (seq === sequence) loading.value = false }
}
async function submitSearch() {
  const target = { name: 'search', query: { mode: mode.value,
    q: mode.value === 'text' ? query.value.trim() || undefined : undefined,
    record: mode.value === 'image' && referenceId.value && !file.value ? referenceId.value : undefined,
    include_pending: includePending.value ? undefined : 'false' } }
  if (router.resolve(target).fullPath === route.fullPath) await executeSearch()
  else await router.push(target)
}
function switchMode(value: 'text' | 'image') {
  router.push({ name: 'search', query: { mode: value, include_pending: includePending.value ? undefined : 'false' } })
}
function useExample(value: string) { query.value = value; submitSearch() }
function onFile(e: Event) {
  const selected = (e.target as HTMLInputElement).files?.[0]
  if (!selected) return
  if (referenceUrl.value.startsWith('blob:')) URL.revokeObjectURL(referenceUrl.value)
  file.value = selected; referenceId.value = ''; referenceTitle.value = selected.name
  referenceUrl.value = URL.createObjectURL(selected)
  submitSearch()
}
watch(() => route.fullPath, async () => {
  const seq = ++sequence
  mode.value = route.query.record || route.query.mode === 'image' ? 'image' : 'text'
  includePending.value = route.query.include_pending !== 'false'
  error.value = ''; info.value = null; loading.value = false
  if (typeof route.query.q === 'string') query.value = route.query.q
  else if (mode.value === 'text') query.value = ''
  if (typeof route.query.record === 'string') {
    loading.value = true
    try {
      const record = await getRecord(route.query.record)
      if (seq !== sequence) return
      if (referenceUrl.value.startsWith('blob:')) URL.revokeObjectURL(referenceUrl.value)
      referenceId.value = record.id; referenceUrl.value = record.image_url
      referenceTitle.value = record.display_tags.一句话描述 || record.display_tags.品类; file.value = null
    } catch (e) {
      if (seq === sequence) { error.value = e instanceof Error ? e.message : String(e); loading.value = false; items.value = [] }
      return
    }
  }
  if ((mode.value === 'text' && query.value.trim()) || (mode.value === 'image' && (file.value || referenceId.value))) {
    await executeSearch()
  } else { items.value = []; searched.value = false }
}, { immediate: true })
</script>

<template>
  <main class="panel search-page">
    <div class="page-head"><div><h2>{{ mode === 'image' ? '以图找款' : '灵感找款' }}</h2>
      <p class="meta">{{ mode === 'text' ? '写下风格、氛围、穿着场景，也可以补充颜色、材质和不想要的元素。' : '上传参考图，或以图库中的款式为参考寻找相似款。' }}</p></div></div>
    <div class="inspiration-modes" role="group" aria-label="检索方式">
      <button class="btn" :aria-pressed="mode === 'text'" @click="switchMode('text')">灵感文字</button>
      <button class="btn" :aria-pressed="mode === 'image'" @click="switchMode('image')">参考图片</button>
    </div>
    <form class="inspiration-form" @submit.prevent="submitSearch">
      <label v-if="mode === 'text'" for="inspiration-query" class="visually-hidden">灵感描述</label>
      <input v-if="mode === 'text'" id="inspiration-query" v-model="query" type="search" maxlength="500" placeholder="例如：秋日法式，奶油色，松弛感" />
      <input v-else type="file" accept="image/*" aria-label="上传参考图" @change="onFile" />
      <button class="btn primary" type="submit" :disabled="loading">{{ loading ? '正在找款…' : '搜索款式' }}</button>
    </form>
    <div v-if="mode === 'text'" class="inspiration-examples" role="group" aria-label="灵感示例"><span>试试看</span><button v-for="example in examples" :key="example" type="button" :disabled="loading" @click="useExample(example)">{{ example }}</button></div>
    <div class="inspiration-options"><label><input v-model="includePending" type="checkbox" @change="submitSearch" />包含未审核图片</label>
      <p class="meta">{{ info?.engine === 'clip-multilingual' ? '本地语义检索' : info ? '本地词汇匹配' : '图片、标签与已有说明综合检索' }} · 不调用付费接口</p></div>
    <div v-if="info?.concepts.length" class="inspiration-concepts" role="group" aria-label="识别到的灵感条件"><span>已识别</span><span v-for="concept in info.concepts" :key="concept" class="inspiration-concept">{{ concept }}</span></div>
    <div v-if="referenceUrl && mode === 'image'" class="search-reference"><img :src="referenceUrl" alt="本次检索参考图" width="90" height="120" /><div><strong>参考款式</strong><p class="meta">{{ referenceTitle }}</p><RouterLink v-if="referenceId" class="review-text-link" :to="{ name: 'record', params: { id: referenceId } }">查看参考款详情</RouterLink></div></div>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p v-if="loading" class="meta" role="status">正在本机匹配款式，首次搜索需要准备图片索引…</p>
    <p v-else-if="searched && !error" class="meta" role="status">找到 {{ items.length }} 款，按相似程度排序；结果可能只符合部分偏好。</p>
    <p v-if="!items.length && !loading && !error" class="empty">{{ searched ? includePending ? '暂时没有匹配款式，试试更具体的颜色、版型或材质描述。' : '暂时没有匹配的已审核款式，可勾选包含未审核图片，或调整描述。' : '选择一个灵感示例，或输入你想找的款式。' }}</p>
    <div v-if="items.length" class="grid"><article v-for="item in items" :key="item.id" class="card"><RouterLink class="card-link" :to="{ name: 'record', params: { id: item.id }, query: { from: route.fullPath } }">
      <img :src="item.thumbnail_url || item.image_url" :alt="item.display_tags.一句话描述 || item.display_tags.品类" loading="lazy" width="180" height="240" />
      <div class="card-body"><div class="library-card-heading"><h3>{{ item.display_tags.品类 }}</h3><span class="library-review-badge" :class="item.review_status">{{ item.review_status === 'approved' ? '已审核' : '未审核' }}</span></div>
      <p class="sub">{{ item.display_tags.颜色主色调 }} · {{ item.display_tags.风格 }}</p><p v-if="item.notes.款号" class="sub">{{ item.notes.款号 }}</p></div>
    </RouterLink></article></div>
  </main>
</template>
