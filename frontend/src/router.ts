import { createRouter, createWebHistory } from 'vue-router'
import { ensureSession } from './api'
import LibraryView from './views/LibraryView.vue'
import SearchView from './views/SearchView.vue'
import ImportView from './views/ImportView.vue'
import RecordView from './views/RecordView.vue'
import LoginView from './views/LoginView.vue'
import AnalyticsView from './views/AnalyticsView.vue'

export const router = createRouter({
  history: createWebHistory(),
  scrollBehavior(to, _from, savedPosition) {
    if (savedPosition) return savedPosition
    if (to.hash) return { el: to.hash, top: 24 }
  },
  routes: [
    { path: '/login', name: 'login', component: LoginView, meta: { public: true } },
    { path: '/', name: 'library', component: LibraryView },
    { path: '/search', name: 'search', component: SearchView },
    { path: '/import', name: 'import', component: ImportView },
    { path: '/records/:id', name: 'record', component: RecordView },
    { path: '/review', name: 'review', redirect: to => typeof to.query.selected === 'string' ? { name: 'record', params: { id: to.query.selected }, query: { edit: 'tags' } } : { path: '/', query: { batch: to.query.batch } } },
    { path: '/analytics', name: 'analytics', component: AnalyticsView },
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
