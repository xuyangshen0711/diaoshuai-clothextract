<script setup lang="ts">
import { computed, ref } from 'vue'
import { updateRecordCover, type CatalogItem, type CoverCrop } from '../api'

const props = defineProps<{ item: CatalogItem }>()
const emit = defineEmits<{ updated: [item: CatalogItem] }>()
const dialog = ref<HTMLDialogElement | null>(null)
const stage = ref<HTMLElement | null>(null)
const selection = ref<CoverCrop>({ left: 0, top: 0, right: 1, bottom: 1 })
const saving = ref(false)
const imageReady = ref(false)
const error = ref('')
let anchor: { x: number; y: number } | null = null
const valid = computed(
  () =>
    selection.value.right - selection.value.left >= 0.01 &&
    selection.value.bottom - selection.value.top >= 0.01,
)
const bounds = [
  { key: 'left', label: '左边界' },
  { key: 'top', label: '上边界' },
  { key: 'right', label: '右边界' },
  { key: 'bottom', label: '下边界' },
] as const
const cropStyle = computed(() => ({
  left: `${selection.value.left * 100}%`,
  top: `${selection.value.top * 100}%`,
  width: `${(selection.value.right - selection.value.left) * 100}%`,
  height: `${(selection.value.bottom - selection.value.top) * 100}%`,
}))
function open() {
  selection.value = props.item.cover_crop
    ? { ...props.item.cover_crop }
    : { left: 0, top: 0, right: 1, bottom: 1 }
  error.value = ''
  dialog.value?.showModal()
}
function point(event: PointerEvent) {
  const rect = stage.value!.getBoundingClientRect()
  return {
    x: Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)),
    y: Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height)),
  }
}
function start(event: PointerEvent) {
  if (!imageReady.value || saving.value || !event.isPrimary || event.button !== 0) return
  anchor = point(event)
  stage.value?.setPointerCapture(event.pointerId)
}
function move(event: PointerEvent) {
  if (!anchor) return
  const end = point(event)
  selection.value = {
    left: Math.min(anchor.x, end.x),
    top: Math.min(anchor.y, end.y),
    right: Math.max(anchor.x, end.x),
    bottom: Math.max(anchor.y, end.y),
  }
}
function stop(event: PointerEvent) {
  if (!anchor) return
  anchor = null
  if (stage.value?.hasPointerCapture(event.pointerId))
    stage.value.releasePointerCapture(event.pointerId)
}
function onImageError() {
  error.value = '原图加载失败，请刷新页面后重试'
  imageReady.value = false
}
async function save(reset = false) {
  if (saving.value || (!reset && (!valid.value || !imageReady.value))) return
  saving.value = true
  error.value = ''
  try {
    const result = await updateRecordCover(props.item, reset ? null : selection.value)
    emit('updated', result)
    dialog.value?.close()
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <button type="button" class="cover-open" @click="open">调整封面</button>
  <dialog
    ref="dialog"
    class="cover-editor"
    aria-labelledby="cover-editor-title"
    @cancel="saving && $event.preventDefault()"
  >
    <header>
      <h2 id="cover-editor-title">调整图库封面</h2>
      <button type="button" aria-label="关闭封面编辑" :disabled="saving" @click="dialog?.close()">
        ×
      </button>
    </header>
    <p class="cover-help">拖动框选服装区域，或输入下方边界百分比。原图和下载文件保持完整。</p>
    <div
      ref="stage"
      class="crop-stage"
      @pointerdown="start"
      @pointermove="move"
      @pointerup="stop"
      @pointercancel="stop"
      @lostpointercapture="anchor = null"
    >
      <img
        :src="item.image_url"
        :alt="`${item.style_code || '款式'}原图，使用边界输入框可通过键盘调整选区`"
        :width="item.image_width || 600"
        :height="item.image_height || 800"
        draggable="false"
        @load="imageReady = true"
        @error="onImageError"
      />
      <div class="crop-selection" :style="cropStyle" aria-hidden="true"><span></span></div>
    </div>
    <fieldset class="crop-boundaries" :disabled="saving">
      <legend>选区边界（%）</legend>
      <label v-for="bound in bounds" :key="bound.key"
        >{{ bound.label
        }}<input
          type="number"
          :name="`cover-${bound.key}`"
          :value="Number((selection[bound.key] * 100).toFixed(1))"
          min="0"
          max="100"
          step="0.1"
          @input="
            selection[bound.key] = Math.max(
              0,
              Math.min(1, Number(($event.target as HTMLInputElement).value) / 100),
            )
          "
      /></label>
    </fieldset>
    <p v-if="!valid" class="cover-error" role="status">请框选至少占原图宽、高各 1% 的区域。</p>
    <p v-if="error" class="cover-error" role="alert">{{ error }}</p>
    <footer>
      <button type="button" class="cover-reset" :disabled="saving" @click="save(true)">
        恢复完整原图
      </button>
      <div>
        <button type="button" :disabled="saving" @click="dialog?.close()">取消</button
        ><button
          type="button"
          class="cover-save"
          :disabled="saving || !valid || !imageReady"
          @click="save(false)"
        >
          {{ saving ? '保存中…' : '保存封面' }}
        </button>
      </div>
    </footer>
  </dialog>
</template>

<style scoped>
.cover-open {
  border: 0;
  background: none;
  padding: 0;
  color: var(--link);
  font-size: inherit;
  cursor: pointer;
}
.cover-open:hover {
  text-decoration: underline;
}
.cover-editor {
  width: min(680px, calc(100vw - 28px));
  max-height: 92vh;
  overflow-y: auto;
  padding: 22px;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  color: var(--ink);
  overscroll-behavior: contain;
}
.cover-editor::backdrop {
  background: #24221fa6;
}
.cover-editor header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}
.cover-editor h2 {
  font-size: 19px;
  font-weight: 600;
  margin: 0;
}
.cover-editor header button {
  font-size: 26px;
  border: 0;
  background: none;
  cursor: pointer;
  color: var(--muted);
}
.cover-help {
  font-size: 12px;
  color: var(--muted);
  margin: 12px 0 18px;
}
.crop-stage {
  position: relative;
  width: fit-content;
  max-width: 100%;
  margin: auto;
  cursor: crosshair;
  touch-action: none;
  overflow: hidden;
  line-height: 0;
}
.crop-stage img {
  display: block;
  width: auto;
  height: auto;
  max-height: 48vh;
  max-width: 100%;
  user-select: none;
}
.crop-selection {
  position: absolute;
  border: 2px solid #fff;
  outline: 1px solid var(--gold);
  box-shadow: 0 0 0 1500px #24221f80;
  pointer-events: none;
}
.crop-selection span {
  display: block;
  width: 100%;
  height: 100%;
  background: repeating-linear-gradient(
    to right,
    transparent 0,
    transparent calc(33.333% - 1px),
    #ffffff50 calc(33.333% - 1px),
    #ffffff50 33.333%
  );
}
.crop-boundaries {
  margin: 18px 0 0;
  padding: 0;
  border: 0;
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
}
.crop-boundaries legend {
  font-size: 12px;
  color: var(--muted);
  margin-bottom: 8px;
}
.crop-boundaries label {
  display: grid;
  gap: 4px;
  color: var(--muted);
  font-size: 11px;
}
.crop-boundaries input {
  border: 1px solid var(--line);
  border-radius: 3px;
  background: var(--search);
  padding: 7px;
  width: 100%;
  min-width: 0;
  color: var(--ink);
  font-size: 13px;
}
.cover-error {
  color: #99443e;
  font-size: 12px;
}
.cover-editor footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-top: 22px;
}
.cover-editor footer > div {
  display: flex;
  gap: 10px;
}
.cover-editor footer button {
  padding: 8px 14px;
  border: 1px solid var(--line);
  background: var(--panel);
  color: var(--ink);
  border-radius: 3px;
  font-size: 12px;
  cursor: pointer;
}
.cover-editor footer .cover-reset {
  padding: 0;
  border: 0;
  color: var(--muted);
}
.cover-editor footer .cover-save {
  color: white;
  background: #8a6a3d;
  border-color: #8a6a3d;
}
.cover-editor button:disabled {
  opacity: 0.45;
  cursor: default;
}
@media (max-width: 600px) {
  .cover-editor {
    padding: 16px;
  }
  .crop-boundaries {
    gap: 8px;
  }
  .cover-editor footer {
    flex-wrap: wrap;
  }
  .cover-editor footer button {
    padding: 8px 10px;
  }
}
</style>
