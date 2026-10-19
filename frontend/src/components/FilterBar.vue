<script setup lang="ts">
import { onMounted, reactive, watch } from 'vue'
import { getFilters, type FilterMeta } from '../api'

const props = defineProps<{
  modelValue: Record<string, string>
}>()

const emit = defineEmits<{
  'update:modelValue': [value: Record<string, string>]
}>()

const keys = ['品类', '风格', '季节', '颜色主色调', '版型'] as const
const keyMap: Record<(typeof keys)[number], string> = {
  品类: 'category',
  风格: 'style',
  季节: 'season',
  颜色主色调: 'color',
  版型: 'fit',
}

const meta = reactive<FilterMeta>({})
const local = reactive<Record<string, string>>({ ...props.modelValue })

onMounted(async () => {
  const data = await getFilters()
  Object.assign(meta, data)
})

watch(
  local,
  () => {
    emit('update:modelValue', { ...local })
  },
  { deep: true },
)
</script>

<template>
  <div class="filters">
    <div v-for="label in keys" :key="label" class="field">
      <label>{{ label }}</label>
      <select v-model="local[keyMap[label]]">
        <option value="全部">全部</option>
        <option v-for="opt in meta[label] || []" :key="opt" :value="opt">{{ opt }}</option>
      </select>
    </div>
  </div>
</template>
