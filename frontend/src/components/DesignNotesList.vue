<script setup lang="ts">
import { computed } from 'vue'
import { NOTE_FIELDS, type DesignNotes } from '../api'

const props = defineProps<{ notes: DesignNotes | undefined }>()
const filled = computed(() => NOTE_FIELDS.filter(key => key !== '款号' && props.notes?.[key]?.trim()))
</script>

<template>
  <section class="design-notes" aria-label="设计说明">
    <h3>设计说明</h3>
    <dl v-if="filled.length"><div v-for="key in filled" :key="key"><dt>{{ key }}</dt><dd>{{ notes![key] }}</dd></div></dl>
    <p v-else class="meta">资料中没有找到这张图旁边的文字说明。</p>
  </section>
</template>
