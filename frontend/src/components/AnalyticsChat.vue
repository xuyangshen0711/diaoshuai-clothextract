<script setup lang="ts">
import { computed, nextTick, ref } from 'vue'
import { sendAnalysisMessage, type QuestionAnswer, type QuestionContext } from '../analyticsApi'

defineProps<{ deepseekConfigured: boolean }>()
const busy = defineModel<boolean>('busy', { required: true })

const draft = ref('')
const error = ref('')
const transcript = ref<HTMLElement | null>(null)
const turns = ref<{ id: number; text: string; answer?: QuestionAnswer }[]>([])
const context = ref<QuestionContext>()
let nextId = 0

const examples = ['本厂补单最多的前三款', '市调拍照有多少款', '品牌资料包里有哪些品类？']
const followups = computed(() => {
  const last = turns.value[turns.value.length - 1]?.answer
  if (last?.kind !== 'answer' || !last.context) return []
  if ((last.context.ranked_styles?.length || 0) > 1) return ['第二款按尺码展开', '改成按金额排名']
  if (last.context.style_code) return ['这款按颜色展开', '这款按尺码展开', '改成全部款式，时间保持不变']
  if (last.context.source === '本厂产品') return ['按尺码展开', '补单最多的前三款']
  return ['这些资料有哪些品类？']
})
const choices = { order_date: '按下单日期', order_season: '按订单年度和季节', archive_season: '按订单年度和季节' }
const metricLabels = { quantity: '按订单件数排序', amount: '按金额排序', replenishment: '按补单件数排序' }
const number = new Intl.NumberFormat('zh-CN')
const money = new Intl.NumberFormat('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })

function newConversation() {
  if (busy.value) return
  turns.value = []
  context.value = undefined
  draft.value = ''
  error.value = ''
}

async function scrollToLatest() {
  await nextTick()
  if (transcript.value) transcript.value.scrollTop = transcript.value.scrollHeight
}

async function send(text = draft.value) {
  text = text.trim()
  if (!text || busy.value) return
  const history = turns.value.slice(-4).map(turn => turn.text)
  const id = nextId++
  busy.value = true
  error.value = ''
  draft.value = ''
  turns.value.push({ id, text })
  await scrollToLatest()
  try {
    const answer = await sendAnalysisMessage(text, history, context.value)
    const current = turns.value.find(turn => turn.id === id)
    if (current) current.answer = answer
    if (answer.context) context.value = answer.context
  } catch (reason) {
    turns.value = turns.value.filter(turn => turn.id !== id)
    if (!draft.value.trim()) draft.value = text
    error.value = reason instanceof Error ? reason.message : '发送失败，请重试。'
  } finally {
    busy.value = false
    await scrollToLatest()
  }
}

function onEnter(event: KeyboardEvent) {
  if (event.shiftKey || event.isComposing || event.keyCode === 229) return
  event.preventDefault()
  void send()
}
</script>

<template>
  <section class="analytics-question" aria-label="问数据">
    <header class="analytics-question-head">
      <div>
        <h2>款式与订单助手</h2>
        <p>{{ deepseekConfigured ? 'DeepSeek' : '本地问答' }} · 只读</p>
      </div>
      <button v-if="turns.length" type="button" class="analytics-quiet" :disabled="busy" @click="newConversation">新对话</button>
    </header>

    <div ref="transcript" class="analytics-conversation" role="log" aria-label="对话记录">
      <div v-if="!turns.length" class="analytics-chat-welcome">
        <h3>今天想查什么？</h3>
        <p>可以直接问款式、订单和补单，也可以接着追问。</p>
        <div class="analytics-examples">
          <button v-for="example in examples" :key="example" type="button" @click="send(example)">{{ example }}</button>
        </div>
      </div>

      <article v-for="(turn, index) in turns" :key="turn.id" class="analytics-turn">
        <div class="analytics-user-message">
          <span class="visually-hidden">你</span>
          <p>{{ turn.text }}</p>
        </div>
        <div class="analytics-answer">
          <span class="analytics-assistant-label">款式助手</span>
          <template v-if="turn.answer">
            <p>{{ turn.answer.message }}</p>
            <p v-if="turn.answer.notice" class="analytics-notice" role="status">{{ turn.answer.notice }}</p>
            <button v-if="turn.answer.retryable && index === turns.length - 1" type="button" class="analytics-quiet" :disabled="busy" @click="send(turn.text)">处理后重试这条</button>
            <details v-if="turn.answer.scope" class="analytics-answer-details" open>
              <summary>统计口径</summary>
              <div class="analytics-answer-scope">
                <span>{{ turn.answer.scope.source === '全部' ? '全部资料' : turn.answer.scope.source }}</span>
                <span v-if="turn.answer.scope.start">{{ turn.answer.scope.start }} 至 {{ turn.answer.scope.end }}</span>
                <span v-if="turn.answer.scope.categories?.length">{{ turn.answer.scope.categories.join('、') }}</span>
                <span v-if="turn.answer.scope.season">订单季节 {{ turn.answer.scope.season }}</span>
                <span v-if="turn.answer.scope.style_code">款号 {{ turn.answer.scope.style_code }}</span>
                <span v-if="turn.answer.scope.color">颜色 {{ turn.answer.scope.color }}</span>
                <span v-if="turn.answer.scope.size">尺码 {{ turn.answer.scope.size }}</span>
                <span v-if="turn.answer.scope.order_kind">{{ turn.answer.scope.order_kind }}</span>
                <span v-for="tag in turn.answer.scope.tags" :key="tag">{{ tag }}</span>
                <span v-if="turn.answer.metric && turn.answer.rows?.length && turn.answer.heading !== '期间'">{{ turn.answer.heading === '尺码' ? '完整尺码分布 · 按尺码顺序' : metricLabels[turn.answer.metric] }}</span>
              </div>
            </details>
            <div v-if="turn.answer.rows?.length" class="analytics-table-scroll">
              <table class="analytics-table">
                <thead><tr><th scope="col">{{ turn.answer.heading }}</th><th scope="col">件数</th><th scope="col">金额（元）</th><th scope="col">补单件数</th></tr></thead>
                <tbody>
                  <tr v-for="row in turn.answer.rows" :key="row.name">
                    <th scope="row">{{ row.name }}</th>
                    <td>{{ number.format(row.quantity) }}</td><td>{{ money.format(row.amount) }}</td><td>{{ number.format(row.replenishment) }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
            <dl v-if="turn.answer.counts?.length" class="analytics-counts">
              <div v-for="item in turn.answer.counts" :key="item.name"><dt>{{ item.name }}</dt><dd>{{ number.format(item.count) }}</dd></div>
            </dl>
            <ul v-if="turn.answer.products?.length" class="analytics-matches">
              <li v-for="item in turn.answer.products" :key="item.record_id || item.style_code">
                <RouterLink v-if="item.record_id" :to="{ name: 'record', params: { id: item.record_id } }">{{ item.style_code || item.categories[0] || '查看款式' }}</RouterLink>
                <strong v-else>{{ item.style_code }} <small>未关联图片</small></strong>
                <span>{{ [item.source, item.categories.join('、'), item.colors.join('、'), item.seasons.join('、')].filter(Boolean).join(' · ') }}</span>
              </li>
            </ul>
            <div v-if="turn.answer.choices?.length && index === turns.length - 1" class="analytics-presets">
              <button v-for="choice in turn.answer.choices" :key="choice" type="button" :disabled="busy" @click="send(choices[choice])">{{ choices[choice] }}</button>
            </div>
          </template>
          <p v-else class="analytics-note" role="status">正在理解你的问题…</p>
        </div>
      </article>
    </div>

    <p v-if="error" class="analytics-error" role="alert">{{ error }}</p>
    <div v-if="followups.length && !busy" class="analytics-followups">
      <span>可以接着问</span>
      <div class="analytics-presets"><button v-for="text in followups" :key="text" type="button" @click="send(text)">{{ text }}</button></div>
    </div>
    <form class="analytics-question-form" @submit.prevent="send()">
      <label for="analytics-question" class="visually-hidden">给款式助手发消息</label>
      <textarea id="analytics-question" v-model="draft" maxlength="300" rows="2" placeholder="给款式助手发消息…" @keydown.enter="onEnter" />
      <button type="submit" class="analytics-primary" :disabled="busy || !draft.trim()">发送</button>
    </form>
    <p class="analytics-chat-hint">支持连续追问，换话题可点「新对话」。对话仅保留在当前页面，订单数值由数据库计算。Enter 发送，Shift + Enter 换行。</p>
    <p v-if="!deepseekConfigured" class="analytics-chat-hint">尚未配置 DeepSeek，当前仅支持本地规则查询；复杂问题请拆成款式、颜色或尺码逐步询问。</p>
  </section>
</template>
