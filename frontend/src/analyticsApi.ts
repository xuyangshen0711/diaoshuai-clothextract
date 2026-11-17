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
