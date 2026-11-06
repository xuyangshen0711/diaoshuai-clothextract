export function readEntry(entry: FileSystemEntry): Promise<File[]> {
  return new Promise(resolve => {
    if (entry.isFile) {
      (entry as FileSystemFileEntry).file(file => {
        const path = entry.fullPath.replace(/^\//, '') || file.name
        Object.defineProperty(file, 'webkitRelativePath', { value: path })
        resolve([file])
      }, () => resolve([]))
      return
    }
    if (!entry.isDirectory) { resolve([]); return }
    const reader = (entry as FileSystemDirectoryEntry).createReader()
    const children: FileSystemEntry[] = []
    const readBatch = () => reader.readEntries(async batch => {
      if (!batch.length) {
        const nested: File[] = []
        for (const child of children) nested.push(...await readEntry(child))
        resolve(nested)
        return
      }
      children.push(...batch)
      readBatch()
    }, () => resolve([]))
    readBatch()
  })
}
