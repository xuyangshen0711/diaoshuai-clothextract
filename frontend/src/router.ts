import { createRouter, createWebHistory } from 'vue-router'
import LibraryView from './views/LibraryView.vue'
import SearchView from './views/SearchView.vue'
import ImportView from './views/ImportView.vue'
import EditView from './views/EditView.vue'

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'library', component: LibraryView },
    { path: '/search', name: 'search', component: SearchView },
    { path: '/import', name: 'import', component: ImportView },
    { path: '/edit', name: 'edit', component: EditView },
  ],
})
