<script setup lang="ts">
import { onMounted, reactive, ref, watch } from 'vue'
import { getCatalog, type CatalogItem } from '../api'
import FilterBar from '../components/FilterBar.vue'
import ImageGrid from '../components/ImageGrid.vue'

const filters = reactive({
  category: '全部',
  style: '全部',
  season: '全部',
  color: '全部',
  fit: '全部',
})
const items = ref<CatalogItem[]>([])
const count = ref(0)
const loading = ref(false)
const error = ref('')

async function load() {
  loading.value = true
  error.value = ''
  try {
    const data = await getCatalog(filters)
    items.value = data.items
    count.value = data.count
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

onMounted(load)
watch(filters, load, { deep: true })
</script>

<template>
  <section class="panel">
    <FilterBar v-model="filters" />
    <p class="meta">共 {{ count }} 条{{ loading ? ' · 加载中…' : '' }}</p>
    <div v-if="error" class="error">{{ error }}</div>
    <ImageGrid :items="items" empty-text="暂无图片。请先在「导入」上传 PDF / PPT。" />
  </section>
</template>
