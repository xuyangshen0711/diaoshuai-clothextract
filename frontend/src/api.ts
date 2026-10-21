export type FashionTags = {
  品类: string
  风格: string
  季节: string
  版型: string
  颜色主色调: string
  领型: string
  袖型: string
  细节元素: string[]
  一句话描述: string
}

export type ReviewStatus = 'pending' | 'approved' | 'rejected'

export type CatalogItem = {
  id: string
  image_path: string
  image_url: string
  score?: number
  tags: FashionTags
  display_tags: FashionTags
  tags_source: string
  manually_edited: boolean
  ai_tags: FashionTags | null
  ai_tags_source: string | null
  proposed_tags: FashionTags | null
  review_status: ReviewStatus
  reviewer: string | null
  reviewed_at: string | null
  review_note: string
  revision: number
  source: {
    source_file: string
    source_type: string
    location: string
    batch_id: string | null
    uploaded_at: string | null
    extracted_at: string
  }
  source_url?: string | null
}

export type FilterMeta = Record<string, string[]>

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init)
  if (!res.ok) {
    let detail = res.statusText
    try {
      const body = await res.json()
      detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail || body)
    } catch {
      /* ignore */
    }
    throw new Error(detail)
  }
  return res.json() as Promise<T>
}

export function getHealth() {
  return request<{
    ok: boolean
    openai_ready: boolean
    catalog_count: number
    tagger?: string
    embedding_backend?: string
  }>('/api/health')
}

export function getFilters() {
  return request<FilterMeta>('/api/meta/filters')
}

export function getCatalog(params: Record<string, string | boolean>) {
  const qs = new URLSearchParams()
  Object.entries(params).forEach(([k, v]) => {
    if (v === undefined || v === null || v === '' || v === '全部') return
    qs.set(k, String(v))
  })
  if (!('exclude_samples' in params)) qs.set('exclude_samples', 'true')
  const suffix = qs.toString() ? `?${qs}` : ''
  return request<{ count: number; items: CatalogItem[] }>(`/api/catalog${suffix}`)
}

export function searchText(q: string, params: Record<string, string>) {
  const qs = new URLSearchParams({ q })
  Object.entries(params).forEach(([k, v]) => {
    if (v && v !== '全部') qs.set(k, v)
  })
  return request<{ count: number; items: CatalogItem[] }>(`/api/search/text?${qs}`)
}

export async function searchImage(file: File, params: Record<string, string>) {
  const body = new FormData()
  body.append('file', file)
  const qs = new URLSearchParams()
  Object.entries(params).forEach(([k, v]) => {
    if (v && v !== '全部') qs.set(k, v)
  })
  const suffix = qs.toString() ? `?${qs}` : ''
  return request<{ count: number; items: CatalogItem[] }>(`/api/search/image${suffix}`, {
    method: 'POST',
    body,
  })
}

export type ImportJob = {
  id: string
  kind: 'import' | 'retag'
  backend: string
  status: 'queued' | 'extracting' | 'tagging' | 'completed' | 'partial' | 'failed' | 'interrupted'
  name: string
  created_at: string
  updated_at: string
  total: number
  processed: number
  succeeded: number
  filtered: number
  failed: number
  files_total: number
  files_processed: number
  elapsed_seconds: number
  eta_seconds: number | null
  current_file: string
  message: string
  errors: { name: string; message: string }[]
}

export async function importFiles(files: File[], backend: string) {
  const body = new FormData()
  files.forEach(f => body.append('files', f))
  return request<ImportJob>(`/api/import/jobs?backend=${encodeURIComponent(backend)}`, { method: 'POST', body })
}

export function retag(backend: string, overwrite: boolean) {
  return request<ImportJob>('/api/tag/jobs', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ backend, overwrite }),
  })
}

export function getJobs() {
  return request<{ items: ImportJob[] }>('/api/import/jobs')
}

export function retryJob(id: string) {
  return request<ImportJob>(`/api/import/jobs/${id}/retry`, { method: 'POST' })
}

export function getRecord(id: string) {
  return request<CatalogItem>(`/api/records/${encodeURIComponent(id)}`)
}

export function getSimilarRecords(id: string) {
  return request<{ count: number; items: CatalogItem[] }>(`/api/records/${encodeURIComponent(id)}/similar`)
}

export type ReviewSnapshot = Pick<CatalogItem, 'tags' | 'ai_tags' | 'proposed_tags' | 'review_status'>
export type ReviewEvent = {
  id: number
  action: string
  actor: string
  note: string
  created_at: string
  before: ReviewSnapshot | null
  after: ReviewSnapshot
}

export function getReviews(status: ReviewStatus, page = 1, params: Record<string, string> = {}) {
  const qs = new URLSearchParams({ status, page: String(page), ...params })
  return request<{
    count: number
    counts: Record<ReviewStatus, number>
    page: number
    facets: { batches: { id: string; label: string }[]; files: string[] }
    items: CatalogItem[]
  }>(`/api/reviews?${qs}`)
}

export function getReviewHistory(id: string) {
  return request<{ items: ReviewEvent[] }>(`/api/records/${id}/history`)
}

export function reviewRecord(item: CatalogItem, action: 'approve' | 'reject' | 'resubmit',
  reviewer: string, note: string, tags?: FashionTags) {
  return request<CatalogItem>(`/api/records/${item.id}/review`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action, reviewer, note, tags, expected_revision: item.revision }),
  })
}

export function rebuildIndex() {
  return request<{ ok: boolean; catalog_count: number }>('/api/index/rebuild', { method: 'POST' })
}
