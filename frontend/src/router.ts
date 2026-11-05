import { createRouter, createWebHistory } from 'vue-router'
import LibraryView from './views/LibraryView.vue'
import SearchView from './views/SearchView.vue'
import ImportView from './views/ImportView.vue'
import ReviewView from './views/ReviewView.vue'
import RecordView from './views/RecordView.vue'
import SalesDetailsView from './views/SalesDetailsView.vue'

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'library', component: LibraryView },
    { path: '/search', name: 'search', component: SearchView },
    { path: '/import', name: 'import', component: ImportView },
    { path: '/records/:id/sales', name: 'sales-details', component: SalesDetailsView },
    { path: '/records/:id', name: 'record', component: RecordView },
    { path: '/review', name: 'review', component: ReviewView },
    { path: '/categories', redirect: '/' },
    { path: '/edit', redirect: '/review' },
  ],
})
