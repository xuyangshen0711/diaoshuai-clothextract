<script setup lang="ts">
import { computed } from 'vue'
import { checkingProviders, preferredProvider, providerLabel, refreshServiceStatus, serviceStatus,
  serviceStatusError, serviceStatusLoading, testService } from '../composables/serviceStatus'

const props = defineProps<{ backend: string }>()
const selection = computed(() => serviceStatusError.value ? '当前无法读取 API 状态，请刷新重试。'
  : !serviceStatus.value ? '正在获取分类服务状态。'
    : props.backend === 'mock' ? '本次导入使用模拟分类，仅用于测试。'
  : props.backend === 'deferred' ? '原图先保存在任务中，配置 AI 并完成分类后才自动进入图库。'
    : `本次图片导入使用 ${props.backend === 'deepseek' ? 'DeepSeek' : 'OpenAI'} 自动分类。`)
function checkedTime(value: string) {
  return new Date(value).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })
}
</script>

<template>
  <section id="api-status" class="api-status-panel" aria-labelledby="api-status-title">
    <header class="api-status-heading">
      <div><h3 id="api-status-title">分类 API 状态</h3><p>{{ selection }}</p></div>
      <div class="api-status-tools">
        <span v-if="serviceStatus && !serviceStatusError" class="backend-connected"><i aria-hidden="true"></i>网站后端已连接</span>
        <button type="button" class="text-button" :disabled="serviceStatusLoading" @click="refreshServiceStatus">{{ serviceStatusLoading ? '正在刷新…' : '刷新状态' }}</button>
      </div>
    </header>
    <p v-if="serviceStatusError" class="api-status-error" role="alert">{{ serviceStatusError }}</p>
    <p v-if="!serviceStatus && serviceStatusLoading" class="api-status-loading" role="status">正在读取 API 配置…</p>
    <div v-if="serviceStatus" class="api-provider-list" :aria-busy="serviceStatusLoading">
      <article v-for="provider in serviceStatus.providers" :key="provider.provider" class="api-provider-row">
        <div class="api-provider-name"><strong>{{ provider.name }}</strong><span v-if="provider.provider === preferredProvider?.provider" class="api-default-tag">自动首选</span><small>模型：{{ provider.model }}</small></div>
        <div class="api-provider-state" :class="provider.status" aria-live="polite">
          <span class="api-state-label"><i aria-hidden="true"></i>{{ providerLabel(provider) }}</span>
          <small>{{ serviceStatusError ? '网站连接异常，恢复后请重新检测。' : provider.message }}</small>
          <small v-if="provider.checked_at" class="api-checked-time">最近检测 {{ checkedTime(provider.checked_at) }}<template v-if="provider.latency_ms !== null"> · {{ provider.latency_ms.toLocaleString() }} ms</template></small>
        </div>
        <button type="button" class="btn small api-test-button" :disabled="!provider.configured || !!serviceStatusError || checkingProviders.includes(provider.provider) || provider.status === 'checking'" @click="testService(provider.provider)">{{ checkingProviders.includes(provider.provider) || provider.status === 'checking' ? '检测中…' : provider.checked_at ? '重新检测' : '测试连接' }}</button>
      </article>
    </div>
    <p class="api-status-note">“已配置”仅表示密钥已填写。测试连接会读取服务商模型列表，确认认证与网络；余额和图片分类能力以实际任务结果为准。检测结果保留 5 分钟。</p>
  </section>
</template>

<style scoped>
.api-status-panel { margin: 0 0 25px; padding: 22px 26px 16px; border: 1px solid var(--line); border-radius: 7px; background: var(--panel); scroll-margin-top: 24px; }
.api-status-heading { display: flex; justify-content: space-between; align-items: flex-start; gap: 20px; }
.api-status-heading h3 { margin: 0; font-size: 17px; }
.api-status-heading p { margin: 5px 0 0; color: var(--muted); font-size: 13px; }
.api-status-tools { display: flex; flex-wrap: wrap; justify-content: flex-end; align-items: center; gap: 18px; padding-top: 2px; }
.backend-connected { display: inline-flex; align-items: center; gap: 7px; color: #41624a; font-size: 12px; white-space: nowrap; }
.backend-connected i, .api-state-label i { width: 7px; height: 7px; flex: 0 0 7px; border-radius: 50%; background: currentColor; }
.api-provider-list { margin-top: 19px; border-top: 1px solid var(--line); }
.api-provider-row { display: grid; grid-template-columns: minmax(170px, .8fr) minmax(220px, 1.65fr) auto; align-items: center; gap: 24px; padding: 17px 0; border-bottom: 1px solid var(--divider); }
.api-provider-row:last-child { border-bottom: 0; padding-bottom: 12px; }
.api-provider-name strong { font-size: 15px; font-weight: 600; }
.api-provider-name small { display: block; margin-top: 4px; color: var(--muted); font-size: 12px; overflow-wrap: anywhere; }
.api-default-tag { margin-left: 9px; padding: 2px 6px; border-radius: 3px; background: #f1ebdf; color: #665236; font-size: 11px; vertical-align: 1px; }
.api-provider-state { min-width: 0; color: var(--muted); }
.api-state-label { display: inline-flex; align-items: center; gap: 7px; font-size: 13px; font-weight: 500; }
.api-provider-state small { display: block; margin-top: 4px; color: var(--muted); font-size: 12px; line-height: 1.7; overflow-wrap: anywhere; }
.api-provider-state.connected .api-state-label { color: #41624a; }
.api-provider-state.error .api-state-label, .api-status-error { color: #9a403a; }
.api-provider-state.unchecked .api-state-label, .api-provider-state.placeholder .api-state-label, .api-provider-state.unsupported .api-state-label { color: #80602f; }
.api-provider-state .api-checked-time { font-size: 11px; }
.api-test-button { white-space: nowrap; min-width: 92px; }
.import-workspace .api-status-note { margin: 10px 0 0; max-width: none; color: var(--muted); font-size: 12px; line-height: 1.8; }
.api-status-error, .api-status-loading { font-size: 13px; }
@media (max-width: 700px) {
  .api-status-panel { padding: 20px 18px 15px; }
  .api-status-heading { flex-direction: column; gap: 12px; }
  .api-status-tools { width: 100%; justify-content: space-between; }
  .api-provider-row { grid-template-columns: 1fr auto; gap: 12px; }
  .api-provider-name { grid-column: 1 / -1; }
  .api-provider-state { align-self: start; }
}
</style>
