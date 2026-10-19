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
      <h3>{{ item.tags.一句话描述 || '未命名款式' }}</h3>
      <p class="sub">
        {{ item.tags.品类 }}
        <template v-if="item.tags.颜色主色调 && item.tags.颜色主色调 !== '其他'">
          · {{ item.tags.颜色主色调 }}
        </template>
      </p>
    </article>
  </div>
</template>
