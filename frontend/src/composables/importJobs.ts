import { computed, ref } from 'vue'
import { getJobs, type ImportJob } from '../api'

export const jobs = ref<ImportJob[]>([])
export const jobsError = ref('')
export const jobNotice = ref('')
export const activeJobCount = computed(() => jobs.value.filter(isActive).length)
export function isActive(job: ImportJob) {
  return ['queued', 'extracting', 'tagging'].includes(job.status)
}
export function rememberJob(job: ImportJob) {
  jobs.value = [job, ...jobs.value.filter(item => item.id !== job.id)]
}
let latest = 0
export async function refreshJobs() {
  const sequence = ++latest
  try {
    const data = await getJobs()
    if (sequence !== latest) return
    const previous = new Map(jobs.value.map(job => [job.id, job]))
    for (const job of data.items) {
      const old = previous.get(job.id)
      if (old && isActive(old) && !isActive(job)) {
        jobNotice.value = `${job.name}：${job.message}（成功 ${job.succeeded} 张）`
      }
    }
    jobs.value = data.items
    jobsError.value = ''
  } catch {
    if (sequence === latest) jobsError.value = '无法读取任务进度，请检查服务后刷新；已提交任务不会因关闭页面而取消。'
  }
}
export function startJobPolling() {
  refreshJobs()
  const timer = window.setInterval(() => { if (!document.hidden) refreshJobs() }, 2500)
  const visible = () => { if (!document.hidden) refreshJobs() }
  document.addEventListener('visibilitychange', visible)
  return () => { clearInterval(timer); document.removeEventListener('visibilitychange', visible) }
}
