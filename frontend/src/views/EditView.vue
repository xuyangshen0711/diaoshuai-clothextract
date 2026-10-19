<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { getCatalog, getFilters, updateRecord, type CatalogItem, type FashionTags, type FilterMeta } from '../api'

const items = ref<CatalogItem[]>([])
const selectedId = ref('')
const meta = reactive<FilterMeta>({})
const form = reactive<FashionTags>({
  品类: '其他',
  风格: '其他',
  季节: '其他',
  版型: '其他',
  颜色主色调: '其他',
  领型: '其他',
  袖型: '其他',
  细节元素: [],
  一句话描述: '',
})
const detailsText = ref('')
const status = ref('')
const error = ref('')
const saving = ref(false)

const current = computed(() => items.value.find((x) => x.id === selectedId.value) || null)

onMounted(async () => {
  Object.assign(meta, await getFilters())
  const data = await getCatalog({})
  items.value = data.items
  if (items.value.length) selectedId.value = items.value[0].id
})

watch(selectedId, () => {
  const item = current.value
  if (!item) return
  Object.assign(form, item.tags)
  detailsText.value = item.tags.细节元素.join(',')
  status.value = ''
  error.value = ''
})

async function save() {
  if (!current.value) return
  saving.value = true
  error.value = ''
  status.value = ''
  try {
    form.细节元素 = detailsText.value
      .split(',')
      .map((x) => x.trim())
      .filter(Boolean)
    const updated = await updateRecord(current.value.id, { ...form })
    const idx = items.value.findIndex((x) => x.id === updated.id)
    if (idx >= 0) items.value[idx] = updated
    status.value = '已保存，并重建索引'
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <section class="panel">
    <div class="field" style="margin-bottom: 16px">
      <label>选择图片</label>
      <select v-model="selectedId">
        <option v-for="item in items" :key="item.id" :value="item.id">
          {{ item.id }} · {{ item.tags.一句话描述 || item.source.location }}
        </option>
      </select>
    </div>

    <div v-if="current" class="edit-layout">
      <div>
        <img :src="current.image_url" :alt="current.id" />
        <p class="sub" style="margin-top: 10px">
          {{ current.source.source_file }} / {{ current.source.location }}
        </p>
      </div>
      <div>
        <div class="form-grid">
          <div v-for="key in ['品类', '风格', '季节', '版型', '颜色主色调', '领型', '袖型']" :key="key" class="field">
            <label>{{ key }}</label>
            <select v-model="(form as any)[key]">
              <option v-for="opt in meta[key] || []" :key="opt" :value="opt">{{ opt }}</option>
            </select>
          </div>
          <div class="field wide">
            <label>细节元素（逗号分隔）</label>
            <input v-model="detailsText" />
          </div>
          <div class="field wide">
            <label>一句话描述</label>
            <textarea v-model="form.一句话描述" rows="3" />
          </div>
        </div>
        <div class="toolbar" style="margin-top: 14px">
          <button class="btn primary" :disabled="saving" @click="save">保存修正</button>
        </div>
        <div v-if="status" class="status">{{ status }}</div>
        <div v-if="error" class="error">{{ error }}</div>
      </div>
    </div>
  </section>
</template>
