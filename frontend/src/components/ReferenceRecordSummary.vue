<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { assignSourceCategory, getRecord, type CatalogItem } from '../api'
import CoverEditor from './CoverEditor.vue'
import { formatTime, importTime, sourceType, swatchColor, visibleDescription } from '../utils/source'
import { hasUpperBody, knownAttribute, recordCategory, recordSleeve, referenceTitle } from '../utils/recordPresentation'

const props = defineProps<{ item: CatalogItem; categoryChoices: string[] }>()
const emit = defineEmits<{ updated: [item: CatalogItem]; image: []; tags: [] }>()
const title = computed(() => referenceTitle(props.item))
const category = computed(() => recordCategory(props.item))
const more = ref<HTMLDetailsElement | null>(null)
const editingCategory = ref(false)
const categoryInput = ref<HTMLSelectElement | null>(null)
const savingCategory = ref(false)
const categoryError = ref('')
const categoryMessage = ref('')
const reviewLabels = { pending: '待审核', approved: '已审核', rejected: '已驳回' }
const missingCategory = computed(() => !props.item.source_category || props.item.source_category === '未分类')
const description = computed(() => {
  const tags = props.item.display_tags
  const words = [knownAttribute(tags.版型) ? `${tags.版型}剪裁` : '', knownAttribute(tags.风格) ? `${tags.风格}风格` : ''].filter(Boolean)
  return words.join('，') || visibleDescription(tags.一句话描述, props.item.review_status)
})
const attributes = computed(() => {
  const item = props.item; const tags = item.display_tags
  const rows = [
    { label: '品类', value: category.value }, { label: '季节', value: tags.季节 },
    { label: '织物类型', value: item.fabric || tags.面料 }, { label: '版型', value: tags.版型 },
  ]
  if (hasUpperBody(item)) rows.push({ label: '领型', value: tags.领型 }, { label: '袖长', value: recordSleeve(item) })
  rows.push({ label: '颜色', value: tags.颜色主色调 }, { label: '风格', value: tags.风格 })
  return rows.map(row => ({ ...row, value: knownAttribute(row.value) || '未提供' }))
})
const location = computed(() => props.item.source.location.replace(/^page\s+(\d+)$/i, '第 $1 页').replace(/^slide\s+(\d+)$/i, '第 $1 张幻灯片'))
const lowResolution = computed(() => props.item.image_width && props.item.image_height && Math.max(props.item.image_width, props.item.image_height) < 600)
const stateNote = computed(() => {
  const item = props.item
  if (item.review_status === 'rejected') return '此图片已驳回，不在图库或搜索结果中展示。'
  if (item.review_status === 'approved') return '分类已由人工审核确认。'
  if (item.tags_source === 'deferred') return '图片已保存，尚未自动分类，可直接人工审核。'
  if (item.ai_tags_source === 'mock' || item.tags_source === 'mock') return '当前为模拟分类，待人工确认。'
  return item.ai_tags ? '分类为 AI 建议，待人工确认。' : '当前分类尚待人工审核确认。'
})
async function editCategory() {
  editingCategory.value = !editingCategory.value
  if (editingCategory.value) { await nextTick(); categoryInput.value?.focus() }
}
async function changeCategory(event: Event) {
  const id = props.item.id
  const value = (event.target as HTMLSelectElement).value
  savingCategory.value = true; categoryError.value = ''; categoryMessage.value = ''
  try {
    await assignSourceCategory([id], value)
    const result = await getRecord(id)
    if (id !== props.item.id) return
    emit('updated', result); editingCategory.value = false; categoryMessage.value = `资料大类已设置为${value}`
  } catch (e) { if (id === props.item.id) categoryError.value = e instanceof Error ? e.message : String(e) }
  finally { savingCategory.value = false }
}
function closeMore(event: Event) {
  if (!more.value?.open || more.value.contains(event.target as Node)) return
  more.value.open = false
}
function escapeMore(event: KeyboardEvent) {
  if (event.key !== 'Escape' || !more.value?.open || document.querySelector('dialog[open]')) return
  more.value.open = false; more.value.querySelector('summary')?.focus()
}
function coverUpdated(item: CatalogItem) { emit('updated', item); if (more.value) more.value.open = false }
onMounted(() => { document.addEventListener('pointerdown', closeMore); document.addEventListener('keydown', escapeMore) })
onUnmounted(() => { document.removeEventListener('pointerdown', closeMore); document.removeEventListener('keydown', escapeMore) })
</script>

<template>
  <section class="product-summary reference-summary" aria-label="款式概览">
    <div class="product-preview">
      <button class="product-image" type="button" aria-label="放大查看款式原图" @click="emit('image')"><img :src="item.image_url" :alt="title" :width="item.image_width || 600" :height="item.image_height || 600" /><span class="image-expand" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M14 4h6v6M20 4l-7 7M10 20H4v-6M4 20l7-7" /></svg></span></button>
      <div class="preview-caption"><button type="button" @click="emit('image')">查看原图</button><span v-if="item.image_width && item.image_height">{{ item.image_width }} × {{ item.image_height }} 像素</span><span v-if="lowResolution" class="image-quality-note">原图分辨率较低，放大可能模糊。</span></div>
      <p v-if="item.has_pending_image" class="preview-notice">有新图片待审核，通过后替换当前原图。</p>
    </div>
    <div class="reference-information">
      <div class="reference-status"><span class="review-badge" :class="item.review_status">{{ item.tags_source === 'deferred' && item.review_status === 'pending' ? '待分类' : reviewLabels[item.review_status] }}</span><span>{{ category || '品类待确认' }}</span></div>
      <h1>{{ title }}</h1><p v-if="description" class="reference-description">{{ description }}</p>
      <dl class="reference-attributes"><div v-for="row in attributes" :key="row.label"><dt>{{ row.label }}</dt><dd :class="{ 'attribute-missing': row.value === '未提供' }"><i v-if="row.label === '颜色' && row.value !== '未提供'" class="reference-color" :style="{ background: swatchColor(row.value) }" aria-hidden="true"></i>{{ row.value }}</dd></div></dl>
      <button class="reference-tags-link" type="button" @click="emit('tags')">查看完整标签 <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" aria-hidden="true"><path d="m4 6 4 4 4-4" /></svg></button>
      <div class="reference-actions"><RouterLink class="reference-primary" :to="{ name: 'search', query: { record: item.id } }"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><circle cx="10.5" cy="10.5" r="7.5" /><path d="m16 16 5 5" /></svg>找相似款</RouterLink><RouterLink class="reference-secondary" :to="{ path: '/review', query: { status: item.review_status, selected: item.id } }">{{ item.review_status === 'pending' ? '审核此款' : '查看审核结果' }}</RouterLink>
        <details ref="more" class="record-more"><summary aria-label="更多款式操作" title="更多操作">···</summary><div class="record-more-popup"><CoverEditor :item="item" @updated="coverUpdated" /><a :href="item.download_url || item.image_url" download @click="more && (more.open = false)">下载原图</a><a v-if="item.source_url" :href="item.source_url" download @click="more && (more.open = false)">下载来源资料</a></div></details>
      </div>
      <section class="reference-source" aria-labelledby="reference-source-heading"><h2 id="reference-source-heading">资料来源</h2><div class="reference-source-content"><div class="reference-source-file"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="M14 2H5v20h14V7l-5-5Z" /><path d="M14 2v6h5" /></svg><span>{{ item.source.source_file || '未记录来源文件' }}</span></div>
          <p class="reference-source-meta"><span>{{ sourceType(item.source) }}</span><span v-if="location">{{ location }}</span><span v-if="item.image_width && item.image_height">{{ item.image_width }} × {{ item.image_height }} 像素</span></p><p class="reference-source-time">导入时间 <time :datetime="importTime(item.source)">{{ formatTime(importTime(item.source)) }}</time></p>
          <div class="reference-source-category"><span :class="{ 'category-unset': missingCategory }">{{ missingCategory ? '资料大类未设置' : `资料大类：${item.source_category}` }}</span><button type="button" :disabled="savingCategory" :aria-expanded="editingCategory" aria-controls="reference-category-editor" @click="editCategory">{{ missingCategory ? '设置资料大类' : '更改' }}</button></div>
          <div v-if="editingCategory" id="reference-category-editor" class="reference-category-editor"><label for="reference-category-input">资料大类</label><select id="reference-category-input" ref="categoryInput" :value="item.source_category" :disabled="savingCategory" @change="changeCategory"><option v-for="choice in categoryChoices" :key="choice" :value="choice">{{ choice }}</option></select><button type="button" :disabled="savingCategory" @click="editingCategory = false">取消</button></div>
          <p v-if="categoryMessage" class="category-message" role="status">{{ categoryMessage }}</p><p v-if="categoryError" class="error" role="alert">{{ categoryError }}</p><p class="reference-review-note">{{ stateNote }}</p>
        </div></section>
    </div>
  </section>
</template>
