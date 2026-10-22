import { ref } from 'vue'
import type { CatalogItem } from '../api'
// Preserve the in-memory search when opening a result and returning to the list.
export const searchMode = ref<'text' | 'image'>('text')
export const searchIncludePending = ref(true)
export const searchQuery = ref('')
export const searchFile = ref<File | null>(null)
export const searchItems = ref<CatalogItem[]>([])
export const searchPerformed = ref(false)
export const referenceId = ref('')
export const referenceUrl = ref('')
export const referenceTitle = ref('')
