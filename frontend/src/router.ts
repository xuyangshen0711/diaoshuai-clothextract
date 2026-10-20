import { createRouter, createWebHistory } from 'vue-router'
import LibraryView from './views/LibraryView.vue'
import SearchView from './views/SearchView.vue'
import ImportView from './views/ImportView.vue'
import ReviewView from './views/ReviewView.vue'
import RecordView from './views/RecordView.vue'

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'library', component: LibraryView },
    { path: '/search', name: 'search', component: SearchView },
    { path: '/import', name: 'import', component: ImportView },
    { path: '/records/:id', name: 'record', component: RecordView },
    { path: '/review', name: 'review', component: ReviewView },
    { path: '/edit', redirect: '/review' },
  ],
})
