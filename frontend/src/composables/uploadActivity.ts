import { onMounted, onUnmounted, ref } from 'vue'
import { getUploadActivity, setUploadActivity } from '../api'

const ownUploads = new Set<string>()
export const otherUploadCount = ref<number | null>(null)
export function trackUpload(id: string) {
  ownUploads.add(id)
  let pending: Promise<unknown> | null = null
  const beat = () => {
    if (pending) return
    pending = setUploadActivity(id, true).catch(() => {}).finally(() => { pending = null })
  }
  beat()
  const timer = window.setInterval(beat, 20000)
  return () => {
    clearInterval(timer)
    // Finish after any outstanding heartbeat, so it cannot revive a finished upload.
    void (pending || Promise.resolve()).then(() => setUploadActivity(id, false)).catch(() => {}).finally(() => ownUploads.delete(id))
  }
}
export function useUploadActivity() {
  let timer: ReturnType<typeof setInterval> | undefined
  let stopped = false
  let loading = false
  const refresh = async () => {
    if (loading || document.hidden) return
    loading = true
    const own = [...ownUploads].sort().join(',')
    try {
      const data = await getUploadActivity([...ownUploads])
      if (!stopped && own === [...ownUploads].sort().join(',')) otherUploadCount.value = data.active_uploads
    } catch { if (!stopped) otherUploadCount.value = null }
    finally { loading = false }
  }
  onMounted(() => { void refresh(); timer = setInterval(refresh, 5000); document.addEventListener('visibilitychange', refresh) })
  onUnmounted(() => { stopped = true; clearInterval(timer); otherUploadCount.value = null; document.removeEventListener('visibilitychange', refresh) })
}
