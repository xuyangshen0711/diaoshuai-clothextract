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

export type CatalogItem = {
  id: string
  image_path: string
  image_url: string
  score?: number
  tags: FashionTags
  tags_source: string
  manually_edited: boolean
  source: {
    source_file: string
    source_type: string
    location: string
  }
  row: Record<string, unknown>
}

export type FilterMeta = Record<string, string[]>

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init)
  if (!res.ok) {
    let detail = res.statusText
    try {
      const body = await res.json()
      detail = body.detail || JSON.stringify(body)
    } catch {
      /* ignore */
    }
    throw new Error(detail)
  }
  return res.json() as Promise<T>
}

export function getHealth() {
  return request<{ ok: boolean; openai_ready: boolean; catalog_count: number }>('/api/health')
}

export function getFilters() {
  return request<FilterMeta>('/api/meta/filters')
}

export function getCatalog(params: Record<string, string>) {
  const qs = new URLSearchParams()
  Object.entries(params).forEach(([k, v]) => {
    if (v && v !== '全部') qs.set(k, v)
  })
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

export async function importFiles(files: File[], backend: string) {
  const body = new FormData()
  files.forEach((f) => body.append('files', f))
  return request<{ extracted: number; catalog_count: number; backend: string }>(
    `/api/import?backend=${encodeURIComponent(backend)}`,
    { method: 'POST', body },
  )
}

export function retag(backend: string, overwrite: boolean) {
  return request<{ catalog_count: number }>('/api/tag', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ backend, overwrite }),
  })
}

export function updateRecord(id: string, tags: FashionTags) {
  return request<CatalogItem>(`/api/records/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ tags, mark_manual: true }),
  })
}

export function rebuildIndex() {
  return request<{ ok: boolean; catalog_count: number }>('/api/index/rebuild', { method: 'POST' })
}
