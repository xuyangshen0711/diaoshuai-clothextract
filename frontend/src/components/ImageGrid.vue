<script setup lang="ts">
import type { CatalogItem } from '../api'

defineProps<{
  items: CatalogItem[]
  emptyText?: string
}>()
</script>

<template>
  <div v-if="!items.length" class="empty">{{ emptyText || '暂无图片' }}</div>
  <div v-else class="grid">
    <article v-for="item in items" :key="item.id" class="card">
      <img :src="item.image_url" :alt="item.tags.一句话描述 || item.id" loading="lazy" />
      <h3>{{ item.tags.一句话描述 || '（无描述）' }}</h3>
      <p class="sub">
        {{ item.tags.品类 }} · {{ item.tags.风格 }} · {{ item.tags.颜色主色调 }}
        <template v-if="item.score != null"> · {{ item.score.toFixed(3) }}</template>
      </p>
      <p class="sub">{{ item.source.source_file }} / {{ item.source.location }}</p>
    </article>
  </div>
</template>
