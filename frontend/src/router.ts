import { createRouter, createWebHistory } from 'vue-router'
import { ensureSession } from './api'
import LibraryView from './views/LibraryView.vue'
import SearchView from './views/SearchView.vue'
import ImportView from './views/ImportView.vue'
import ReviewView from './views/ReviewView.vue'
import RecordView from './views/RecordView.vue'
import LoginView from './views/LoginView.vue'

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', name: 'login', component: LoginView, meta: { public: true } },
    { path: '/', name: 'library', component: LibraryView },
    { path: '/search', name: 'search', component: SearchView },
    { path: '/import', name: 'import', component: ImportView },
    { path: '/records/:id', name: 'record', component: RecordView },
    { path: '/review', name: 'review', component: ReviewView },
    { path: '/categories', redirect: '/' },
    { path: '/edit', redirect: '/review' },
  ],
})

function safeNext(value: unknown) {
  if (typeof value !== 'string' || !value.startsWith('/') || value.startsWith('//') || value.startsWith('/login')) return '/'
  return value
}

router.beforeEach(async to => {
  const session = await ensureSession()
  if (to.name === 'login') {
    if (!session.required || session.authenticated) return safeNext(to.query.next)
    return true
  }
  if (session.required && !session.authenticated) {
    return { name: 'login', query: { next: to.fullPath } }
  }
  return true
})
