import { computed, ref } from 'vue'
import { checkAiService, getAiServiceStatus, type AiProvider, type AiService, type AiServiceStatus } from '../api'

export const serviceStatus = ref<AiServiceStatus | null>(null)
export const serviceStatusError = ref('')
export const serviceStatusLoading = ref(false)
export const checkingProviders = ref<AiProvider[]>([])
let pendingRefresh: Promise<void> | null = null
let revision = 0

export const configuredProviders = computed(() => serviceStatus.value?.providers.filter(p => p.configured) || [])
export const preferredProvider = computed(() => configuredProviders.value.find(p => p.provider === serviceStatus.value?.default_backend) || configuredProviders.value[0])
export const serviceSummary = computed(() => {
  if (serviceStatusError.value) return { label: 'API 状态读取失败', tone: 'error' }
  if (!serviceStatus.value) return { label: 'API 状态检测中', tone: 'quiet' }
  const provider = preferredProvider.value
  if (!provider) return { label: '分类 API 未配置', tone: 'quiet' }
  if (checkingProviders.value.includes(provider.provider)) return { label: `${provider.name} 检测中`, tone: 'pending' }
  if (provider.status === 'connected') return { label: `${provider.name} 连接成功`, tone: 'connected' }
  if (provider.status === 'error') return { label: `${provider.name} 连接失败`, tone: 'error' }
  return { label: `${provider.name} 已配置`, tone: 'pending' }
})

export function refreshServiceStatus() {
  if (pendingRefresh) return pendingRefresh
  const requestedAt = revision
  serviceStatusLoading.value = true
  pendingRefresh = (async () => {
    try {
      const result = await getAiServiceStatus()
      if (revision === requestedAt) serviceStatus.value = result
      serviceStatusError.value = ''
    } catch {
      serviceStatus.value = null
      serviceStatusError.value = '无法读取网站后端的 API 状态，请检查服务或网络后重试。'
    } finally {
      serviceStatusLoading.value = false
      pendingRefresh = null
    }
  })()
  return pendingRefresh
}

export async function testService(provider: AiProvider) {
  if (checkingProviders.value.includes(provider)) return
  checkingProviders.value = [...checkingProviders.value, provider]
  try {
    const result = await checkAiService(provider)
    revision++
    if (serviceStatus.value) {
      serviceStatus.value = { ...serviceStatus.value, providers: serviceStatus.value.providers.map(p => p.provider === provider ? result : p) }
    }
    serviceStatusError.value = ''
  } catch {
    serviceStatusError.value = '检测请求未完成，请检查网站后端连接后重试。'
  } finally {
    checkingProviders.value = checkingProviders.value.filter(p => p !== provider)
  }
}

export function providerLabel(provider: AiService) {
  if (checkingProviders.value.includes(provider.provider)) return '正在检测'
  return { connected: '连接成功', error: '连接失败', unchecked: '已配置 · 未检测',
    not_configured: '未配置', placeholder: '待配置', checking: '正在检测', unsupported: '暂无法验证' }[provider.status]
}

export function startServiceStatusPolling() {
  const refresh = () => { if (document.visibilityState !== 'hidden' && !checkingProviders.value.length) void refreshServiceStatus() }
  refresh()
  const timer = window.setInterval(refresh, 60000)
  window.addEventListener('focus', refresh)
  return () => { window.clearInterval(timer); window.removeEventListener('focus', refresh) }
}
