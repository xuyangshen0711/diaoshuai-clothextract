<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { assignSourceCategory, getLibraryMeta, getRecord, NOTE_FIELDS, type CatalogItem, type LibraryMeta } from '../api'
import DesignNotesList from '../components/DesignNotesList.vue'
import RecordHistory from '../components/RecordHistory.vue'
import { formatTime, importTime, sourceType, swatchColor, swatchLight, visibleDescription } from '../utils/source'
const route = useRoute()
const item = ref<CatalogItem | null>(null)
const loading = ref(false)
const error = ref('')
const categoryMessage = ref('')
const libraryMeta = ref<LibraryMeta>({ source_categories: ['本厂产品', '网图趋势', '品牌资料包', '市调拍照'], unclassified: '未分类', factory: {} })
const labels = { pending: '待审核', approved: '已审核', rejected: '已驳回' }
const back = computed(() => {
  const path = typeof route.query.from === 'string' ? route.query.from : '/'
  return path.startsWith('/') && !path.startsWith('//') && !path.startsWith('/records/') ? path : '/'
})
const tags = computed(() => item.value?.display_tags)
const ownFactory = computed(() => item.value?.source_category === '本厂产品')
const hasDesignNotes = computed(() => NOTE_FIELDS.some(key => key !== '款号' && Boolean(item.value?.notes?.[key]?.trim())))
const categoryChoices = computed(() => [...libraryMeta.value.source_categories, libraryMeta.value.unclassified])
const selectedVariantCode = ref('')
const selectedVariant = computed(() => item.value?.variants?.find(variant => variant.variant_code === selectedVariantCode.value) || null)
const hasVariantSalesBreakdown = computed(() => Boolean(selectedVariant.value?.sales_by_color && selectedVariant.value.sales_by_size))
const selectedSalesByColor = computed(() => selectedVariant.value?.sales_by_color || item.value?.sales?.by_color || [])
const selectedSalesBySize = computed(() => selectedVariant.value?.sales_by_size || item.value?.sales?.by_size || [])
const selectedSalesQuantity = computed(() => hasVariantSalesBreakdown.value ? selectedVariant.value?.quantity || 0 : item.value?.sales?.total_quantity || 0)
const selectedSalesScope = computed(() => hasVariantSalesBreakdown.value ? '所选版本' : '全款号汇总')
const galleryImages = computed(() => {
  if (!item.value) return []
  const images: { url: string; label: string }[] = [{ url: item.value.image_url, label: '当前原图' }]
  if (item.value.pending_image_url) images.push({ url: item.value.pending_image_url, label: '待审核图片' })
  for (const [index, old] of (item.value.image_history || []).entries()) {
    images.push({ url: old.url, label: old.note || `历史原图 ${index + 1}` })
  }
  return images.filter((image, index, all) => all.findIndex(candidate => candidate.url === image.url) === index)
})
const selectedImageUrl = ref('')
const display = (value: unknown) => Array.isArray(value) ? value.join('、') || '无' : String(value || '未提供')
const pieces = (value: number) => `${value.toLocaleString('zh-CN')} 件`
const unique = (values: (string | undefined)[]) => {
  const seen: string[] = []
  for (const value of values) if (value && !seen.includes(value)) seen.push(value)
  return seen
}
const seasons = computed(() => unique((item.value?.variants || []).map(row => row.season)))
function barWidth(quantity: number, rows: { quantity: number }[]) {
  const max = Math.max(...rows.map(row => row.quantity), 0)
  if (!max) return '0%'
  return `${Math.round(quantity / max * 100)}%`
}
let latest = 0
watch(() => route.params.id, async id => {
  const seq = ++latest
  loading.value = true; error.value = ''; item.value = null; categoryMessage.value = ''
  try {
    const [result, meta] = await Promise.all([getRecord(String(id)), getLibraryMeta()])
    if (seq !== latest) return
    item.value = result
    libraryMeta.value = meta
    selectedImageUrl.value = result.image_url
    const requestedVariant = typeof route.query.variant === 'string'
      ? result.variants?.find(variant => variant.variant_code === route.query.variant)?.variant_code
      : ''
    selectedVariantCode.value = requestedVariant
      || result.variants?.find(variant => variant.customer === '本厂自有版本')?.variant_code
      || result.variants?.[0]?.variant_code
      || ''
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
  <main class="record-page" :class="{ 'factory-record-page': ownFactory }" v-if="item && tags">
    <p v-if="error" class="error" role="alert">{{ error }}</p>

    <template v-if="ownFactory">
      <div class="factory-record-topline">
        <RouterLink class="factory-record-back" :to="back">← 返回列表</RouterLink>
        <div class="factory-record-identity">
          <strong>{{ item.style_code }}</strong>
          <span v-if="item.library_category" class="factory-category-pill">{{ item.library_category }}</span>
          <span v-for="season in seasons" :key="season" class="factory-season-pill">{{ season }}</span>
        </div>
        <div v-if="item.sales" class="factory-topline-sales" aria-label="累计销售">
          <span>累计销售</span>
          <strong>{{ item.sales.total_quantity.toLocaleString('zh-CN') }}</strong><span>件</span>
          <span class="sales-dot" aria-hidden="true">·</span>
          <strong>{{ item.sales.total_amount.toLocaleString('zh-CN') }}</strong><span>元</span>
        </div>
        <span class="task-state" :class="item.review_status">{{ labels[item.review_status] }}</span>
        <RouterLink class="btn primary factory-review-action" :to="{ path: '/review', query: { status: item.review_status, selected: item.id } }">
          {{ item.review_status === 'pending' ? '审核' : '查看审核' }}
        </RouterLink>
      </div>

      <div class="factory-record-main">
        <section class="factory-product-visual" aria-label="款式原图">
          <a class="factory-main-image" :href="selectedImageUrl || item.image_url" target="_blank" rel="noopener">
            <img :src="selectedImageUrl || item.image_url" :alt="item.style_code || item.library_category || tags.品类" width="600" height="800" />
          </a>
          <div v-if="galleryImages.length > 1" class="factory-image-thumbnails" aria-label="款式图片">
            <button v-for="image in galleryImages" :key="image.url" type="button" :aria-label="image.label" :aria-pressed="selectedImageUrl === image.url" @click="selectedImageUrl = image.url">
              <img :src="image.url" :alt="image.label" width="64" height="84" />
            </button>
          </div>
          <p v-if="item.has_pending_image" class="meta">有一张替换图片待审核；通过后才会替换当前原图。</p>
          <p v-if="item.image_width && item.image_height && Math.max(item.image_width, item.image_height) < 600" class="meta">来源图片分辨率较低，放大可能模糊。建议上传更高清的原始资料。</p>
        </section>

        <section v-if="selectedVariant" class="factory-selected-version" aria-label="所选客户版本">
            <div class="factory-selected-version-heading">
              <h2>{{ selectedVariant.customer }}</h2>
              <strong>{{ selectedVariant.quantity.toLocaleString('zh-CN') }} 件 <span>·</span> {{ selectedVariant.amount.toLocaleString('zh-CN') }} 元</strong>
            </div>
            <dl class="factory-selected-version-facts">
              <div><dt>客户</dt><dd>{{ selectedVariant.customer }}</dd></div>
              <div><dt>大货款号</dt><dd>{{ selectedVariant.variant_code }}</dd></div>
              <div><dt>季节</dt><dd>{{ selectedVariant.season || '—' }}</dd></div>
              <div><dt>颜色</dt><dd>
                <span v-if="!selectedVariant.colors.length">—</span>
                <span v-for="color in selectedVariant.colors" :key="color.name" class="factory-version-color">
                  <span class="card-dot" :class="{ light: swatchLight(color.family) }" :style="{ background: swatchColor(color.family) }"></span>{{ color.name }}
                </span>
              </dd></div>
            </dl>
            <p class="factory-selected-materials">
              <span v-if="selectedVariant.needle">针型 · {{ selectedVariant.needle }}</span>
              <span v-if="selectedVariant.material">原料 · {{ selectedVariant.material }}</span>
              <span v-if="selectedVariant.sizes.length">尺码 · {{ selectedVariant.sizes.map(size => size.label).join('、') }}</span>
            </p>
            <div class="factory-selected-sales">
              <section :aria-label="`${selectedSalesScope}按颜色销售`">
                <div class="detail-block-head"><h3>按颜色销售 <small>{{ selectedSalesScope }}</small></h3><span class="meta">共 {{ pieces(selectedSalesQuantity) }}</span></div>
                <template v-if="selectedSalesByColor.length">
                  <div class="factory-sales-rows">
                    <div v-for="row in selectedSalesByColor" :key="row.name" class="bar-row">
                      <span class="card-dot" :class="{ light: swatchLight(row.family || row.name) }" :style="{ background: swatchColor(row.family || row.name) }"></span>
                      <span class="bar-name">{{ row.name }}</span>
                      <span class="bar-track"><span class="bar-fill" :style="{ width: barWidth(row.quantity, selectedSalesByColor), background: swatchColor(row.family || row.name) }"></span></span>
                      <span class="bar-num">{{ row.quantity.toLocaleString('zh-CN') }}</span>
                    </div>
                  </div>
                </template>
                <p v-else class="meta">{{ hasVariantSalesBreakdown ? '该版本暂无颜色销售明细。' : '该款号暂无颜色销售明细。' }}</p>
              </section>
              <section :aria-label="`${selectedSalesScope}按尺码销售`">
                <div class="detail-block-head"><h3>按尺码销售 <small>{{ selectedSalesScope }}</small></h3><span class="meta">共 {{ pieces(selectedSalesQuantity) }}</span></div>
                <template v-if="selectedSalesBySize.length">
                  <div class="factory-sales-rows">
                    <div v-for="row in selectedSalesBySize" :key="row.name" class="bar-row">
                      <span class="bar-name size-name">{{ row.name }}</span>
                      <span class="bar-track"><span class="bar-fill size-fill" :style="{ width: barWidth(row.quantity, selectedSalesBySize) }"></span></span>
                      <span class="bar-num">{{ row.quantity.toLocaleString('zh-CN') }}</span>
                    </div>
                  </div>
                </template>
                <p v-else class="meta">{{ hasVariantSalesBreakdown ? '该版本暂无尺码销售明细。' : '该款号暂无尺码销售明细。' }}</p>
              </section>
            </div>
          </section>
      </div>
      <section class="factory-sales-detail-section" aria-label="销售明细">
        <p v-if="item.sales_details === undefined" class="meta sales-detail-unavailable" role="status">销售明细暂不可用：当前服务尚未加载此功能。重启图库服务后刷新即可查看。</p>
        <p v-else-if="!item.sales_details.length" class="meta">该款号还没有已导入的销售明细。可从导入页上传对应销量表。</p>
        <RouterLink v-else class="factory-sales-detail-entry"
          :to="{ name: 'sales-details', params: { id: item.id }, query: { from: route.query.from } }">
          <span class="factory-sales-detail-entry-copy">
            <strong>销售明细</strong>
            <small>{{ item.sales_details.length }} 条汇总记录 · 款号、颜色、尺码、年度和周期相同的行已合并</small>
          </span>
          <span class="factory-sales-detail-entry-action">查看明细 <span aria-hidden="true">→</span></span>
        </RouterLink>
      </section>
      <section class="factory-version-table-section" aria-label="客户版本比较">
        <div class="factory-version-table-heading">
          <h2>客户版本明细</h2>
          <span>{{ item.variants?.length || 0 }} 个版本 · 点击行切换所选版本</span>
        </div>
        <p v-if="!item.variants?.length" class="meta">款式档案里还没有这个款号。</p>
        <div v-else class="factory-version-table-wrap">
          <table class="factory-version-table">
            <thead><tr><th scope="col">客户</th><th scope="col">大货款号</th><th scope="col">季节</th><th scope="col">颜色</th><th scope="col">件数</th><th scope="col">金额（元）</th></tr></thead>
            <tbody>
              <tr v-for="variant in item.variants" :key="variant.variant_code" :class="{ selected: selectedVariantCode === variant.variant_code }">
                <td><button type="button" class="factory-table-select" :aria-pressed="selectedVariantCode === variant.variant_code" @click="selectedVariantCode = variant.variant_code">{{ variant.customer }}</button></td>
                <td>{{ variant.variant_code }}</td>
                <td>{{ variant.season || '—' }}</td>
                <td>
                  <span v-if="!variant.colors.length">—</span>
                  <span v-for="color in variant.colors" :key="color.name" class="factory-version-color">
                    <span class="card-dot" :class="{ light: swatchLight(color.family) }" :style="{ background: swatchColor(color.family) }"></span>{{ color.name }}
                  </span>
                </td>
                <td class="numeric">{{ variant.quantity.toLocaleString('zh-CN') }}</td>
                <td class="numeric">{{ variant.amount.toLocaleString('zh-CN') }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
    </template>

    <template v-else>
      <div class="page-head"><RouterLink class="factory-record-back" :to="back">返回列表</RouterLink>
        <p class="detail-crumb">{{ item.source_category || '未分类' }}<template v-if="item.library_category"> / {{ item.library_category }}</template><template v-if="item.style_code"> / {{ item.style_code }}</template></p>
      </div>
      <div class="record-layout detail-hero">
        <section class="record-image-panel" aria-label="款式原图">
          <a :href="item.image_url" target="_blank" rel="noopener"><img :src="item.image_url" :alt="item.style_code || tags.一句话描述 || item.library_category || tags.品类" width="600" height="800" /></a>
          <p v-if="item.has_pending_image" class="meta">有一张新图片待审核。通过后才会替换当前原图。</p>
          <p v-if="item.image_width && item.image_height && Math.max(item.image_width, item.image_height) < 600" class="meta">来源图片分辨率较低，放大可能模糊。建议上传更高清的原始资料。</p>
        </section>
        <section class="record-information" aria-label="款式信息">
          <div class="detail-heading">
            <h2>{{ item.style_code || item.library_category || tags.品类 }}</h2>
            <span class="task-state" :class="item.review_status">{{ labels[item.review_status] }}</span>
          </div>
          <div class="detail-pills">
            <span v-if="item.library_category || tags.品类" class="detail-pill strong">{{ item.library_category || tags.品类 }}</span>
            <span v-for="season in [tags.季节].filter(Boolean)" :key="season" class="detail-pill">{{ season }}</span>
          </div>
          <p class="record-description">{{ visibleDescription(tags.一句话描述, item.review_status) || '暂无款式描述' }}</p>
        </section>
      </div>
    </template>

    <section class="record-extra-details" aria-label="其他款式信息">
      <details class="detail-fold">
        <summary>来源与操作</summary>
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
          <div><dt>来源资料</dt><dd>{{ item.source.source_file }}</dd></div>
          <div><dt>所在位置</dt><dd>{{ item.source.location }}</dd></div>
          <div v-if="item.image_width && item.image_height"><dt>尺寸</dt><dd>{{ item.image_width }} × {{ item.image_height }} 像素</dd></div>
          <div v-if="item.reviewer"><dt>上次审核</dt><dd>{{ item.reviewer }} · {{ item.reviewed_at ? new Date(item.reviewed_at).toLocaleString('zh-CN', { hour12: false }) : '' }}</dd></div>
          <div v-if="item.review_note"><dt>审核意见</dt><dd>{{ item.review_note }}</dd></div>
        </dl>
        <div class="toolbar">
          <a class="btn" :href="item.download_url || item.image_url" download>下载原图</a>
          <a v-if="item.source_url" class="btn" :href="item.source_url" download>下载来源资料</a>
          <RouterLink class="btn" :to="{ name: 'search', query: { record: item.id } }">找相似款</RouterLink>
          <RouterLink class="btn" :to="{ path: '/review', query: { status: item.review_status, selected: item.id } }">{{ item.review_status === 'pending' ? '前往审核此款' : '查看审核结果' }}</RouterLink>
        </div>
      </details>
      <details v-if="item.image_history?.length" class="detail-fold">
        <summary>历史原图</summary>
        <div class="image-history">
          <a v-for="(old, index) in item.image_history" :key="index" :href="old.url" target="_blank" rel="noopener">
            <img :src="old.url" :alt="old.note || '替换前保留的原图'" width="80" height="106" />
          </a>
        </div>
      </details>
      <details class="detail-fold">
        <summary>分类标签对照</summary>
        <div class="record-tag-table"><table><caption class="visually-hidden">分类标签对照</caption><thead><tr><th scope="col">标签</th><th scope="col">AI 建议{{ item.ai_tags_source === 'mock' ? '（模拟）' : '' }}</th><th scope="col">{{ item.review_status === 'approved' ? '最终分类' : '待审核分类' }}</th></tr></thead><tbody>
          <tr v-for="(value, key) in tags" :key="key" :class="{ changed: item.ai_tags && display(item.ai_tags[key]) !== display(value) }"><th scope="row">{{ key }}</th><td>{{ key === '一句话描述' ? (visibleDescription(display(item.ai_tags?.[key]), item.review_status) || '未提供') : display(item.ai_tags?.[key]) }}</td><td>{{ key === '一句话描述' ? (visibleDescription(display(value), item.review_status) || '未提供') : display(value) }}</td></tr>
        </tbody></table></div>
      </details>
      <details v-if="hasDesignNotes" class="detail-fold">
        <summary>设计说明</summary>
        <DesignNotesList :notes="item.notes" />
      </details>
      <RecordHistory :id="item.id" />
    </section>
  </main>
  <main v-else class="record-page">
    <div class="page-head"><RouterLink class="factory-record-back" :to="back">返回列表</RouterLink><span class="meta">款式详情</span></div>
    <p v-if="loading" class="empty" role="status">正在读取款式…</p>
    <p v-if="error" class="error" role="alert">{{ error }} <RouterLink class="factory-record-back" to="/">返回图库</RouterLink></p>
  </main>
</template>
