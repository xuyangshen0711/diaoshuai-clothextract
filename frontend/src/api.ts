import { batchProblem, fileProblem } from './utils/uploadGuidance'

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
  tag_overrides: Partial<FashionTags>
  entry_ready: boolean
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
  order_seasons?: string[]
  archive_seasons?: string[]
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
  impact?: SalesImportImpact
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
const READ_CACHE_TTL_MS = 60_000
type TimedEntry<T> = { at: number; value?: T; inflight?: Promise<T> }
let filtersMemo: TimedEntry<FilterMeta> | null = null
let taxonomyMemo: TimedEntry<Taxonomy> | null = null
let libraryMetaMemo: TimedEntry<LibraryMeta> | null = null
export type CatalogResponse = { count: number; items: CatalogItem[]; color_counts?: Record<string, number> }
const catalogViewCache = new Map<string, { at: number; data: CatalogResponse }>()

export function invalidateReadCaches() {
  filtersMemo = null
  taxonomyMemo = null
  libraryMetaMemo = null
  catalogViewCache.clear()
}

function memoizedGet<T>(
  slot: () => TimedEntry<T> | null,
  assign: (next: TimedEntry<T> | null) => void,
  fetcher: () => Promise<T>,
): Promise<T> {
  const now = Date.now()
  const current = slot()
  if (current?.value !== undefined && now - current.at < READ_CACHE_TTL_MS) {
    return Promise.resolve(current.value)
  }
  if (current?.inflight) return current.inflight
  const entry: TimedEntry<T> = { at: now }
  const p = fetcher()
    .then(val => {
      assign({ at: Date.now(), value: val })
      return val
    })
    .catch(err => {
      if (slot() === entry) assign(null)
      throw err
    })
  entry.inflight = p
  assign(entry)
  return p
}

export function rememberSession(session: SiteSession) {
  sessionCache = session
}

function redirectToLogin() {
  if (window.location.pathname.startsWith('/login')) return
  const next = window.location.pathname + window.location.search
  window.location.assign(`/login?next=${encodeURIComponent(next)}`)
}

export async function requestResponse(url: string, init?: RequestInit): Promise<Response> {
  const method = (init?.method || 'GET').toUpperCase()
  const res = await fetch(url, init)
  if (method !== 'GET' && res.ok) {
    invalidateReadCaches()
  }
  if (res.status === 401 && !url.startsWith('/api/login')) {
    rememberSession({ required: true, authenticated: false })
    redirectToLogin()
  }
  if (!res.ok) {
    let detail = res.statusText
    try {
      const body = await res.json()
      if (typeof body.detail === 'string') {
        detail = body.detail
      } else if (Array.isArray(body.detail) && body.detail.length) {
        const first = body.detail[0]
        if (first?.type === 'too_long' && Array.isArray(first?.loc) && first.loc.includes('ids')) {
          detail = `单次提交数量超出限制（最多 ${first?.ctx?.max_length ?? ''} 条），请分批操作或刷新页面重试。`
        } else if (typeof first?.msg === 'string') {
          detail = `请求参数格式有误：${first.msg}`
        } else {
          detail = '请求参数未通过校验，请核对后重试。'
        }
      } else {
        detail = JSON.stringify(body.detail || body)
      }
    } catch {
      /* ignore */
    }
    if (res.status === 413 && detail === res.statusText) detail = '文件超过本次上传上限，请刷新页面使用分批上传；单张图片最多 100 MB，文档最多 512 MB，单批最多 2 GB。'
    if (res.status === 504 && url.startsWith('/api/import/table-')) {
      detail = url.startsWith('/api/import/table-confirm')
        ? '确认入库响应超时，结果尚未确认。请先查看导入记录；也可再次点击确认，系统会按同一预览编号返回结果，不会重复累计。'
        : '表格预览等待超时，尚未确认入库。请稍后重新选择文件；这不是表头格式错误。'
    }
    throw new Error(detail)
  }
  return res
}

export async function request<T>(url: string, init?: RequestInit): Promise<T> {
  return (await requestResponse(url, init)).json() as Promise<T>
}

export async function ensureSession(force = false) {
  if (!force && sessionCache) return sessionCache
  const controller = new AbortController()
  const timer = window.setTimeout(() => controller.abort(), 15000)
  try {
    const session = await request<SiteSession>('/api/auth/session', { signal: controller.signal, cache: 'no-store' })
    if (typeof session.required !== 'boolean' || typeof session.authenticated !== 'boolean') throw new Error('登录状态读取失败，请刷新重试。')
    sessionCache = session
    return sessionCache
  } finally { window.clearTimeout(timer) }
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

export type AiProvider = 'deepseek' | 'openai'
export type AiService = {
  provider: AiProvider
  name: string
  configured: boolean
  placeholder: boolean
  model: string
  status: 'not_configured' | 'placeholder' | 'unchecked' | 'checking' | 'connected' | 'error' | 'unsupported'
  message: string
  checked_at: string | null
  latency_ms: number | null
  model_listed: boolean | null
}
export type AiServiceStatus = { default_backend: string; providers: AiService[] }

async function serviceRequest<T>(url: string, init?: RequestInit): Promise<T> {
  const controller = new AbortController()
  const timer = window.setTimeout(() => controller.abort(), 15000)
  try { return await request<T>(url, { ...init, signal: controller.signal }) }
  finally { window.clearTimeout(timer) }
}
export function getAiServiceStatus() {
  return serviceRequest<AiServiceStatus>('/api/services/ai')
}
export function checkAiService(provider: AiProvider) {
  return serviceRequest<AiService>(`/api/services/ai/${provider}/check`, { method: 'POST' })
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
  return memoizedGet(() => filtersMemo, v => { filtersMemo = v }, () => request<FilterMeta>('/api/meta/filters'))
}

export function getTaxonomy() {
  return memoizedGet(() => taxonomyMemo, v => { taxonomyMemo = v }, () => request<Taxonomy>('/api/taxonomy'))
}

export function assignRecordCategory(item: CatalogItem, level1: string, level2 = '') {
  return request<CatalogItem>(`/api/records/${item.id}/category`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ level1, level2, expected_revision: item.revision }),
  })
}

export function getLibraryMeta() {
  return memoizedGet(() => libraryMetaMemo, v => { libraryMetaMemo = v }, () => request<LibraryMeta>('/api/meta/library'))
}

function catalogQuerySuffix(params: Record<string, string | string[] | boolean>) {
  const qs = new URLSearchParams()
  Object.entries(params).forEach(([k, v]) => {
    if (v === undefined || v === null || v === '' || v === '全部') return
    for (const value of new Set(Array.isArray(v) ? v : [v])) {
      if (value !== '' && value !== '全部') qs.append(k, String(value))
    }
  })
  if (!('exclude_samples' in params)) qs.set('exclude_samples', 'true')
  return qs.toString() ? `?${qs}` : ''
}

export function peekCatalogCache(params: Record<string, string | string[] | boolean>): CatalogResponse | null {
  const key = catalogQuerySuffix(params)
  const hit = catalogViewCache.get(key)
  if (!hit) return null
  if (Date.now() - hit.at > READ_CACHE_TTL_MS) {
    catalogViewCache.delete(key)
    return null
  }
  return hit.data
}

export async function getCatalog(params: Record<string, string | string[] | boolean>) {
  const suffix = catalogQuerySuffix(params)
  const data = await request<CatalogResponse>(`/api/catalog${suffix}`)
  if (catalogViewCache.size >= 12) {
    const oldest = catalogViewCache.keys().next().value
    if (oldest !== undefined) catalogViewCache.delete(oldest)
  }
  catalogViewCache.set(suffix, { at: Date.now(), data })
  return data
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

export type ImportSource = { index: number; name: string; deleted: boolean; source_category: string }

export type ImportJob = {
  id: string
  kind: 'import' | 'retag'
  backend: string
  status: 'queued' | 'extracting' | 'tagging' | 'awaiting_ai' | 'completed' | 'partial' | 'failed' | 'interrupted' | 'deleted'
  name: string
  created_at: string
  updated_at: string
  total: number
  processed: number
  running: number
  pending: number
  succeeded: number
  filtered: number
  failed: number
  files_total: number
  files_processed: number
  files?: ImportSource[]
  elapsed_seconds: number
  eta_seconds: number | null
  current_file: string
  message: string
  errors: { name: string; message: string }[]
  skipped?: number
  source_categories?: string[]
}

export type ImportQueue = { concurrency: number; running: number; pending: number; batches: number; preparing: number; processed: number; total: number }

export type UploadSession = { id: string; chunk_size: number; files: { size: number; received: number }[] }
export type UploadEntry = { file: File; name: string; styleCode?: string }
export function createUpload(files: UploadEntry[], category: string) {
  const total = files.reduce((sum, entry) => sum + entry.file.size, 0)
  const problem = batchProblem(files.length, total) || files.map(entry => fileProblem(entry.name, entry.file.size)).find(Boolean)
  if (problem) throw new Error(problem)
  return request<UploadSession>('/api/import/uploads', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ category, files: files.map(entry => ({ name: entry.name, size: entry.file.size, style_code: entry.styleCode })) }) })
}
export function getUploadActivity(exclude: string[]) {
  return request<{ active_uploads: number }>(`/api/import/uploads/activity?${new URLSearchParams({ exclude: exclude.join(',') })}`, { signal: AbortSignal.timeout(5000) })
}
export function setUploadActivity(id: string, active: boolean) {
  return request(`/api/import/uploads/${id}/activity`, { method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ active }), signal: AbortSignal.timeout(5000) })
}
export async function transferUpload(id: string, files: UploadEntry[], progress: (sent: number, total: number, current?: { index: number; name: string }) => void, signal?: AbortSignal) {
  const url = `/api/import/uploads/${id}`
  const session = await request<UploadSession>(url, { signal })
  const total = session.files.reduce((sum, file) => sum + file.size, 0)
  let sent = session.files.reduce((sum, file) => sum + file.received, 0)
  progress(sent, total)
  for (const [index, entry] of files.entries()) {
    const saved = session.files[index]!
    // The server may have stopped part way through a chunk. Re-send that chunk only.
    let offset = saved.received === saved.size ? saved.size : Math.floor(saved.received / session.chunk_size) * session.chunk_size
    sent -= saved.received - offset
    progress(sent, total, { index, name: entry.name })
    while (offset < entry.file.size) {
      const end = Math.min(offset + session.chunk_size, entry.file.size)
      const body = new FormData()
      body.append('file', entry.file.slice(offset, end), 'chunk')
      await request(`${url}/files/${index}?offset=${offset}`, { method: 'PUT', body, signal })
      sent += end - offset
      offset = end
      progress(sent, total, { index, name: entry.name })
    }
  }
}
export function inspectUpload(id: string) {
  return request<{ items: InspectedFile[] }>(`/api/import/uploads/${id}/inspect`, { method: 'POST' })
}
export function commitUpload(id: string, indices: number[], backend: string) {
  return request<ImportJob>(`/api/import/uploads/${id}/commit`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ indices, backend }) })
}

type DeletionResult = { deleted: number; cleanup_pending: number }
type SourceDeletionPreview = { name: string; image_count: number; delete_source: boolean; deleted: boolean }

export function previewImportFileDeletion(jobId: string, fileIndex: number) {
  return request<SourceDeletionPreview>(`/api/import/jobs/${jobId}/files/${fileIndex}`)
}

export function deleteImportFile(jobId: string, fileIndex: number, preview: SourceDeletionPreview) {
  const query = new URLSearchParams({
    expected_count: String(preview.image_count), expected_source: String(preview.delete_source),
  })
  return request<DeletionResult>(`/api/import/jobs/${jobId}/files/${fileIndex}?${query}`, { method: 'DELETE' })
}

export function deleteGalleryImages(ids: string[]) {
  return request<DeletionResult>('/api/records/bulk-delete', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ids }),
  })
}

export function retryMediaCleanup() {
  return request<{ cleanup_pending: number }>('/api/media/deletions/retry', { method: 'POST' })
}

export async function importFiles(files: { file: File; relativePath: string; category: string; styleCode?: string }[], backend: string) {
  const body = new FormData()
  const hasStyleCodes = files.some(item => item.styleCode !== undefined)
  files.forEach(item => {
    body.append('files', item.file, item.relativePath || item.file.name)
    body.append('source_categories', item.category)
    if (hasStyleCodes) body.append('style_codes', item.styleCode ?? '')
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

export type SkippedCategoryConflict = {
  id: string
  style_code: string
  source_file: string
  reason: string
}

export type SourceCategoryUpdateResult = {
  count: number
  moved_count: number
  unchanged_count: number
  skipped_conflicts: SkippedCategoryConflict[]
  items: CatalogItem[]
}

export function assignSourceCategory(
  ids: string[],
  sourceCategory: string,
  options?: { skipConflicts?: boolean; returnItems?: boolean },
) {
  return request<SourceCategoryUpdateResult>('/api/records/source-category', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      ids,
      source_category: sourceCategory,
      skip_conflicts: Boolean(options?.skipConflicts),
      return_items: options?.returnItems ?? true,
    }),
  })
}

export async function batchMoveSourceCategory(
  ids: string[],
  sourceCategory: string,
  onProgress?: (done: number, total: number) => void,
): Promise<Omit<SourceCategoryUpdateResult, 'items'>> {
  const chunkSize = 500
  let count = 0
  let movedCount = 0
  let unchangedCount = 0
  const skippedConflicts: SkippedCategoryConflict[] = []
  const total = ids.length
  for (let offset = 0; offset < total; offset += chunkSize) {
    const chunk = ids.slice(offset, offset + chunkSize)
    const res = await assignSourceCategory(chunk, sourceCategory, {
      skipConflicts: true,
      returnItems: false,
    })
    count += res.count
    movedCount += res.moved_count ?? res.count
    unchangedCount += res.unchanged_count ?? 0
    if (res.skipped_conflicts?.length) skippedConflicts.push(...res.skipped_conflicts)
    onProgress?.(Math.min(total, offset + chunk.length), total)
  }
  return {
    count,
    moved_count: movedCount,
    unchanged_count: unchangedCount,
    skipped_conflicts: skippedConflicts,
  }
}

export function retag(backend: string, overwrite: boolean) {
  return request<ImportJob>('/api/tag/jobs', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ backend, overwrite }),
  })
}

export function getJobs() {
  return request<{ items: ImportJob[]; queue: ImportQueue }>('/api/import/jobs')
}

export function retryJob(id: string, backend?: string) {
  const query = backend ? `?backend=${encodeURIComponent(backend)}` : ''
  return request<ImportJob>(`/api/import/jobs/${encodeURIComponent(id)}/retry${query}`, { method: 'POST' })
}

export function getRecord(id: string) {
  return request<CatalogItem>(`/api/records/${encodeURIComponent(id)}`)
}

export function updateRecordTags(item: CatalogItem, changes: Partial<FashionTags>, resetFields: string[]) {
  return request<CatalogItem>(`/api/records/${encodeURIComponent(item.id)}/tags`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ expected_revision: item.revision, changes, reset_fields: resetFields }),
  })
}

export function updateRecordCover(item: CatalogItem, crop: CoverCrop | null) {
  return request<CatalogItem>(`/api/records/${encodeURIComponent(item.id)}/cover`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ crop, expected_revision: item.revision }),
  })
}

export function getSimilarRecords(id: string, includePending = false, sourceCategory = '全部') {
  return request<{ count: number; items: CatalogItem[] }>(`/api/records/${encodeURIComponent(id)}/similar?include_pending=${includePending}&source_category=${encodeURIComponent(sourceCategory)}`)
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
  impact?: SalesImportImpact
  historical_start?: string; historical_end?: string
  token: string; kind: 'archive' | 'sales'; requested_kind: string; source_file: string
  format: string; format_label: string; row_count: number; style_codes: string[]
  counts: { new: number; updated: number; unchanged: number }
  meta: { warnings?: string[]; parsed_quantity?: number; parsed_amount?: number; sheet_quantity?: number | null; sheet_amount?: number | null; reconciled?: boolean | null; reconcile_note?: string; evidence_columns?: string[] }
  samples: { variant_code: string; style_code: string; customer: string; color?: string; size_label?: string; quantity?: number; amount?: number; season?: string; category?: string; subcategory?: string; design_code?: string; action: string }[]
  issues: { row: number; style_code: string; message: string }[]; issue_count: number; ignored_rows: number
  overlap_variants: string[]; missing_images: string[]; can_confirm: boolean
}
export type TableImportResult = TableReport & { id: string; kind: 'archive' | 'sales'; created_at: string; unchanged: number; issue_count: number; sales_policy: FactoryWorkspace['sales_policy'] }
export type ImportMetric = { quantity: number; amount: number }
export type ImportMetricChange = { before: ImportMetric; after: ImportMetric; delta: ImportMetric }
export type SalesImportImpact = {
  version: number; row_kind: 'orders' | 'summary'; scope: 'all_records_all_seasons'
  retained_rows: number; summaries_deactivated: number; summaries_reactivated: number
  orders: ImportMetricChange; counted: ImportMetricChange
}
export type InspectedFile = { name: string; kind: string; action: string; style_code: string; message: string; width?: number; height?: number }
export function getFactoryWorkspace(status = 'missing', q = '', page = 1, verifyRemote = true) {
  const qs = new URLSearchParams({ status, q, page: String(page) })
  if (!verifyRemote) qs.set('verify_remote', 'false')
  return request<FactoryWorkspace>(`/api/import/workspace?${qs}`)
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
export function inspectImport(files: { file: File; name: string; styleCode?: string }[], category: string) {
  const body = new FormData()
  const hasStyleCodes = files.some(item => item.styleCode !== undefined)
  files.forEach(item => {
    body.append('files', item.file, item.name)
    if (hasStyleCodes) body.append('style_codes', item.styleCode ?? '')
  })
  body.append('category', category)
  return request<{ items: InspectedFile[] }>('/api/import/inspect', { method: 'POST', body })
}
export function classifyBatch(backend: string, batch_id: string) {
  return request<ImportJob>('/api/tag/jobs', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ backend, batch_id, overwrite: false }) })
}

export type SalesRow = { id: string; kind: 'orders' | 'summary'; order_no: string; variant_code: string; style_code: string; customer: string; color: string; size_label: string; quantity: number; amount: number; source_file: string; order_date: string; year: string; period: string; updated_at: string }
export type SalesBatch = { name: string; protected: number; legacy: boolean }
export type SalesRows = { items: SalesRow[]; total: number; page: number; page_size: number; batch: SalesBatch | null }
export type SalesSelection = { scope: 'selected' | 'batch' | 'all'; batch_id: string; ids: string[] }
export type SalesDeletePreview = { token: string; row_count: number; order_rows: number; summary_rows: number; style_codes: string[]; before: { quantity: number; amount: number }; after: { quantity: number; amount: number }; reactivated_summary_rows: number; batch: SalesBatch | null }
export type SalesDeletionLog = { id: string; created_at: string; row_count: number; source_file: string; restored_at: string | null; restored_rows: number; skipped_rows: number }
export function getSalesData(batch_id = '', q = '', page = 1) {
  return request<SalesRows>(`/api/import/sales-data?${new URLSearchParams({ batch_id, q, page: String(page) })}`)
}
export function previewSalesDeletion(selection: SalesSelection) {
  return request<SalesDeletePreview>('/api/import/sales-data/delete-preview', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(selection) })
}
export function deleteSalesData(selection: SalesSelection, token: string) {
  return request<{ deleted: number; backup_id: string }>('/api/import/sales-data/delete', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ...selection, token }) })
}
export function getSalesDeletions(page = 1) { return request<SalesDeletionLog[]>(`/api/import/sales-deletions?page=${page}`) }

export type SalesRestorePreview = { token: string; backup_id: string; row_count: number; restore_count: number; skipped_existing: number; identical_rows: number; conflicting_rows: number; conflicts: { order_no: string; variant_code: string; color: string; size_label: string }[]; before: { quantity: number; amount: number }; after: { quantity: number; amount: number }; style_codes: string[]; suppressed_summary_rows: number }
export function previewSalesRestoration(id: string) {
  return request<SalesRestorePreview>(`/api/import/sales-deletions/${encodeURIComponent(id)}/restore-preview`, { method: 'POST' })
}
export function restoreSalesData(id: string, token: string) {
  return request<{ restored: number; skipped_existing: number; already_restored: boolean }>(`/api/import/sales-deletions/${encodeURIComponent(id)}/restore`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token }) })
}
