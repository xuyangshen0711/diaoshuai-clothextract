import type { CatalogItem } from '../api'

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
