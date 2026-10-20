<script setup lang="ts">
import { ref, watch } from 'vue'
import { getReviewHistory, type FashionTags, type ReviewEvent } from '../api'
const props = defineProps<{ id: string }>()
const events = ref<ReviewEvent[]>([])
const error = ref('')
const actions: Record<string, string> = { approve: '审核通过', reject: '驳回分类', resubmit: '重新提交审核',
  record_created: '导入并生成分类', classification_proposed: '提交新的分类', legacy_imported: '迁移历史资料' }
const describe = (tags: FashionTags) => Object.entries(tags).map(([key, value]) => `${key}：${Array.isArray(value) ? value.join('、') || '无' : value || '未提供'}`).join('；')
let latest = 0
watch(() => props.id, async id => {
  const seq = ++latest
  events.value = []; error.value = ''
  try { const data = await getReviewHistory(id); if (seq === latest) events.value = data.items }
  catch { if (seq === latest) error.value = '暂时无法读取历史记录，请刷新后重试' }
}, { immediate: true })
</script>
<template>
  <section class="review-history" aria-label="分类与审核历史"><h3>分类与审核历史</h3>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <details v-for="event in events" :key="event.id"><summary>{{ actions[event.action] || event.action }}<span>{{ event.actor || '系统' }} · {{ new Date(event.created_at).toLocaleString('zh-CN', { hour12: false }) }}</span></summary>
      <p v-if="event.note">{{ event.note }}</p><p v-if="event.after.ai_tags" class="meta">AI 建议：{{ describe(event.after.ai_tags) }}</p>
      <p class="meta">{{ event.action === 'approve' ? '最终分类' : '当次分类' }}：{{ describe(event.after.proposed_tags || event.after.tags) }}</p>
      <p v-if="event.before" class="meta">此前分类：{{ describe(event.before.proposed_tags || event.before.tags) }}</p>
    </details>
  </section>
</template>
