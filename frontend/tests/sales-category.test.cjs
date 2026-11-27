const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { join } = require('node:path')
const { test } = require('node:test')
const { compileFunction } = require('node:vm')
const { createSSRApp } = require('vue')
const { parse, compileScript } = require('vue/compiler-sfc')
const { renderToString } = require('vue/server-renderer')
const ts = require('typescript')

// Exercise the component's real setup and event handlers with controlled API responses and timers.
async function salesSelection(api, timers) {
  const filename = join(__dirname, '../src/components/AnalyticsSalesInsights.vue')
  const { descriptor } = parse(readFileSync(filename, 'utf8'), { filename })
  const { content } = compileScript(descriptor, { id: 'sales-category-test' })
  const { outputText } = ts.transpileModule(content, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  })
  const compiled = {}
  compileFunction(outputText, ['require', 'exports', 'window'])(
    name => name === '../analyticsApi' ? api : require(name), compiled, timers,
  )
  let state
  await renderToString(createSSRApp({
    setup() {
      state = compiled.default.setup({ mode: 'selection', active: false }, { expose() {}, emit() {} })
      return () => null
    },
  }))
  state.seasons.value = ['2026冬']
  return state
}

test('same category preserves the lock; switching clears the query, request and in-flight suggestions', async () => {
  const requests = []
  let fireTimer
  let finishSearch
  const state = await salesSelection({
    async previewSalesSelection(filters) {
      requests.push({ category: filters.category, code: filters.target_style_code })
      return { scope: { ...filters }, target_hero: filters.target_style_code ? { code: filters.target_style_code } : null }
    },
    searchSalesStyles() {
      return new Promise(resolve => { finishSearch = resolve })
    },
  }, {
    setTimeout(callback) { fireTimer = callback; return 1 },
    clearTimeout() { fireTimer = undefined },
  })

  await state.chooseSearchedStyle({ code: '669215' })
  await state.pickCategory('长袖套衫')
  assert.equal(state.styleSearchQuery.value, '669215')
  assert.equal(state.targetStyleCode.value, '669215')
  assert.equal(requests.length, 1)

  state.onStyleSearchInput()
  const lateReply = fireTimer()
  await state.pickCategory('小品类')
  finishSearch({ items: [{ code: '669215' }] })
  await lateReply

  assert.deepEqual(requests.at(-1), { category: '小品类', code: '' })
  assert.equal(state.styleSearchQuery.value, '')
  assert.equal(state.targetStyleCode.value, '')
  assert.equal(state.styleSearchOpen.value, false)
  assert.deepEqual(state.styleSearchHits.value, [])
})

