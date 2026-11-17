<script setup lang="ts">
import { computed, nextTick, onMounted, ref } from 'vue'
import { getFilters, getRecord, updateRecordTags, type CatalogItem, type FashionTags, type FilterMeta } from '../api'

const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value))
const props = defineProps<{ item: CatalogItem }>()
const emit = defineEmits<{ close: []; saved: [item: CatalogItem] }>()
const dialog = ref<HTMLDialogElement | null>(null)
const current = ref(props.item)
const draft = ref<Partial<FashionTags>>(clone(props.item.tag_overrides || {}))
const meta = ref<FilterMeta>({ fields: {}, detail_groups: {} })
const loading = ref(true)
const loaded = ref(false)
const saving = ref(false)
const error = ref('')
const conflict = ref(false)
const fields: (keyof FashionTags)[] = ['品类', '风格', '季节', '版型', '颜色主色调', '领型', '袖型', '袖长', '面料', '图案', '细节元素', '一句话描述']
const has = (key: keyof FashionTags) => Object.hasOwn(draft.value, key)
const baseline = computed(() => current.value.ai_tags || current.value.display_tags)
const effective = (key: keyof FashionTags) => has(key) ? draft.value[key] : baseline.value[key]
const show = (value: unknown) => Array.isArray(value) ? value.join('、') || '无' : String(value || '未提供')
const options = (key: keyof FashionTags) => Array.from(new Set([...(meta.value.fields[key] || []), String(effective(key) || '')])).filter(Boolean)
const detailOptions = computed(() => Array.from(new Set([...Object.values(meta.value.detail_groups).flat(), ...(baseline.value.细节元素 || []), ...(draft.value.细节元素 || [])])))
const dirty = computed(() => JSON.stringify(draft.value) !== JSON.stringify(current.value.tag_overrides || {}))
function setValue(key: keyof FashionTags, value: string | string[]) {
  draft.value = { ...draft.value, [key]: value }
}
function toggleDetail(value: string, checked: boolean) {
  const values = new Set(effective('细节元素') as string[] || [])
  if (checked) values.add(value); else values.delete(value)
  setValue('细节元素', [...values])
}
function reset(key: keyof FashionTags) { delete draft.value[key] }
function close() {
  if (saving.value) return
  if (dirty.value && !window.confirm('尚未保存修改，确定放弃吗？')) return
  dialog.value?.close(); emit('close')
}
async function reload() {
  if (dirty.value && !window.confirm('重新读取会放弃当前未保存的修改，继续吗？')) return
  loading.value = true; error.value = ''
  try {
    const [latest, filters] = await Promise.all([getRecord(props.item.id), getFilters()])
    current.value = latest; meta.value = filters; loaded.value = true
    draft.value = clone(current.value.tag_overrides || {})
    conflict.value = false
  } catch (e) { error.value = e instanceof Error ? e.message : String(e) }
  finally { loading.value = false }
}
async function save() {
  if (!dirty.value || !loaded.value || saving.value || loading.value || conflict.value) return
  const original = current.value.tag_overrides || {}
  const changes: Partial<FashionTags> = {}
  const resets: string[] = []
  for (const key of fields) {
    if (has(key)) {
      if (!Object.hasOwn(original, key) || JSON.stringify(original[key]) !== JSON.stringify(draft.value[key]))
        Object.assign(changes, { [key]: draft.value[key] })
    } else if (Object.hasOwn(original, key)) resets.push(key)
  }
  saving.value = true; error.value = ''
  try {
    const updated = await updateRecordTags(current.value, changes, resets)
    dialog.value?.close(); emit('saved', updated); emit('close')
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
    conflict.value = /刷新|其他操作|冲突|重新载入/.test(error.value)
  } finally { saving.value = false }
}
onMounted(async () => {
  await nextTick(); dialog.value?.showModal()
  try {
    const [latest, filters] = await Promise.all([getRecord(props.item.id), getFilters()])
    current.value = latest; draft.value = clone(latest.tag_overrides || {}); meta.value = filters; loaded.value = true
  } catch (e) { error.value = e instanceof Error ? e.message : String(e) }
  finally { loading.value = false }
})
</script>

<template>
  <dialog ref="dialog" class="tag-editor" aria-labelledby="tag-editor-title" aria-describedby="tag-editor-help" @cancel.prevent="close">
    <form @submit.prevent="save">
      <header class="tag-editor-heading">
        <div><p class="meta">{{ current.style_code || current.notes?.款号 || '款式标签' }}</p><h2 id="tag-editor-title">修改标签</h2></div>
        <button class="btn" type="button" :disabled="saving" aria-label="关闭标签编辑" @click="close">关闭</button>
      </header>
      <p id="tag-editor-help">改过的字段以你的修改为准，其他字段沿用 AI。保存后用于图库、文字搜索、以图搜索的标签筛选及数据助手。</p>
      <div class="tag-editor-context"><img :src="current.thumbnail_url || current.image_url" alt="正在修改的款式" /><div><p>AI 原始建议始终保留，重新识别不会覆盖人工修改。</p><p class="meta">以图找款仍会比较图片外观；修改标签不会改变原图。适穿季节是图片标签，不会修改订单年度、订单季节或档案季节。</p></div></div>
      <p v-if="loading" role="status">正在读取最新标签…</p>
      <div v-else-if="loaded" class="tag-editor-fields">
        <div v-for="key in fields" :key="key" class="tag-editor-field" :class="{ 'is-overridden': has(key), 'is-wide': key === '细节元素' || key === '一句话描述' }">
          <div class="tag-editor-field-label"><label :for="`edit-tag-${key}`">{{ key === '季节' ? '适穿季节' : key }}</label><span>{{ has(key) ? '人工修改' : '沿用 AI' }}</span></div>
          <textarea v-if="key === '一句话描述'" :id="`edit-tag-${key}`" :value="effective(key)" rows="3" maxlength="2000" :disabled="saving" @input="setValue(key, ($event.target as HTMLTextAreaElement).value)" />
          <fieldset v-else-if="key === '细节元素'" :id="`edit-tag-${key}`" :disabled="saving"><legend class="visually-hidden">细节元素</legend><label v-for="detail in detailOptions" :key="detail"><input type="checkbox" :checked="(effective(key) as string[] || []).includes(detail)" @change="toggleDetail(detail, ($event.target as HTMLInputElement).checked)" />{{ detail }}</label></fieldset>
          <select v-else :id="`edit-tag-${key}`" :value="effective(key) || ''" :disabled="saving" @change="setValue(key, ($event.target as HTMLSelectElement).value)"><option value="">未填写</option><option v-for="choice in options(key)" :key="choice" :value="choice">{{ choice }}</option></select>
          <p class="tag-editor-ai">AI 建议：{{ show(current.ai_tags?.[key]) }}</p>
          <div class="tag-editor-field-actions"><button type="button" :disabled="saving" @click="setValue(key, key === '细节元素' ? [] : '')">清空</button><button type="button" :disabled="saving || !has(key) || !current.ai_tags" @click="reset(key)">恢复 AI 建议</button></div>
        </div>
      </div>
      <p v-if="error" class="error" role="alert">{{ error }} <button v-if="conflict || !Object.keys(meta.fields).length" class="btn" type="button" :disabled="saving || loading" @click="reload">重新读取最新标签</button></p>
      <footer class="tag-editor-footer"><button type="button" class="btn" :disabled="loading || saving || !Object.keys(draft).length || !current.ai_tags" @click="draft = {}">全部恢复 AI</button><div><button type="button" class="btn" :disabled="saving" @click="close">取消</button><button type="submit" class="btn primary" :disabled="!dirty || !loaded || loading || saving || conflict">{{ saving ? '保存中…' : '保存修改' }}</button></div></footer>
    </form>
  </dialog>
</template>

<style scoped>
.tag-editor { width: min(900px, calc(100vw - 32px)); max-height: calc(100dvh - 40px); padding: 0; border: 1px solid var(--border, #ded5c7); border-radius: 14px; color: var(--text, #292824); background: var(--surface, #fffdfa); }
.tag-editor::backdrop { background: #25272099; }
.tag-editor form { padding: 28px; }
.tag-editor-heading, .tag-editor-field-label, .tag-editor-footer, .tag-editor-footer > div { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.tag-editor h2 { margin: 2px 0; font-size: 25px; }
.tag-editor-heading p { margin: 0; }
#tag-editor-help { color: #5e665b; line-height: 1.7; margin: 18px 0; }
.tag-editor-context { display: flex; align-items: center; gap: 16px; padding: 14px; background: #f4f2ec; border-radius: 8px; margin-bottom: 22px; }
.tag-editor-context img { width: 64px; height: 80px; object-fit: contain; }
.tag-editor-context p { margin: 4px 0; line-height: 1.6; }
.tag-editor-fields { display: grid; grid-template-columns: 1fr 1fr; gap: 20px 24px; }
.tag-editor-field-label { margin-bottom: 8px; }
.tag-editor-field-label label { font-weight: 600; }
.tag-editor-field-label span { font-size: 12px; color: #79766e; }
.is-overridden .tag-editor-field-label span { color: #45604e; font-weight: 600; }
.tag-editor-field select, .tag-editor-field textarea { box-sizing: border-box; width: 100%; padding: 10px; border: 1px solid #d9d2c4; border-radius: 6px; background: white; font: inherit; color: inherit; }
.is-overridden select, .is-overridden textarea { border-color: #748976; }
.is-wide { grid-column: 1 / -1; }
.tag-editor fieldset { border: 1px solid #ded5c7; border-radius: 6px; display: flex; flex-wrap: wrap; gap: 10px 16px; padding: 12px; margin: 0; }
.tag-editor fieldset label { display: inline-flex; align-items: center; gap: 6px; }
.tag-editor-ai { font-size: 13px; color: #706c63; margin: 7px 0; line-height: 1.6; overflow-wrap: anywhere; }
.tag-editor-field-actions { display: flex; gap: 16px; }
.tag-editor-field-actions button { border: 0; background: none; padding: 3px 0; color: #6b674f; font: inherit; font-size: 12px; cursor: pointer; }
.tag-editor button:disabled { opacity: .45; cursor: default; }
.tag-editor-footer { position: sticky; bottom: 0; padding: 16px 0 12px; margin-top: 24px; background: #fffdfa; border-top: 1px solid #e4ddcf; }
.tag-editor :focus-visible { outline: 2px solid #54785c; outline-offset: 3px; }
@media (max-width: 600px) { .tag-editor form { padding: 18px; } .tag-editor-fields { grid-template-columns: 1fr; } .tag-editor-footer { flex-wrap: wrap; } }
</style>
