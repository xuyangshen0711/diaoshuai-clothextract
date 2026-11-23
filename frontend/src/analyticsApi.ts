import { request, requestResponse } from './api'

export type AnalysisSession = {
  configured: boolean
  authenticated: boolean
  deepseek_configured: boolean
  unencrypted_connection?: boolean
}
export type AnalysisOptions = {
  categories: string[]
  seasons: string[]
  first_order_date: string | null
  last_order_date: string | null
  sources: string[]
}
export type AnalysisFilters = { start: string; end: string; categories: string[]; season: string | null }
export type Metrics = { quantity: number; amount: number; styles: number; replenishment: number }
export type RankedRow = Metrics & { name: string; categories?: string; record_id?: string | null }
export type AnalysisReport = {
  filters: AnalysisFilters
  totals: Metrics
  categories: RankedRow[]
  ranking: RankedRow[]
  trend: { unit: string; items: RankedRow[] }
  focus: {
    style_code: string
    sizes: string[]
    rows: { color: string; quantities: number[]; total: number }[]
    totals: number[]
    total: number
  } | null
  coverage: { summary_styles: number; undated_lines: number }
  observations: string[]
  emphasis: 'category' | 'style' | 'replenishment' | null
  provider: 'deepseek' | 'rules'
  snapshot: string
  notice?: string
}
export type QuestionChoice = 'order_date' | 'order_season' | 'archive_season'
export type QuestionContext = {
  intent: string
  source: string
  filters: AnalysisFilters
  style_code: string
  metric: 'quantity' | 'amount' | 'replenishment'
  color?: string
  size?: string
  order_kind?: 'all' | 'regular' | 'replenishment'
  catalog_category?: string
  catalog_season?: string
  catalog_tags?: string[]
  ranked_styles?: string[]
}
export type QuestionAnswer = {
  kind: 'answer' | 'clarify' | 'outside'
  message: string
  notice?: string
  retryable?: boolean
  choices?: QuestionChoice[]
  provider?: 'deepseek' | 'rules'
  metric?: 'quantity' | 'amount' | 'replenishment'
  scope?: {
    source: string; start?: string; end?: string; categories?: string[]; season?: string
    style_code?: string; color?: string; size?: string; order_kind?: string; tags?: string[]
  }
  context?: QuestionContext
  heading?: string
  rows?: RankedRow[]
  counts?: { name: string; count: number }[]
  products?: {
    record_id: string | null; style_code: string; source: string; categories: string[]
    colors: string[]; seasons: string[]; tags: string[]; variants: number
  }[]
}

export type YarnTimeMode = 'period' | 'date'
export type YarnCompareMode = 'none' | 'previous_period' | 'custom_period' | 'custom_date'

export type YarnPeriodOption = {
  key: string
  label: string
  start: string
  end: string
  has_orders: boolean
}

export type YarnFilterOptions = {
  seasons: string[]
  periods: YarnPeriodOption[]
  first_order_date: string | null
  last_order_date: string | null
  dated_lines: number
  undated_lines: number
}

export type YarnReportFilters = {
  seasons: string[]
  time_mode: YarnTimeMode
  period_start: string
  period_end: string
  start_date: string | null
  end_date: string | null
  compare_mode: YarnCompareMode
  compare_period_start: string
  compare_period_end: string
  compare_start_date: string | null
  compare_end_date: string | null
}

export type YarnMissingWarning = {
  code: string
  severity: 'high' | 'medium'
  title: string
  detail: string
  style_count: number
  brand_style_count: number
  line_count: number
  quantity: number
  sample_codes: string[]
}

export type YarnMissingSummary = {
  has_warnings: boolean
  warning_count: number
  warnings: YarnMissingWarning[]
}

export type YarnScopeMeta = {
  seasons: string[]
  season_label: string
  time_mode: YarnTimeMode
  window_label: string
  effective_start_date: string | null
  effective_end_date: string
  end_period_key: string
  end_period_label: string
  compare_mode: YarnCompareMode
  compare_label: string
  order_line_count: number
}

export type YarnMaterialRow = {
  rank: number
  material: string
  is_unmatched: boolean
  is_main: boolean
  style_count: number
  brand_style_count: number
  customer_count: number
  customer_share: number
  quantity: number
  amount: number
  quantity_share: number
  avg_per_style: number
  compare_quantity: number | null
  growth_quantity: number | null
  growth_rate: number | null
}

export type YarnMaterialTotals = Omit<YarnMaterialRow, 'rank' | 'material' | 'is_unmatched' | 'is_main'>

export type YarnColorRow = {
  rank: number
  material: string
  color_group: string
  style_count: number
  brand_style_count: number
  customer_count: number
  quantity: number
  amount: number
  share: number
  compare_quantity: number | null
  growth_quantity: number | null
  growth_rate: number | null
}

export type YarnColorBlock = {
  material: string
  is_main: boolean
  is_unmatched: boolean
  rows: YarnColorRow[]
  totals: Omit<YarnColorRow, 'rank' | 'material' | 'color_group'>
}

export type YarnReportData = {
  scope: YarnScopeMeta
  main_materials: string[]
  materials_summary: YarnMaterialRow[]
  materials_totals: YarnMaterialTotals
  color_blocks: YarnColorBlock[]
  snapshot: string
}

export type YarnPreviewResponse = {
  requires_confirmation: boolean
  filters: YarnReportFilters
  scope: YarnScopeMeta
  missing: YarnMissingSummary
  report: YarnReportData | null
}

const base = '/api/analytics'
function jsonPost(body: object = {}): RequestInit {
  return { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }
}

export const analysisSession = () => request<AnalysisSession>(base + '/session')
export const analysisLogin = (password: string) => request(base + '/login', jsonPost({ password }))
export const analysisLogout = () => request(base + '/logout', jsonPost())
export const analysisOptions = () => request<AnalysisOptions>(base + '/options')
export const previewReport = (filters: AnalysisFilters, useDeepseek: boolean) =>
  request<AnalysisReport>(base + '/report', jsonPost({ filters, use_deepseek: useDeepseek }))
export async function exportReport(report: AnalysisReport) {
  const { filters, snapshot, emphasis } = report
  const response = await requestResponse(base + '/report/pdf', jsonPost({ filters, snapshot, emphasis }))
  return response.blob()
}
export const sendAnalysisMessage = (question: string, history: string[], context?: QuestionContext) =>
  request<QuestionAnswer>(base + '/chat', jsonPost({ question, history, context }))

export const yarnTableOptions = () => request<YarnFilterOptions>(base + '/yarn-table/options')
export const previewYarnTable = (filters: YarnReportFilters, acknowledgeMissing = false) =>
  request<YarnPreviewResponse>(
    base + '/yarn-table/preview',
    jsonPost({ filters, acknowledge_missing: acknowledgeMissing }),
  )
export async function exportYarnTable(
  filters: YarnReportFilters,
  snapshot: string,
  acknowledgeMissing: boolean,
  selectedMaterials: string[],
) {
  const response = await requestResponse(
    base + '/yarn-table/xlsx',
    jsonPost({
      filters,
      snapshot,
      acknowledge_missing: acknowledgeMissing,
      selected_materials: selectedMaterials,
    }),
  )
  return response.blob()
}

// ---------------------------------------------------------------- 季度款式分析
export type SeasonGrade = 'S' | 'A' | 'B' | 'C' | 'D' | '无产量'
export type SeasonExportScope = 'all' | 'tracking' | 'review'
export type SeasonFilters = { seasons: string[]; cutoff: string | null; compare: string | null; include_brand: boolean }
export type SeasonOptions = {
  seasons: { season: string; styles: number; orders: number }[]
  default_seasons: string[]
  last_order_date: string | null
  sources: { production_rows: number; guide_rows: number; stock_rows: number }
}
export type SeasonStyle = { code: string; quantity: number; amount: number; image_url: string | null; grade: SeasonGrade }
export type SeasonHeadline = {
  sku: number; ordered: number; produced: number; unproduced: number
  sell_through: number; production_ratio: number; invalid_ratio: number
}
export type SeasonGroupRow = SeasonHeadline & {
  name: string; key?: string; skc: number; sku_share: number; skc_share: number
  quantity: number; quantity_share: number; amount: number; amount_share: number; unit_price: number
}
export type SeasonShareRow = { name: string; sku: number; sku_share: number; quantity_share: number; amount_share: number }
export type SeasonCheck = { level: 'high' | 'medium' | 'info'; title: string; detail: string; count: number; codes: string[] }
export type SeasonPerson = {
  name: string; designed: number; showroom: number; showroom_ratio: number | null
  ordered: number; sell_through: number | null; produced: number; production_ratio: number | null
}
export type SeasonReport = {
  scope: { seasons: string[]; include_brand: boolean; cutoff: string | null; compare: string | null; first_order_date: string | null; last_order_date: string | null }
  checks: SeasonCheck[]
  overview: SeasonHeadline & {
    skc: number; quantity: number; amount: number; grade_counts: Record<SeasonGrade, number>
    market_sku: number; brand_sku: number; excluded_brand_sku: number; image_styles: number
  }
  materials: { rows: SeasonGroupRow[]; totals: SeasonGroupRow }
  categories: { rows: SeasonGroupRow[]; totals: SeasonGroupRow }
  walls: {
    category: { name: string; headline: SeasonHeadline; rows: { material: string; sku: number; grades: Record<SeasonGrade, SeasonStyle[]> }[]; shares: SeasonShareRow[] }[]
    material: { name: string; headline: SeasonHeadline; grades: Record<SeasonGrade, SeasonStyle[]>; share: Omit<SeasonShareRow, 'name'> }[]
  }
  people: { designer: SeasonPerson[]; craftsman: SeasonPerson[] }
  grades: { name: string; min: number }[]
  snapshot: string
}

export const seasonOptions = () => request<SeasonOptions>(base + '/season/options')
export const previewSeason = (filters: SeasonFilters) => request<SeasonReport>(base + '/season/preview', jsonPost({ filters }))
export async function exportSeason(filters: SeasonFilters, snapshot: string, export_scope: SeasonExportScope = 'all') {
  const response = await requestResponse(base + '/season/xlsx', jsonPost({ filters, snapshot, export_scope }))
  return { blob: await response.blob(), images: Number(response.headers.get('X-Season-Images') || 0) }
}
