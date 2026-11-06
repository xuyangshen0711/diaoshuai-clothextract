export type FashionTags = {
  品类: string
  风格: string
  季节: string
  版型: string
  颜色主色调: string
  领型: string
  袖型: string
  图案: string
  细节元素: string[]
  一句话描述: string
  袖长?: string
  面料?: string
}

// Free text printed next to the picture in the source document; order = display order.
export const NOTE_FIELDS = ['款号', '价格', '成分', '材质说明', '灵感价值', '版型特点', '工艺细节',
  '其他说明', '图注', '系列', '分组'] as const
export type DesignNotes = Record<(typeof NOTE_FIELDS)[number], string>

export type ReviewStatus = 'pending' | 'approved' | 'rejected'

export type CoverCrop = { left: number; top: number; right: number; bottom: number }

export type CatalogItem = {
  id: string
  image_path: string
  image_url: string
  thumbnail_url: string
  cover_url?: string
  cover_crop?: CoverCrop | null
  download_url: string
  image_width: number | null
  image_height: number | null
  score?: number
  tags: FashionTags
  notes: DesignNotes
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
  source_category: string
  style_code: string
  library_category: string
  category_level1?: string
  category_level2?: string
  sleeve_length?: string
  fabric?: string
  archive_category?: string
  category_raw?: string
  subcategory?: string
  designer?: string
  craftsman?: string
  design_codes?: string[]
  seasons?: string[]
  sales_quantity: number
  sales_amount: number
  color_families?: string[]
  variant_count?: number
  order_count?: number
  customer_count?: number
  replenishment_count?: number
  replenishment_quantity?: number
  first_order_date?: string
  last_order_date?: string
  average_price?: number
  has_order_lines?: boolean
  has_pending_image: boolean
  pending_image_url: string | null
  variants?: StyleVariant[]
  sales?: SalesSummary | null
  image_history?: { url: string; archived_at?: string | null; note?: string }[]
}

export type StyleVariant = {
  variant_code: string
  customer: string
  season: string
  needle: string
  series: string
  category: string
  material: string
  colors: { name: string; family: string }[]
  sizes: { label: string; number: string; letter: string }[]
  quantity: number
  amount: number
  design_code?: string
  designer?: string
  craftsman?: string
  subcategory?: string
  customer_style_code?: string
  order_count?: number
  buyer_count?: number
  unit_price?: number
  last_order_date?: string
  size_span?: string
}

export type SalesSummary = {
  total_quantity: number
  total_amount: number
  order_count?: number
  customer_count?: number
  average_price?: number
  replenishment_count?: number
  replenishment_quantity?: number
  first_order_date?: string
  last_order_date?: string
  has_order_lines?: boolean
  by_color: { name: string; family?: string; quantity: number; amount: number }[]
  by_size: { name: string; quantity: number; amount: number }[]
  by_customer: { variant_code: string; customer: string; season: string; quantity: number; amount: number }[]
  monthly?: { month: string; order_quantity: number; replenishment_quantity: number }[]
  heatmap?: { sizes: string[]; rows: { color: string; family?: string; quantities: number[]; total: number }[]; totals: number[]; total: number }
  top_customers?: { customer: string; variant_code: string; salesperson: string; order_count: number; quantity: number; amount: number; last_order_date: string }[]
  salespeople?: { name: string; quantity: number; amount: number }[]
  orders?: OrderLine[]
}

export type OrderLine = {
  order_no: string
  order_kind: string
  variant_code: string
  customer: string
  customer_style_code: string
  color: string
  size_label: string
  quantity: number
  amount: number
  order_date: string
  salesperson: string
  season: string
}

export type LibraryMeta = {
  source_categories: string[]
  unclassified: string
  factory: Record<string, string[]>
  style_codes?: string[]
}

export type TableReport = {
  imported: number
  updated: number
  skipped_rows: number
  warnings?: string[]
  style_codes: string[]
  variant_count?: number
  archive_without_image: string[]
  image_without_archive: string[]
  sales_without_image?: string[]
  source_file: string
  row_count?: number
  parsed_quantity?: number
  parsed_amount?: number
  stored_quantity?: number
  stored_amount?: number
  parsed_rows?: number
  format?: string
  format_label?: string
  evidence_columns?: string[]
  sheet_quantity?: number | null
  sheet_amount?: number | null
  reconciled?: boolean | null
  reconcile_note?: string
  order_count?: number
  customer_count?: number
}

export type FilterMeta = {
  fields: Record<string, string[]>
  detail_groups: Record<string, string[]>
}

export type SiteSession = { required: boolean; authenticated: boolean }

let sessionCache: SiteSession | null = null

export function rememberSession(session: SiteSession) {
  sessionCache = session
}

function redirectToLogin() {
  if (window.location.pathname.startsWith('/login')) return
  const next = window.location.pathname + window.location.search
  window.location.assign(`/login?next=${encodeURIComponent(next)}`)
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init)
  if (res.status === 401 && !url.startsWith('/api/login')) {
    rememberSession({ required: true, authenticated: false })
    redirectToLogin()
  }
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

export async function ensureSession(force = false) {
  if (!force && sessionCache) return sessionCache
  sessionCache = await request<SiteSession>('/api/auth/session')
  return sessionCache
}

export async function loginSite(password: string) {
  const result = await request<{ ok: boolean; required: boolean }>('/api/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ password }),
  })
  rememberSession({ required: result.required, authenticated: true })
  return result
}

export async function logoutSite() {
  await request<{ ok: boolean }>('/api/logout', { method: 'POST' })
  rememberSession({ required: true, authenticated: false })
}

export function getHealth() {
  return request<{
    ok: boolean
    openai_ready: boolean
    openai_placeholder?: boolean
    deepseek_ready?: boolean
    deepseek_placeholder?: boolean
    default_backend?: string
    catalog_count: number
    tagger?: string
    embedding_backend?: string
  }>('/api/health')
}

export type TaxonomyNode = { level1: string; children: string[] }
export type Taxonomy = {
  unsorted: string
  sleeves: string[]
  fabrics: string[]
  tree: TaxonomyNode[]
  labels: string[]
}

export function getFilters() {
  return request<FilterMeta>('/api/meta/filters')
}

export function getTaxonomy() {
  return request<Taxonomy>('/api/taxonomy')
}

export function assignRecordCategory(item: CatalogItem, level1: string, level2 = '') {
  return request<CatalogItem>(`/api/records/${item.id}/category`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ level1, level2, expected_revision: item.revision }),
  })
}

export function getLibraryMeta() {
  return request<LibraryMeta>('/api/meta/library')
}

export function getCatalog(params: Record<string, string | boolean>) {
  const qs = new URLSearchParams()
  Object.entries(params).forEach(([k, v]) => {
    if (v === undefined || v === null || v === '' || v === '全部') return
    qs.set(k, String(v))
  })
  if (!('exclude_samples' in params)) qs.set('exclude_samples', 'true')
  const suffix = qs.toString() ? `?${qs}` : ''
  return request<{ count: number; items: CatalogItem[]; color_counts?: Record<string, number> }>(`/api/catalog${suffix}`)
}

export type SearchInfo = { engine: string; local_only: boolean; include_pending: boolean; concepts: string[] }

export function searchText(q: string, params: Record<string, string | boolean>) {
  const qs = new URLSearchParams({ q })
  Object.entries(params).forEach(([k, v]) => {
    if (v !== '' && v !== '全部') qs.set(k, String(v))
  })
  return request<{ count: number; items: CatalogItem[]; search_info: SearchInfo }>(`/api/search/text?${qs}`)
}

export async function searchImage(file: File, params: Record<string, string | boolean>) {
  const body = new FormData()
  body.append('file', file)
  const qs = new URLSearchParams()
  Object.entries(params).forEach(([k, v]) => {
    if (v !== '' && v !== '全部') qs.set(k, String(v))
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
  skipped?: number
  source_categories?: string[]
}

export async function importFiles(files: { file: File; relativePath: string; category: string }[], backend: string) {
  const body = new FormData()
  files.forEach(item => {
    body.append('files', item.file, item.relativePath || item.file.name)
    body.append('source_categories', item.category)
  })
  return request<ImportJob>(`/api/import/jobs?backend=${encodeURIComponent(backend)}`, { method: 'POST', body })
}

export async function importStyleArchive(file: File) {
  const body = new FormData()
  body.append('file', file)
  return request<TableReport>('/api/import/style-archive', { method: 'POST', body })
}

export async function importSalesSheet(file: File) {
  const body = new FormData()
  body.append('file', file)
  return request<TableReport>('/api/import/sales', { method: 'POST', body })
}

export function assignSourceCategory(ids: string[], sourceCategory: string) {
  return request<{ count: number; items: CatalogItem[] }>('/api/records/source-category', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ids, source_category: sourceCategory }),
  })
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

export function updateRecordCover(item: CatalogItem, crop: CoverCrop | null) {
  return request<CatalogItem>(`/api/records/${encodeURIComponent(item.id)}/cover`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ crop, expected_revision: item.revision }),
  })
}

export function getSimilarRecords(id: string, includePending = false) {
  return request<{ count: number; items: CatalogItem[] }>(`/api/records/${encodeURIComponent(id)}/similar?include_pending=${includePending}`)
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

export function reviewRecord(item: CatalogItem, action: 'approve' | 'reject' | 'resubmit' | 'keep_image',
  reviewer: string, note: string, tags?: FashionTags, notes?: DesignNotes) {
  return request<CatalogItem>(`/api/records/${item.id}/review`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action, reviewer, note, tags, notes, expected_revision: item.revision }),
  })
}

export function rebuildIndex() {
  return request<{ ok: boolean; catalog_count: number }>('/api/index/rebuild', { method: 'POST' })
}

export type WorkspaceRow = { style_code: string; has_image: boolean; has_archive: boolean; has_sales: boolean; record_id: string | null; pending_review: boolean; missing: string[] }
export type FactoryWorkspace = {
  counts: { styles: number; missing_images: number; missing_archives: number; no_sales: number; pending_review: number }
  items: WorkspaceRow[]; total: number; page: number; page_size: number
  sales_policy: { reference_rows: number; reference_quantity: number; note: string }
}
export type TablePreview = {
  historical_start?: string; historical_end?: string
  token: string; kind: 'archive' | 'sales'; requested_kind: string; source_file: string
  format: string; format_label: string; row_count: number; style_codes: string[]
  counts: { new: number; updated: number; unchanged: number }
  meta: { warnings?: string[]; parsed_quantity?: number; parsed_amount?: number; sheet_quantity?: number | null; sheet_amount?: number | null; reconciled?: boolean | null; reconcile_note?: string; evidence_columns?: string[] }
  samples: { variant_code: string; style_code: string; customer: string; color?: string; size_label?: string; quantity?: number; amount?: number; season?: string; action: string }[]
  issues: { row: number; style_code: string; message: string }[]; issue_count: number; ignored_rows: number
  overlap_variants: string[]; missing_images: string[]; can_confirm: boolean
}
export type TableImportResult = TableReport & { id: string; kind: 'archive' | 'sales'; created_at: string; unchanged: number; issue_count: number; sales_policy: FactoryWorkspace['sales_policy'] }
export type InspectedFile = { name: string; kind: string; action: string; style_code: string; message: string; width?: number; height?: number }
export function getFactoryWorkspace(status = 'missing', q = '', page = 1) {
  return request<FactoryWorkspace>(`/api/import/workspace?${new URLSearchParams({ status, q, page: String(page) })}`)
}
export function getTableHistory() { return request<TableImportResult[]>('/api/import/table-history') }
export function previewTable(file: File, kind: string) {
  const body = new FormData(); body.append('file', file); body.append('kind', kind)
  return request<TablePreview>('/api/import/table-preview', { method: 'POST', body })
}
export function confirmTable(token: string, acknowledge: boolean, historical_start = '', historical_end = '') {
  return request<TableImportResult>('/api/import/table-confirm', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token, acknowledge, historical_start, historical_end }) })
}
export function replanTable(token: string, historical_start = '', historical_end = '') {
  return request<TablePreview>('/api/import/table-replan', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token, historical_start, historical_end }) })
}
export function inspectImport(files: { file: File; name: string }[], category: string) {
  const body = new FormData(); files.forEach(f => body.append('files', f.file, f.name)); body.append('category', category)
  return request<{ items: InspectedFile[] }>('/api/import/inspect', { method: 'POST', body })
}
export function classifyBatch(backend: string, batch_id: string) {
  return request<ImportJob>('/api/tag/jobs', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ backend, batch_id, overwrite: false }) })
}
