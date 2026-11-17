import type { CatalogItem } from '../api'

// Sort the combined sizes across customer versions, not each version separately.
export function orderedSizeLabels(labels: string[]): string[] {
  const rank = (label: string): [number, number] => {
    const value = label.normalize('NFKC').replace(/\s+/g, '').toUpperCase()
    const numeric = value.match(/^(\d+)(?:\/|$)/)
    if (numeric) return [0, Number(numeric[1])]
    if (/^X*S$/.test(value)) return [1, -value.length]
    const small = value.match(/^(\d+)XS$/)
    if (small) return [1, -Number(small[1]) - 1]
    if (value === 'M') return [1, 0]
    const large = value.match(/^(?:(\d+)X|(X*))L$/)
    if (large) return [1, (large[1] ? Number(large[1]) : large[2]!.length) + 1]
    return [2, 0]
  }
  return [...new Set(labels)].sort((a, b) => {
    const left = rank(a), right = rank(b)
    return left[0] - right[0] || left[1] - right[1] || a.localeCompare(b, 'zh-CN', { numeric: true })
  })
}

export function knownAttribute(value: string | undefined): string {
  const text = (value || '').trim()
  return ['', '未提供', '未判断', '待归类', '未分类', '未知', '不确定', '其他'].includes(text) ? '' : text
}

export function recordCategory(item: CatalogItem): string {
  return (knownAttribute(item.library_category) || knownAttribute(item.display_tags.品类)).replace(/\s*[>＞]\s*/g, ' / ')
}

export function hasUpperBody(item: CatalogItem): boolean {
  return !/^(下装|裤装|半身裙|裙装)(\s|\/|$)/.test(recordCategory(item))
}

export function recordSleeve(item: CatalogItem): string {
  if (Object.hasOwn(item.tag_overrides || {}, '袖长')) return knownAttribute(item.display_tags.袖长)
  return knownAttribute(item.sleeve_length || item.display_tags.袖长) ||
    item.display_tags.袖型.match(/长袖|短袖|七分袖|五分袖|九分袖|无袖/)?.[0] || ''
}

export function referenceTitle(item: CatalogItem): string {
  const category = recordCategory(item).split(' / ').at(-1) || '款式详情'
  const color = knownAttribute(item.display_tags.颜色主色调)
  const sleeve = hasUpperBody(item) ? recordSleeve(item) : ''
  const collar = hasUpperBody(item) ? knownAttribute(item.display_tags.领型) : ''
  const parts = [color, sleeve, collar === '无领' ? '' : collar]
    .filter((value, index, values) => value && !category.includes(value) && values.indexOf(value) === index)
  return [...parts, category].join('')
}

export function tagStatus(item: CatalogItem): string {
  if (item.review_status === 'rejected') return '已隐藏'
  if (item.entry_ready === false) return '待识别'
  if (Object.keys(item.tag_overrides || {}).length || item.manually_edited) return '人工已修改'
  if (item.ai_tags_source === 'mock') return '模拟分类'
  return 'AI 已识别'
}
