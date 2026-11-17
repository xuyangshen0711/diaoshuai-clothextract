import { fileSizeLabel } from './source'

export const MB = 1024 * 1024
export const MAX_BATCH_BYTES = 2048 * MB
export const MAX_BATCH_FILES = 1000
export function fileLimit(name: string) {
  return /\.(png|jpe?g|webp|gif|bmp|tiff?)$/i.test(name) ? 100 * MB : 512 * MB
}
export function fileProblem(name: string, size: number) {
  if (!size) return '文件为空，请重新导出后选择。'
  const limit = fileLimit(name)
  if (size <= limit) return ''
  return limit === 100 * MB
    ? `图片为 ${fileSizeLabel(size)}，超过 100 MB 上限。请压缩图片或移除此项。`
    : `文件为 ${fileSizeLabel(size)}，超过 512 MB 上限。请压缩文档中的图片、拆分文档，或移除此项。`
}
export function batchProblem(count: number, bytes: number) {
  if (count > MAX_BATCH_FILES) return `本批有 ${count} 个文件，超过 1000 个上限。请减少本批文件，分批上传。`
  if (bytes > MAX_BATCH_BYTES) return `本批共 ${fileSizeLabel(bytes)}，超过 2 GB 上限。请减少本批文件，分批上传。`
  return ''
}
