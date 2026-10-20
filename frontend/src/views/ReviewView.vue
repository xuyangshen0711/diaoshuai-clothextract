<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { jobs, isActive } from '../composables/importJobs'
import { onBeforeRouteLeave, onBeforeRouteUpdate, useRoute, useRouter } from 'vue-router'
import { getFilters, getReviews, getReviewHistory, reviewRecord, type CatalogItem,
  type FashionTags, type FilterMeta, type ReviewEvent, type ReviewStatus } from '../api'

const route = useRoute()
const router = useRouter()
const labels: Record<ReviewStatus, string> = { pending: '待审核', approved: '已通过', rejected: '已驳回' }
const actions: Record<string, string> = {
  record_created: '导入并生成初始分类', legacy_imported: '迁移历史数据',
  classification_proposed: '提交新的待审核分类', approve: '审核通过', reject: '驳回分类', resubmit: '重新提交审核',
}
const fields = ['品类', '风格', '季节', '版型', '颜色主色调', '领型', '袖型'] as const
const queryValue = (key: string) => typeof route.query[key] === 'string' ? route.query[key] as string : ''
const state = computed<ReviewStatus>(() => ['pending', 'approved', 'rejected'].includes(queryValue('status')) ? queryValue('status') as ReviewStatus : 'pending')
const page = computed(() => Math.max(1, Number.parseInt(queryValue('page')) || 1))
const batch = computed(() => queryValue('batch'))
const source = computed(() => queryValue('source'))
const category = computed(() => queryValue('category'))
const facets = ref<{ batches: { id: string; label: string }[]; files: string[] }>({ batches: [], files: [] })
const counts = reactive<Record<ReviewStatus, number>>({ pending: 0, approved: 0, rejected: 0 })
const meta = ref<FilterMeta>({})
const items = ref<CatalogItem[]>([])
const total = ref(0)
const selectedId = ref('')
const current = computed(() => items.value.find(item => item.id === selectedId.value))
const position = computed(() => (page.value - 1) * 30 + items.value.findIndex(item => item.id === selectedId.value) + 1)
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
const baseline = ref('')
const signature = () => JSON.stringify({ tags: form, note: note.value })
const editable = computed(() => current.value?.review_status === 'pending')
const dirty = computed(() => editable.value && baseline.value !== '' && signature() !== baseline.value)
const edited = computed(() => !current.value?.ai_tags || JSON.stringify(form) !== JSON.stringify(current.value.ai_tags))
let loadSequence = 0
let historySequence = 0
let chooseLast = false
let ready = false

function confirmLeave() {
  if (saving.value) return false
  return !dirty.value || window.confirm('分类修改尚未提交，是否放弃修改并离开当前图片？')
}
onBeforeRouteLeave(confirmLeave)
onBeforeRouteUpdate(confirmLeave)
function beforeUnload(event: BeforeUnloadEvent) {
  if (dirty.value) { event.preventDefault(); event.returnValue = '' }
}
function date(value: string | null) { return value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '' }
function display(value: unknown) { return Array.isArray(value) ? value.join('、') || '无' : String(value || '未提供') }
async function load(preferId = ''): Promise<void> {
  const sequence = ++loadSequence
  loading.value = true; error.value = ''; items.value = []; selectedId.value = ''
  try {
    const params: Record<string, string> = {}
    if (batch.value) params.batch = batch.value
    if (source.value) params.source_file = source.value
    if (category.value) params.category = category.value
    if (queryValue('selected')) params.selected_id = queryValue('selected')
    const data = await getReviews(state.value, page.value, params)
    if (sequence !== loadSequence) return
    items.value = data.items; total.value = data.count
    facets.value = data.facets; Object.assign(counts, data.counts)
    const desired = preferId || queryValue('selected')
    const match = items.value.find(item => item.id === desired)
    const fallback = chooseLast ? items.value[items.value.length - 1] : items.value[0]
    chooseLast = false
    selectedId.value = match?.id || fallback?.id || ''
    if (queryValue('selected') !== selectedId.value || page.value !== data.page) {
      await router.replace({ query: { ...route.query, page: String(data.page), selected: selectedId.value || undefined } })
    }
  } catch (e) { if (sequence === loadSequence) error.value = e instanceof Error ? e.message : String(e) }
  finally { if (sequence === loadSequence) loading.value = false }
}
function setParams(values: Record<string, string | undefined>) {
  return router.replace({ query: { ...route.query, page: '1', selected: undefined, ...values } })
}
function switchState(value: ReviewStatus) { message.value = ''; setParams({ status: value }) }
function selectItem(id: string) { return router.replace({ query: { ...route.query, selected: id } }) }
function changePage(delta: number) {
  chooseLast = delta < 0
  router.replace({ query: { ...route.query, page: String(page.value + delta), selected: undefined } })
}
async function move(delta: number, skip = false) {
  if (saving.value || loading.value || !current.value) return
  const index = items.value.findIndex(item => item.id === selectedId.value)
  const target = items.value[index + delta]
  if (target) await selectItem(target.id)
  else if (delta > 0 && page.value * 30 < total.value) changePage(1)
  else if (delta < 0 && page.value > 1) changePage(-1)
  else if (skip && items.value.length > 1) await selectItem(items.value[0]!.id)
  else if (skip) { message.value = '当前只有这一张待处理图片，已保留在待审核中'; return }
  await nextTick()
  if (skip && !dirty.value) message.value = '已暂时跳过，图片仍保留在待审核队列'
}
async function refreshQueue() {
  if (!confirmLeave()) return
  baseline.value = ''; await load(selectedId.value)
}
watch(() => jobs.value.filter(job => !isActive(job)).map(job => `${job.id}:${job.status}`).join(','), () => {
  if (ready && !dirty.value && !saving.value && !loading.value) load(selectedId.value)
})
watch([state, page, batch, source, category], () => load())
watch(() => route.query.selected, value => {
  if (typeof value === 'string' && items.value.some(item => item.id === value)) selectedId.value = value
})
watch(current, async item => {
  const sequence = ++historySequence
  baseline.value = ''; history.value = []; historyError.value = ''; note.value = ''
  if (!item) return
  Object.assign(form, JSON.parse(JSON.stringify(item.proposed_tags || item.tags)))
  for (const key of fields) if (!meta.value[key]?.includes(form[key])) form[key] = ''
  baseline.value = signature()
  try {
    const data = await getReviewHistory(item.id)
    if (sequence === historySequence) history.value = data.items
  } catch (e) { if (sequence === historySequence) historyError.value = e instanceof Error ? e.message : String(e) }
})
async function decide(action: 'approve' | 'reject' | 'resubmit') {
  const item = current.value
  if (!item || saving.value) return
  error.value = ''; message.value = ''
  if (!reviewer.value.trim()) { error.value = '请先填写审核人姓名'; document.getElementById('reviewer')?.focus(); return }
  if (action === 'reject' && !note.value.trim()) { error.value = '请填写驳回原因'; document.getElementById('review-note')?.focus(); return }
  if (action === 'approve' && fields.some(key => !form[key])) { error.value = '请补全分类标签后再通过'; return }
  const index = items.value.findIndex(candidate => candidate.id === item.id)
  const nextId = items.value[index + 1]?.id || items.value[index - 1]?.id || ''
  saving.value = true
  try {
    await reviewRecord(item, action, reviewer.value.trim(), note.value.trim(),
      action === 'approve' ? JSON.parse(JSON.stringify(form)) : undefined)
    localStorage.setItem('clothextract-reviewer', reviewer.value.trim())
    message.value = action === 'approve' ? '已通过，主页已标记为已审核' :
      action === 'reject' ? '已驳回，图片已从主页移除' : '已重新提交，请在待审核中确认分类'
    baseline.value = ''; saving.value = false
    await load(nextId)
  } catch (e) { error.value = e instanceof Error ? e.message : String(e) }
  finally { saving.value = false }
}
function onKey(event: KeyboardEvent) {
  const target = event.target as HTMLElement
  if (event.ctrlKey || event.metaKey || event.altKey || target.closest('input, textarea, select, [contenteditable="true"]')) return
  if (event.key === 'ArrowRight') { event.preventDefault(); move(1) }
  if (event.key === 'ArrowLeft') { event.preventDefault(); move(-1) }
  if (event.key.toLowerCase() === 's' && editable.value) { event.preventDefault(); move(1, true) }
  if (event.key.toLowerCase() === 'a' && editable.value) { event.preventDefault(); decide('approve') }
}
onMounted(async () => {
  window.addEventListener('keydown', onKey); window.addEventListener('beforeunload', beforeUnload)
  try { meta.value = await getFilters(); await load(); ready = true }
  catch (e) { error.value = e instanceof Error ? e.message : String(e) }
})
onUnmounted(() => { window.removeEventListener('keydown', onKey); window.removeEventListener('beforeunload', beforeUnload) })
</script>

<template>
  <main class="review-page" :aria-busy="saving || loading">
    <div class="page-head review-heading">
      <div>
        <h2>人工审核</h2>
        <p class="meta">确认或修正 AI 分类；未审核图片已在主页标注。</p>
      </div>
      <button class="btn" :disabled="loading || saving" @click="refreshQueue">刷新队列</button>
    </div>
    <div class="review-tabs" role="group" aria-label="审核状态">
      <button v-for="(label, key) in labels" :key="key" type="button"
        :class="{ active: state === key }" :aria-pressed="state === key"
        :disabled="saving" @click="switchState(key)">
        {{ label }} <span>{{ counts[key] }}</span>
      </button>
    </div>
    <div class="review-filterbar">
      <div class="field"><label for="review-batch">导入批次</label><select id="review-batch" :value="batch" :disabled="saving" @change="setParams({ batch: ($event.target as HTMLSelectElement).value || undefined })"><option value="">全部批次</option><option v-for="item in facets.batches" :key="item.id" :value="item.id">{{ item.label }}</option></select></div>
      <div class="field"><label for="review-source">来源文件</label><select id="review-source" :value="source" :disabled="saving" @change="setParams({ source: ($event.target as HTMLSelectElement).value || undefined })"><option value="">全部文件</option><option v-for="item in facets.files" :key="item" :value="item">{{ item }}</option></select></div>
      <div class="field"><label for="review-category">服装品类</label><select id="review-category" :value="category" :disabled="saving" @change="setParams({ category: ($event.target as HTMLSelectElement).value || undefined })"><option value="">全部品类</option><option v-for="item in meta['品类'] || []" :key="item" :value="item">{{ item }}</option></select></div>
      <button v-if="batch || source || category" class="btn" :disabled="saving" @click="setParams({ batch: undefined, source: undefined, category: undefined })">清除筛选</button>
    </div>
    <p v-if="message" class="review-notice" role="status">{{ message }}</p>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p v-if="loading" class="empty" role="status">正在加载审核队列…</p>
    <div v-else-if="!items.length && !error" class="review-empty">
      <h3>{{ state === 'pending' ? '待审核图片已处理完' : `暂无${labels[state]}的图片` }}</h3>
      <p>新导入的图片和重新分类的图片会进入待审核队列。</p>
      <RouterLink to="/import" class="btn">导入资料</RouterLink>
      <RouterLink to="/" class="btn">查看主页图库</RouterLink>
    </div>
    <div v-else-if="items.length" class="review-workspace">
      <aside class="review-queue" aria-label="图片审核队列">
        <p class="meta">共 {{ total }} 张，第 {{ page }} 页</p>
        <button v-for="item in items" :key="item.id" class="review-queue-item"
          :class="{ selected: selectedId === item.id }" :aria-pressed="selectedId === item.id"
          :disabled="saving" @click="selectItem(item.id)">
          <img :src="item.image_url" alt="" loading="lazy" />
          <span><strong>{{ (item.proposed_tags || item.tags).品类 }}</strong>
            <small>{{ item.source.source_file }}</small><small>{{ item.source.location }}</small></span>
        </button>
        <div class="toolbar review-pagination">
          <button class="btn" :disabled="page === 1 || saving" @click="changePage(-1)">上一页</button>
          <button class="btn" :disabled="page * 30 >= total || saving" @click="changePage(1)">下一页</button>
        </div>
      </aside>
      <div v-if="current" class="review-content">
        <div class="review-navigation"><span>第 {{ position }} / {{ total }} 张<span v-if="dirty" class="unsaved-mark"> · 有未提交修改</span></span><div class="toolbar">
          <button class="btn" :disabled="saving || position <= 1" @click="move(-1)">上一张</button><button class="btn" :disabled="saving || position >= total" @click="move(1)">下一张</button>
          <RouterLink class="btn" :to="{ name: 'record', params: { id: current.id }, query: { from: route.fullPath } }">款式详情</RouterLink>
        </div></div>
        <p class="meta review-shortcuts">← / → 切图 · A 通过 · S 暂时跳过（在输入框外使用）</p>
        <div class="review-detail" :key="current.id">
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
              <button class="btn" type="button" :disabled="saving" @click="move(1, true)">暂时跳过</button>
              <button class="btn review-reject" type="button" :disabled="saving" @click="decide('reject')">驳回</button>
            </div>
            <template v-else>
              <p class="meta">{{ current.review_status === 'approved' ? '已通过的分类如需修改，可重新提交审核。重新审核期间将在主页标记为未审核。' : '修正后可重新提交审核，并在主页显示为未审核。' }}</p>
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
    </div>
  </main>
</template>
