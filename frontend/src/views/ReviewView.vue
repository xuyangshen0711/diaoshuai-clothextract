<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { getFilters, getReviews, getReviewHistory, reviewRecord, type CatalogItem,
  type FashionTags, type FilterMeta, type ReviewEvent, type ReviewStatus } from '../api'

const labels: Record<ReviewStatus, string> = { pending: '待审核', approved: '已通过', rejected: '已驳回' }
const actions: Record<string, string> = {
  record_created: '导入并生成初始分类', legacy_imported: '迁移历史数据',
  classification_proposed: '提交新的待审核分类', approve: '审核通过', reject: '驳回分类', resubmit: '重新提交审核',
}
const fields = ['品类', '风格', '季节', '版型', '颜色主色调', '领型', '袖型'] as const
const state = ref<ReviewStatus>('pending')
const counts = reactive<Record<ReviewStatus, number>>({ pending: 0, approved: 0, rejected: 0 })
const meta = ref<FilterMeta>({})
const items = ref<CatalogItem[]>([])
const total = ref(0)
const page = ref(1)
const selectedId = ref('')
const current = computed(() => items.value.find(item => item.id === selectedId.value))
const form = reactive<FashionTags>({ 品类: '', 风格: '', 季节: '', 版型: '', 颜色主色调: '',
  领型: '', 袖型: '', 细节元素: [], 一句话描述: '' })
const reviewer = ref(localStorage.getItem('clothextract-reviewer') || '')
const note = ref('')
const history = ref<ReviewEvent[]>([])
const loading = ref(false)
const saving = ref(false)
const error = ref('')
const historyError = ref('')
const message = ref('')
let loadSequence = 0
let historySequence = 0
const editable = computed(() => current.value?.review_status === 'pending')
const edited = computed(() => {
  const base = current.value?.ai_tags
  return !base || JSON.stringify(form) !== JSON.stringify(base)
})

function date(value: string | null) {
  return value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : ''
}
function display(value: unknown) {
  return Array.isArray(value) ? value.join('、') || '无' : String(value || '未提供')
}
async function load(): Promise<void> {
  const sequence = ++loadSequence
  loading.value = true
  error.value = ''
  // Clear stale forms before fetching a different queue or page.
  items.value = []
  selectedId.value = ''
  try {
    const data = await getReviews(state.value, page.value)
    if (sequence !== loadSequence) return
    if (page.value > 1 && !data.items.length) {
      page.value--
      return load()
    }
    items.value = data.items
    Object.assign(counts, data.counts)
    total.value = data.count
    selectedId.value = items.value[0]?.id || ''
  } catch (e) {
    if (sequence === loadSequence) error.value = e instanceof Error ? e.message : String(e)
  } finally {
    if (sequence === loadSequence) loading.value = false
  }
}
function switchState(value: ReviewStatus) {
  state.value = value
  page.value = 1
  message.value = ''
  load()
}
watch(current, async item => {
  const sequence = ++historySequence
  history.value = []
  historyError.value = ''
  note.value = ''
  if (!item) return
  Object.assign(form, JSON.parse(JSON.stringify(item.proposed_tags || item.tags)))
  for (const key of fields) {
    if (!meta.value[key]?.includes(form[key])) form[key] = ''
  }
  try {
    const data = await getReviewHistory(item.id)
    if (sequence === historySequence) history.value = data.items
  } catch (e) {
    if (sequence === historySequence) historyError.value = e instanceof Error ? e.message : String(e)
  }
})

async function decide(action: 'approve' | 'reject' | 'resubmit') {
  const item = current.value
  if (!item || saving.value) return
  error.value = ''
  message.value = ''
  if (!reviewer.value.trim()) { error.value = '请先填写审核人姓名'; return }
  if (action === 'reject' && !note.value.trim()) { error.value = '请填写驳回原因'; return }
  if (action === 'approve' && fields.some(key => !form[key])) {
    error.value = '请补全分类标签后再通过'; return
  }
  saving.value = true
  try {
    await reviewRecord(item, action, reviewer.value.trim(), note.value.trim(),
      action === 'approve' ? structuredClone({ ...form, 细节元素: [...form.细节元素] }) : undefined)
    localStorage.setItem('clothextract-reviewer', reviewer.value.trim())
    message.value = action === 'approve' ? '已通过，图片已进入正式图库' :
      action === 'reject' ? '已驳回，图片不会出现在正式图库' : '已重新提交，请在待审核中确认分类'
    await load()
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    saving.value = false
  }
}

onMounted(async () => {
  try { meta.value = await getFilters(); await load() }
  catch (e) { error.value = e instanceof Error ? e.message : String(e) }
})
</script>

<template>
  <main class="review-page" :aria-busy="saving || loading">
    <div class="page-head review-heading">
      <div>
        <h2>人工审核</h2>
        <p class="meta">逐张确认 AI 分类，审核通过后进入正式图库。</p>
      </div>
      <button class="btn" :disabled="loading || saving" @click="load">刷新队列</button>
    </div>
    <div class="review-tabs" role="group" aria-label="审核状态">
      <button v-for="(label, key) in labels" :key="key" type="button"
        :class="{ active: state === key }" :aria-pressed="state === key"
        :disabled="saving" @click="switchState(key)">
        {{ label }} <span>{{ counts[key] }}</span>
      </button>
    </div>
    <p v-if="message" class="review-notice" role="status">{{ message }}</p>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p v-if="loading" class="empty" role="status">正在加载审核队列…</p>
    <div v-else-if="!items.length && !error" class="review-empty">
      <h3>{{ state === 'pending' ? '待审核图片已处理完' : `暂无${labels[state]}的图片` }}</h3>
      <p>新导入的图片和重新分类的图片会进入待审核队列。</p>
      <RouterLink to="/import" class="btn">导入资料</RouterLink>
      <RouterLink to="/" class="btn">查看正式图库</RouterLink>
    </div>
    <div v-else-if="items.length" class="review-workspace">
      <aside class="review-queue" aria-label="图片审核队列">
        <p class="meta">共 {{ total }} 张，第 {{ page }} 页</p>
        <button v-for="item in items" :key="item.id" class="review-queue-item"
          :class="{ selected: selectedId === item.id }" :aria-pressed="selectedId === item.id"
          :disabled="saving" @click="selectedId = item.id">
          <img :src="item.image_url" alt="" loading="lazy" />
          <span><strong>{{ (item.proposed_tags || item.tags).品类 }}</strong>
            <small>{{ item.source.source_file }}</small><small>{{ item.source.location }}</small></span>
        </button>
        <div class="toolbar review-pagination">
          <button class="btn" :disabled="page === 1 || saving" @click="page--; load()">上一页</button>
          <button class="btn" :disabled="page * 30 >= total || saving" @click="page++; load()">下一页</button>
        </div>
      </aside>
      <div v-if="current" class="review-detail" :key="current.id">
        <div class="review-image-panel">
          <a :href="current.image_url" target="_blank" rel="noopener" aria-label="打开完整原图">
            <img class="review-original" :src="current.image_url" :alt="current.tags.一句话描述 || '待审核服装原图'" />
          </a>
          <p class="review-source">{{ current.source.source_file }}<br />{{ current.source.location }} · 点击图片查看原图</p>
          <p v-if="current.reviewed_at" class="meta">
            上次审核：{{ current.reviewer }}<br />{{ date(current.reviewed_at) }}
          </p>
          <p v-if="current.review_note" class="review-note">上次审核意见：{{ current.review_note }}</p>
        </div>
        <form class="review-form" @submit.prevent="decide('approve')">
          <div class="review-comparison-head"><strong>分类标签</strong><span>AI 建议{{ current.ai_tags_source === 'mock' ? '（模拟）' : '' }}</span><span>{{ editable ? '人工确认' : '审核结果' }}</span></div>
          <fieldset :disabled="!editable || saving" class="review-fields">
            <div v-for="key in fields" :key="key" class="review-comparison-row"
              :class="{ changed: current.ai_tags && form[key] !== current.ai_tags[key] }">
              <label :for="`review-${key}`">{{ key }}</label>
              <span>{{ display(current.ai_tags?.[key]) }}</span>
              <select :id="`review-${key}`" v-model="form[key]">
                <option disabled value="">请选择</option>
                <option v-for="opt in meta[key] || []" :key="opt" :value="opt">{{ opt }}</option>
              </select>
            </div>
            <div class="review-comparison-row review-details-row">
              <span id="detail-label">细节元素</span><span>{{ display(current.ai_tags?.细节元素) }}</span>
              <div class="review-checks" role="group" aria-labelledby="detail-label">
                <label v-for="opt in meta['细节元素'] || []" :key="opt">
                  <input v-model="form.细节元素" type="checkbox" :value="opt" />{{ opt }}
                </label>
              </div>
            </div>
            <div class="review-description">
              <label for="review-description">一句话描述</label>
              <p class="meta">AI：{{ current.ai_tags?.一句话描述 || '未提供' }}</p>
              <textarea id="review-description" v-model="form.一句话描述" rows="2" />
            </div>
          </fieldset>
          <div class="review-decision">
            <div class="field"><label for="reviewer">审核人（手动填写）</label>
              <input id="reviewer" v-model="reviewer" maxlength="100" placeholder="填写你的姓名" :disabled="saving" autocomplete="name" /></div>
            <div v-if="editable" class="field"><label for="review-note">审核意见 / 驳回原因（驳回必填）</label>
              <textarea id="review-note" v-model="note" rows="2" maxlength="2000" :disabled="saving" placeholder="例如：图片不是服装款式图" /></div>
            <div v-if="editable" class="toolbar">
              <button class="btn primary" type="submit" :disabled="saving">{{ saving ? '提交中…' : edited ? '修改后通过' : '直接通过' }}</button>
              <button class="btn review-reject" type="button" :disabled="saving" @click="decide('reject')">驳回</button>
            </div>
            <template v-else>
              <p class="meta">{{ current.review_status === 'approved' ? '已通过的分类如需修改，可重新提交审核。重新审核期间将暂时移出正式图库。' : '修正后可重新提交审核，通过后进入正式图库。' }}</p>
              <button class="btn" type="button" :disabled="saving" @click="decide('resubmit')">重新提交审核</button>
            </template>
          </div>
          <section class="review-history" aria-label="分类与审核历史">
            <h3>分类与审核历史</h3>
            <p v-if="historyError" class="error" role="alert">{{ historyError }}</p>
            <details v-for="event in history" :key="event.id">
              <summary>{{ actions[event.action] || event.action }}<span>{{ event.actor || '系统' }} · {{ date(event.created_at) }}</span></summary>
              <p v-if="event.note">{{ event.note }}</p>
              <p v-if="event.after.ai_tags" class="meta">AI 分类：{{ Object.entries(event.after.ai_tags).map(([key, value]) => `${key}：${display(value)}`).join('；') }}</p>
              <p class="meta">{{ event.action === 'approve' ? '最终分类' : '当次分类' }}：{{ Object.entries(event.after.proposed_tags || event.after.tags).map(([key, value]) => `${key}：${display(value)}`).join('；') }}</p>
              <p v-if="event.before" class="meta">此前分类：{{ Object.entries(event.before.proposed_tags || event.before.tags).map(([key, value]) => `${key}：${display(value)}`).join('；') }}</p>
            </details>
          </section>
        </form>
      </div>
    </div>
  </main>
</template>
