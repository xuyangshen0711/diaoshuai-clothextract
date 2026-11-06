<script setup lang="ts">
import { computed, nextTick, ref } from 'vue'
import type { CatalogItem } from '../api'
import DesignNotesList from './DesignNotesList.vue'
import RecordHistory from './RecordHistory.vue'
import { visibleDescription } from '../utils/source'
const props = defineProps<{ item: CatalogItem; reference: boolean }>()
const tags = computed(() => props.item.display_tags)
const root = ref<HTMLElement | null>(null)
const comparison = ref<HTMLDetailsElement | null>(null)
const historyOpened = ref(false)
const display = (value: unknown) => Array.isArray(value) ? value.join('、') || '无' : String(value || '未提供')
async function showTags() {
  if (root.value instanceof HTMLDetailsElement) root.value.open = true
  if (comparison.value) comparison.value.open = true
  await nextTick()
  comparison.value?.scrollIntoView({ behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth', block: 'start' })
  comparison.value?.querySelector('summary')?.focus({ preventScroll: true })
}
defineExpose({ showTags })
</script>
<template>
  <component :is="reference ? 'details' : 'div'" ref="root" class="record-extras" :class="{ 'reference-extras': reference }">
    <summary v-if="reference">分类标签对照与审核记录</summary>
    <div class="record-extras-content">
      <details v-if="item.image_history?.length" class="detail-fold">
        <summary>历史原图</summary>
        <div class="image-history">
          <a
            v-for="(old, index) in item.image_history"
            :key="index"
            :href="old.url"
            target="_blank"
            rel="noopener"
          >
            <img :src="old.url" :alt="old.note || '替换前保留的原图'" width="80" height="106" />
          </a>
        </div>
      </details>
      <details ref="comparison" class="detail-fold">
        <summary>分类标签对照</summary>
        <div class="record-tag-table" tabindex="0" role="region" aria-label="可横向滚动的分类标签对照">
          <table>
            <caption class="visually-hidden">
              分类标签对照
            </caption>
            <thead>
              <tr>
                <th scope="col">标签</th>
                <th scope="col">AI 建议{{ item.ai_tags_source === 'mock' ? '（模拟）' : '' }}</th>
                <th scope="col">
                  {{ item.review_status === 'approved' ? '最终分类' : '待审核分类' }}
                </th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="(value, key) in tags"
                :key="key"
                :class="{ changed: item.ai_tags && display(item.ai_tags[key]) !== display(value) }"
              >
                <th scope="row">{{ key }}</th>
                <td>
                  {{
                    key === '一句话描述'
                      ? visibleDescription(display(item.ai_tags?.[key]), item.review_status) ||
                        '未提供'
                      : display(item.ai_tags?.[key])
                  }}
                </td>
                <td>
                  {{
                    key === '一句话描述'
                      ? visibleDescription(display(value), item.review_status) || '未提供'
                      : display(value)
                  }}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </details>
      <details class="detail-fold">
        <summary>设计说明</summary>
        <DesignNotesList :notes="item.notes" />
      </details>
      <details class="detail-fold" @toggle="historyOpened = historyOpened || ($event.target as HTMLDetailsElement).open">
        <summary>审核记录</summary>
        <RecordHistory v-if="historyOpened" :id="item.id" />
      </details>
    </div>
  </component>
</template>
