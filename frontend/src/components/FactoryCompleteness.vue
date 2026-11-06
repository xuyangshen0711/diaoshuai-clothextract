<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from 'vue'
import { getFactoryWorkspace, type FactoryWorkspace } from '../api'
const props = defineProps<{ refreshKey: string }>()
const emit = defineEmits<{ task: [kind: 'images' | 'archive' | 'sales', code: string] }>()
const data = ref<FactoryWorkspace | null>(null)
const expanded = ref(false)
const filter = ref('missing')
const query = ref('')
const page = ref(1)
const error = ref('')
const loading = ref(false)
let sequence = 0
let timer: ReturnType<typeof setTimeout> | undefined
async function load() {
  const id = ++sequence; loading.value = true
  try {
    const result = await getFactoryWorkspace(filter.value, query.value, page.value)
    if (id === sequence) { data.value = result; error.value = '' }
  } catch (e) { if (id === sequence) error.value = String(e instanceof Error ? e.message : e) }
  finally { if (id === sequence) loading.value = false }
}
function show(status: string) { expanded.value = true; filter.value = status; page.value = 1; load() }
watch(() => props.refreshKey, load)
watch(query, () => { clearTimeout(timer); timer = setTimeout(() => { page.value = 1; load() }, 300) })
onMounted(load)
onUnmounted(() => { clearTimeout(timer); sequence++ })
</script>

<template>
  <section class="factory-completeness" aria-label="本厂资料完整度">
    <div class="completeness-heading">
      <div><h3>资料待办</h3><p class="meta">{{ data ? `已记录 ${data.counts.styles} 个款号` : '正在核对资料' }}，补齐后自动更新。</p></div>
      <div v-if="data" class="completeness-counts">
        <button @click="show('image')">待补图片 <strong>{{ data.counts.missing_images }}</strong></button>
        <button @click="show('archive')">待补档案 <strong>{{ data.counts.missing_archives }}</strong></button>
        <button class="optional" @click="show('no_sales')">暂无销量 <span>{{ data.counts.no_sales }}</span><small>可选</small></button>
      </div>
      <button class="text-button" :aria-expanded="expanded" @click="expanded = !expanded">{{ expanded ? '收起清单' : '查看清单' }}</button>
    </div>
    <p v-if="data && !data.counts.styles" class="meta">还没有本厂资料。可以先上传图片，也可以先导入档案或销量。</p>
    <p v-else-if="data && !data.counts.missing_images && !data.counts.missing_archives" class="complete-note">现有款号的图片和档案已齐全。</p>
    <div v-if="expanded" class="completeness-body">
      <div class="toolbar"><label>查看 <select v-model="filter" @change="page = 1; load()"><option value="missing">待补资料</option><option value="image">待补图片</option><option value="archive">待补档案</option><option value="no_sales">暂无销量（可选）</option><option value="all">全部款号</option></select></label>
        <input v-model="query" type="search" placeholder="搜索款号" aria-label="搜索待办款号" />
        <button class="btn" :disabled="loading" @click="load">刷新清单</button></div>
      <div class="workspace-table-wrap" :aria-busy="loading" tabindex="0" role="region" aria-label="可横向滚动的资料清单"><table class="workspace-table"><thead><tr><th>本厂款号</th><th>图片</th><th>款式档案</th><th>销量</th><th>下一步</th></tr></thead>
        <tbody><tr v-for="row in data?.items" :key="row.style_code"><th><RouterLink v-if="row.record_id" :to="`/records/${row.record_id}`">{{ row.style_code }}</RouterLink><span v-else>{{ row.style_code }}</span></th>
          <td :class="{ 'gap-text': !row.has_image }">{{ row.has_image ? (row.pending_review ? '已上传 · 待审核' : '已上传') : '待补图片' }}</td>
          <td :class="{ 'gap-text': !row.has_archive }">{{ row.has_archive ? '已导入' : '待补档案' }}</td><td class="meta">{{ row.has_sales ? '已导入' : '暂无（可选）' }}</td>
          <td><div class="toolbar"><button v-if="!row.has_image" class="btn small" @click="emit('task', 'images', row.style_code)">补图片</button><button v-if="!row.has_archive" class="btn small" @click="emit('task', 'archive', row.style_code)">补档案</button><RouterLink v-if="!row.missing.length && row.record_id" class="text-button" :to="`/records/${row.record_id}`">查看款式</RouterLink></div></td></tr></tbody></table></div>
      <p v-if="data && !data.items.length" class="workspace-empty">{{ query ? '没有找到这个款号。' : '当前没有这类待补资料。' }}</p>
      <div v-if="data && data.total > data.page_size" class="toolbar pagination"><button class="btn" :disabled="page <= 1 || loading" @click="page--; load()">上一页</button><span class="meta">第 {{ page }} 页，共 {{ data.total }} 款</span><button class="btn" :disabled="page * data.page_size >= data.total || loading" @click="page++; load()">下一页</button></div>
    </div>
    <div v-if="data?.sales_policy.reference_rows" class="policy-note">有 {{ data.sales_policy.reference_rows }} 行汇总仅用于核对，未重复计入销量。<details><summary>查看统计口径</summary><p>{{ data.sales_policy.note }}</p></details></div>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
  </section>
</template>
