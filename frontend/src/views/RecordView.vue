<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { assignSourceCategory, getLibraryMeta, getRecord, type CatalogItem, type LibraryMeta } from '../api'
import DesignNotesList from '../components/DesignNotesList.vue'
import RecordHistory from '../components/RecordHistory.vue'
import { formatTime, importTime, sourceType, visibleDescription } from '../utils/source'
const route = useRoute()
const item = ref<CatalogItem | null>(null)
const loading = ref(false)
const error = ref('')
const categoryMessage = ref('')
const libraryMeta = ref<LibraryMeta>({ source_categories: ['本厂产品', '网图趋势', '品牌资料包', '市调拍照'], unclassified: '未分类', factory: {} })
const labels = { pending: '待审核', approved: '已通过', rejected: '已驳回' }
const back = computed(() => {
  const path = typeof route.query.from === 'string' ? route.query.from : '/'
  return path.startsWith('/') && !path.startsWith('//') && !path.startsWith('/records/') ? path : '/'
})
const tags = computed(() => item.value?.display_tags)
const ownFactory = computed(() => item.value?.source_category === '本厂产品')
const categoryChoices = computed(() => [...libraryMeta.value.source_categories, libraryMeta.value.unclassified])
const display = (value: unknown) => Array.isArray(value) ? value.join('、') || '无' : String(value || '未提供')
const pieces = (value: number) => `${value.toLocaleString('zh-CN')} 件`
const yuan = (value: number) => `${value.toLocaleString('zh-CN')} 元`
const colorText = (colors: { name: string; family: string }[]) => colors.map(color => `${color.name}（${color.family}）`).join('、') || '无'
const sizeText = (sizes: { label: string }[]) => sizes.map(size => size.label).join('、') || '无'
let latest = 0
watch(() => route.params.id, async id => {
  const seq = ++latest
  loading.value = true; error.value = ''; item.value = null; categoryMessage.value = ''
  try {
    const [result, meta] = await Promise.all([getRecord(String(id)), getLibraryMeta()])
    if (seq !== latest) return
    item.value = result
    libraryMeta.value = meta
  } catch (e) { if (seq === latest) error.value = e instanceof Error ? e.message : String(e) }
  finally { if (seq === latest) loading.value = false }
}, { immediate: true })

async function changeCategory(event: Event) {
  if (!item.value) return
  const next = (event.target as HTMLSelectElement).value
  categoryMessage.value = ''
  error.value = ''
  try {
    await assignSourceCategory([item.value.id], next)
    item.value = await getRecord(item.value.id)
    categoryMessage.value = `资料大类已改为${next}`
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
    ;(event.target as HTMLSelectElement).value = item.value.source_category
  }
}
</script>
<template>
  <main class="record-page">
    <div class="page-head"><RouterLink class="btn" :to="back">返回列表</RouterLink><span class="meta">款式详情</span></div>
    <p v-if="loading" class="empty" role="status">正在读取款式…</p><p v-if="error" class="error" role="alert">{{ error }} <RouterLink to="/">返回图库</RouterLink></p>
    <div v-if="item && tags" class="record-layout">
      <section class="record-image-panel" aria-label="款式原图"><a :href="item.image_url" target="_blank" rel="noopener"><img :src="item.image_url" :alt="tags.一句话描述 || item.library_category || tags.品类" width="600" height="800" /></a>
        <p class="meta">点击图片查看完整原图，保留原始比例。<span v-if="item.image_width && item.image_height"> {{ item.image_width }} × {{ item.image_height }} 像素</span></p>
        <p v-if="item.image_width && item.image_height && Math.max(item.image_width, item.image_height) < 600" class="meta">来源图片分辨率较低，放大可能模糊。建议上传更高清的原始资料。</p>
        <p v-if="item.has_pending_image" class="meta">有一张新图片待审核。通过后才会替换当前原图。</p>
        <div class="toolbar"><a class="btn" :href="item.download_url || item.image_url" download>下载原图</a><RouterLink class="btn primary" :to="{ name: 'search', query: { record: item.id } }">找相似款</RouterLink></div>
        <div v-if="item.image_history?.length" class="image-history">
          <h3>历史原图</h3>
          <a v-for="(old, index) in item.image_history" :key="index" :href="old.url" target="_blank" rel="noopener">
            <img :src="old.url" :alt="old.note || '替换前保留的原图'" width="80" height="106" />
          </a>
        </div>
      </section>
      <section class="record-information" aria-label="款式信息">
        <div class="record-title"><h2>{{ item.library_category || tags.品类 }}<small v-if="item.style_code" class="record-style-no">款号 {{ item.style_code }}</small></h2><span class="task-state" :class="item.review_status">{{ labels[item.review_status] }}</span></div>
        <p class="record-description">{{ visibleDescription(tags.一句话描述, item.review_status) || '暂无款式描述' }}</p>
        <p v-if="item.review_status === 'pending'" class="meta">当前分类已在主页展示，尚待人工审核确认。搜索时可选择是否包含未审核图片。</p>
        <p v-else-if="item.review_status === 'rejected'" class="meta">此图片已驳回，不在主页或搜索结果中展示。</p>
        <dl class="record-source">
          <div><dt>资料大类</dt><dd>
            <select :value="item.source_category" aria-label="资料大类" @change="changeCategory">
              <option v-for="choice in categoryChoices" :key="choice" :value="choice">{{ choice }}</option>
            </select>
            <span v-if="categoryMessage" class="meta">{{ categoryMessage }}</span>
          </dd></div>
          <div><dt>资料类型</dt><dd>{{ sourceType(item.source) }}</dd></div>
          <div><dt>导入时间</dt><dd><time :datetime="importTime(item.source)">{{ formatTime(importTime(item.source)) }}</time></dd></div>
        </dl>
        <dl class="record-source"><div><dt>来源资料</dt><dd>{{ item.source.source_file }}</dd></div><div><dt>所在位置</dt><dd>{{ item.source.location }}</dd></div><div v-if="item.reviewer"><dt>上次审核</dt><dd>{{ item.reviewer }} · {{ item.reviewed_at ? new Date(item.reviewed_at).toLocaleString('zh-CN', { hour12: false }) : '' }}</dd></div><div v-if="item.review_note"><dt>审核意见</dt><dd>{{ item.review_note }}</dd></div></dl>
        <div class="toolbar"><a v-if="item.source_url" class="btn" :href="item.source_url" download>下载来源资料</a><RouterLink class="btn" :to="{ path: '/review', query: { status: item.review_status, selected: item.id } }">{{ item.review_status === 'pending' ? '前往审核此款' : '查看审核结果' }}</RouterLink></div>

        <section v-if="ownFactory && item.sales" class="factory-sales" aria-label="销量">
          <h3>销量</h3>
          <p class="factory-totals"><strong>{{ pieces(item.sales.total_quantity) }}</strong><strong>{{ yuan(item.sales.total_amount) }}</strong></p>
          <div class="factory-breakdowns">
            <table>
              <caption>按颜色</caption>
              <thead><tr><th scope="col">颜色</th><th scope="col">色系</th><th scope="col">数量</th><th scope="col">金额</th></tr></thead>
              <tbody><tr v-for="row in item.sales.by_color" :key="row.name"><td>{{ row.name }}</td><td>{{ row.family }}</td><td>{{ pieces(row.quantity) }}</td><td>{{ yuan(row.amount) }}</td></tr></tbody>
            </table>
            <table>
              <caption>按尺码</caption>
              <thead><tr><th scope="col">尺码</th><th scope="col">数量</th><th scope="col">金额</th></tr></thead>
              <tbody><tr v-for="row in item.sales.by_size" :key="row.name"><td>{{ row.name }}</td><td>{{ pieces(row.quantity) }}</td><td>{{ yuan(row.amount) }}</td></tr></tbody>
            </table>
            <table>
              <caption>按客户版本</caption>
              <thead><tr><th scope="col">客户</th><th scope="col">大货款号</th><th scope="col">季节</th><th scope="col">数量</th><th scope="col">金额</th></tr></thead>
              <tbody><tr v-for="row in item.sales.by_customer" :key="row.variant_code"><td>{{ row.customer }}</td><td>{{ row.variant_code }}</td><td>{{ row.season || '—' }}</td><td>{{ pieces(row.quantity) }}</td><td>{{ yuan(row.amount) }}</td></tr></tbody>
            </table>
          </div>
        </section>
        <p v-else-if="ownFactory" class="meta">这张本厂产品图还没有销量数据。</p>

        <section v-if="ownFactory" class="factory-variants" aria-label="客户版本">
          <h3>客户版本（{{ item.variants?.length || 0 }}）</h3>
          <p v-if="!item.variants?.length" class="meta">款式档案里还没有这个款号。</p>
          <div v-else class="record-tag-table">
            <table>
              <caption>款式档案</caption>
              <thead><tr><th scope="col">客户</th><th scope="col">大货款号</th><th scope="col">季节</th><th scope="col">一级品类</th><th scope="col">颜色</th><th scope="col">尺码</th><th scope="col">针型</th><th scope="col">系列</th><th scope="col">原料名称</th><th scope="col">销量</th></tr></thead>
              <tbody>
                <tr v-for="variant in item.variants" :key="variant.variant_code">
                  <td>{{ variant.customer }}</td>
                  <td>{{ variant.variant_code }}</td>
                  <td>{{ variant.season || '—' }}</td>
                  <td>{{ variant.category || '—' }}</td>
                  <td>{{ colorText(variant.colors) }}</td>
                  <td>{{ sizeText(variant.sizes) }}</td>
                  <td>{{ variant.needle || '—' }}</td>
                  <td>{{ variant.series || '—' }}</td>
                  <td>{{ variant.material || '—' }}</td>
                  <td>{{ pieces(variant.quantity) }} / {{ yuan(variant.amount) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <p v-if="item.library_category && item.library_category !== tags.品类" class="meta">图库品类使用档案一级品类「{{ item.library_category }}」。下方 AI 标签仅作参考。</p>
        </section>

        <div class="record-tag-table"><table><caption>分类标签对照</caption><thead><tr><th scope="col">标签</th><th scope="col">AI 建议{{ item.ai_tags_source === 'mock' ? '（模拟）' : '' }}</th><th scope="col">{{ item.review_status === 'approved' ? '最终分类' : '待审核分类' }}</th></tr></thead><tbody>
          <tr v-for="(value, key) in tags" :key="key" :class="{ changed: item.ai_tags && display(item.ai_tags[key]) !== display(value) }"><th scope="row">{{ key }}</th><td>{{ key === '一句话描述' ? (visibleDescription(display(item.ai_tags?.[key]), item.review_status) || '未提供') : display(item.ai_tags?.[key]) }}</td><td>{{ key === '一句话描述' ? (visibleDescription(display(value), item.review_status) || '未提供') : display(value) }}</td></tr>
        </tbody></table></div>
        <DesignNotesList :notes="item.notes" />
        <RecordHistory :id="item.id" />
      </section>
    </div>
  </main>
</template>
