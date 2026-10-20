<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { getRecord, type CatalogItem } from '../api'
import RecordHistory from '../components/RecordHistory.vue'
import { formatTime, importTime, sourceType } from '../utils/source'
const route = useRoute()
const item = ref<CatalogItem | null>(null)
const loading = ref(false)
const error = ref('')
const labels = { pending: '待审核', approved: '已通过', rejected: '已驳回' }
const back = computed(() => {
  const path = typeof route.query.from === 'string' ? route.query.from : '/'
  return path.startsWith('/') && !path.startsWith('//') && !path.startsWith('/records/') ? path : '/'
})
const tags = computed(() => item.value?.display_tags)
const display = (value: unknown) => Array.isArray(value) ? value.join('、') || '无' : String(value || '未提供')
let latest = 0
watch(() => route.params.id, async id => {
  const seq = ++latest
  loading.value = true; error.value = ''; item.value = null
  try { const result = await getRecord(String(id)); if (seq === latest) item.value = result }
  catch (e) { if (seq === latest) error.value = e instanceof Error ? e.message : String(e) }
  finally { if (seq === latest) loading.value = false }
}, { immediate: true })
</script>
<template>
  <main class="record-page">
    <div class="page-head"><RouterLink class="btn" :to="back">返回列表</RouterLink><span class="meta">款式详情</span></div>
    <p v-if="loading" class="empty" role="status">正在读取款式…</p><p v-if="error" class="error" role="alert">{{ error }} <RouterLink to="/">返回图库</RouterLink></p>
    <div v-if="item && tags" class="record-layout">
      <section class="record-image-panel" aria-label="款式原图"><a :href="item.image_url" target="_blank" rel="noopener"><img :src="item.image_url" :alt="tags.一句话描述 || tags.品类" width="600" height="800" /></a>
        <p class="meta">点击图片查看完整原图，保留原始比例。</p>
        <div class="toolbar"><a class="btn" :href="`${item.image_url}?download=true`" download>下载原图</a><RouterLink class="btn primary" :to="{ name: 'search', query: { record: item.id } }">找相似款</RouterLink></div>
      </section>
      <section class="record-information" aria-label="款式信息">
        <div class="record-title"><h2>{{ tags.品类 }}</h2><span class="task-state" :class="item.review_status">{{ labels[item.review_status] }}</span></div>
        <p class="record-description">{{ tags.一句话描述 || '暂无款式描述' }}</p>
        <p v-if="item.review_status === 'pending'" class="meta">当前分类已在主页展示，尚待人工审核确认。文字与以图检索仅返回已审核图片。</p>
        <p v-else-if="item.review_status === 'rejected'" class="meta">此图片已驳回，不在主页或搜索结果中展示。</p>
        <dl class="record-source"><div><dt>资料类型</dt><dd>{{ sourceType(item.source) }}</dd></div><div><dt>导入时间</dt><dd><time :datetime="importTime(item.source)">{{ formatTime(importTime(item.source)) }}</time></dd></div></dl>
        <dl class="record-source"><div><dt>来源资料</dt><dd>{{ item.source.source_file }}</dd></div><div><dt>所在位置</dt><dd>{{ item.source.location }}</dd></div><div v-if="item.reviewer"><dt>上次审核</dt><dd>{{ item.reviewer }} · {{ item.reviewed_at ? new Date(item.reviewed_at).toLocaleString('zh-CN', { hour12: false }) : '' }}</dd></div><div v-if="item.review_note"><dt>审核意见</dt><dd>{{ item.review_note }}</dd></div></dl>
        <div class="toolbar"><a v-if="item.source_url" class="btn" :href="item.source_url" download>下载来源资料</a><RouterLink class="btn" :to="{ path: '/review', query: { status: item.review_status, selected: item.id } }">{{ item.review_status === 'pending' ? '前往审核此款' : '查看审核结果' }}</RouterLink></div>
        <div class="record-tag-table"><table><caption>分类标签对照</caption><thead><tr><th scope="col">标签</th><th scope="col">AI 建议{{ item.ai_tags_source === 'mock' ? '（模拟）' : '' }}</th><th scope="col">{{ item.review_status === 'approved' ? '最终分类' : '待审核分类' }}</th></tr></thead><tbody>
          <tr v-for="(value, key) in tags" :key="key" :class="{ changed: item.ai_tags && display(item.ai_tags[key]) !== display(value) }"><th scope="row">{{ key }}</th><td>{{ display(item.ai_tags?.[key]) }}</td><td>{{ display(value) }}</td></tr>
        </tbody></table></div>
        <RecordHistory :id="item.id" />
      </section>
    </div>
  </main>
</template>
