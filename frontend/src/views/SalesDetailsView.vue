<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { getRecord, type CatalogItem, type SalesDetail } from '../api'
import { swatchColor, swatchLight } from '../utils/source'

const route = useRoute()
const item = ref<CatalogItem | null>(null)
const loading = ref(false)
const error = ref('')
const returnToRecord = computed(() => ({
  name: 'record',
  params: { id: String(route.params.id) },
  query: typeof route.query.from === 'string' ? { from: route.query.from } : {},
}))

let latest = 0
watch(() => route.params.id, async id => {
  const seq = ++latest
  item.value = null
  loading.value = true
  error.value = ''
  try {
    const result = await getRecord(String(id))
    if (seq === latest) item.value = result
  } catch (e) {
    if (seq === latest) error.value = e instanceof Error ? e.message : String(e)
  } finally {
    if (seq === latest) loading.value = false
  }
}, { immediate: true })

function variantLocation(sale: SalesDetail) {
  return {
    name: 'record',
    params: { id: String(route.params.id) },
    query: {
      ...(typeof route.query.from === 'string' ? { from: route.query.from } : {}),
      variant: sale.variant_code,
    },
  }
}
</script>

<template>
  <main v-if="item" class="record-page factory-record-page factory-sales-details-view">
    <div class="factory-record-topline">
      <RouterLink class="factory-record-back" :to="returnToRecord">← 返回款式详情</RouterLink>
      <div class="factory-record-identity">
        <strong>{{ item.style_code }}</strong>
        <span v-if="item.library_category" class="factory-category-pill">{{ item.library_category }}</span>
      </div>
      <div v-if="item.sales" class="factory-topline-sales" aria-label="累计销售">
        <span>累计销售</span>
        <strong>{{ item.sales.total_quantity.toLocaleString('zh-CN') }}</strong><span>件</span>
        <span class="sales-dot" aria-hidden="true">·</span>
        <strong>{{ item.sales.total_amount.toLocaleString('zh-CN') }}</strong><span>元</span>
      </div>
    </div>

    <section class="factory-sales-detail-section" aria-label="销售明细">
      <div class="factory-version-table-heading">
        <h2>销售明细</h2>
        <span v-if="item.sales_details !== undefined">
          {{ item.sales_details.length }} 条汇总记录 · 款号、颜色、尺码、年度和周期相同的行已合并
        </span>
      </div>
      <p v-if="item.sales_details === undefined" class="meta sales-detail-unavailable" role="status">
        销售明细暂不可用：当前服务尚未加载此功能。请检查图库服务后刷新。
      </p>
      <p v-else-if="!item.sales_details.length" class="meta">
        该款号还没有已导入的销售明细。可从导入页上传对应销量表。
      </p>
      <div v-else class="factory-version-table-wrap">
        <table class="factory-version-table factory-sales-detail-table">
          <thead>
            <tr>
              <th scope="col">客户</th><th scope="col">大货款号</th><th scope="col">年度</th><th scope="col">周期</th>
              <th scope="col">颜色</th><th scope="col">尺码</th><th scope="col">件数</th><th scope="col">金额（元）</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="sale in item.sales_details" :key="[sale.variant_code, sale.color, sale.size_label, sale.year, sale.period].join('|')">
              <td><RouterLink class="factory-table-select" :to="variantLocation(sale)">{{ sale.customer }}</RouterLink></td>
              <td>{{ sale.variant_code }}</td>
              <td>{{ sale.year || '—' }}</td>
              <td>{{ sale.period || '—' }}</td>
              <td>
                <span class="factory-version-color">
                  <span class="card-dot" :class="{ light: swatchLight(sale.family) }" :style="{ background: swatchColor(sale.family) }"></span>
                  {{ sale.color }}
                </span>
              </td>
              <td>{{ sale.size_label }}</td>
              <td class="numeric">{{ sale.quantity.toLocaleString('zh-CN') }}</td>
              <td class="numeric">{{ sale.amount.toLocaleString('zh-CN') }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </main>
  <main v-else class="record-page factory-record-page">
    <div class="page-head">
      <RouterLink class="factory-record-back" :to="returnToRecord">← 返回款式详情</RouterLink>
      <h2>销售明细</h2>
    </div>
    <p v-if="loading" class="empty" role="status">正在读取销售明细…</p>
    <p v-else-if="error" class="error" role="alert">{{ error }}</p>
  </main>
</template>
