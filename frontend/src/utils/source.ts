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

const SWATCHES: Record<string, string> = {
  红: '#d84c4c', 红色: '#d84c4c', 橙: '#e08a3c', 黄: '#e2b43a', 绿: '#3f8f5b',
  蓝: '#3f6fbe', 蓝色: '#3f6fbe', 紫: '#7b4ea8', 粉: '#e08aaa', 棕咖: '#8d5e3c',
  米白: '#eadcc8', 米色: '#eadcc8', 灰: '#a3a3a3', 灰色: '#a3a3a3',
  黑: '#2a2a2a', 黑色: '#2a2a2a', 白: '#f7f7f7', 白色: '#f7f7f7', 其他: '#d5d0c8',
}

export function swatchColor(name: string): string {
  return SWATCHES[name] || '#d5d0c8'
}

export function swatchLight(name: string): boolean {
  return ['白', '白色', '米白', '米色', '其他'].includes(name)
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
