import type { CatalogItem } from '../api'

export function sourceType(source: CatalogItem['source']): string {
  return ({ pdf: 'PDF', xlsx: 'Excel', xlsm: 'Excel', xls: 'Excel', pptx: 'PPT', ppt: 'PPT',
    image: '图片', upload: '图片', sample: '演示' } as Record<string, string>)[source.source_type] || source.source_type
}

export function importTime(source: CatalogItem['source']): string {
  return source.uploaded_at || source.extracted_at
}

export function visibleDescription(text: string | null | undefined, status: string): string {
  const value = text || ''
  if (status !== 'approved') return value
  return value.replaceAll('（待人工确认）', '').replaceAll('(待人工确认)', '').trim()
}

export function fileSizeLabel(bytes: number): string {
  if (bytes < 1024 * 102.4) return `${Math.round(bytes / 1024)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

export function formatTime(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '时间未记录' : new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(date)
}
