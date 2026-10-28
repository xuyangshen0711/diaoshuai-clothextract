<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import {
  addTaxonomyCategory, deleteTaxonomyCategory, deleteTaxonomySynonym, getTaxonomy, renameTaxonomyCategory,
  saveTaxonomySynonym, type Taxonomy,
} from '../api'

const taxonomy = ref<Taxonomy | null>(null)
const error = ref('')
const message = ref('')
const level1Name = ref('')
const childParent = ref('')
const childName = ref('')
const phrase = ref('')
const synonymLevel1 = ref('')
const synonymLevel2 = ref('')
const synonymSleeve = ref('')
const synonymFabric = ref('')
const renameDrafts = ref<Record<string, string>>({})

const childOptions = computed(() => taxonomy.value?.tree.find(item => item.level1 === synonymLevel1.value)?.children || [])
const addChildOptions = computed(() => taxonomy.value?.tree.find(item => item.level1 === childParent.value)?.children || [])

watch(synonymLevel1, () => { synonymLevel2.value = '' })

function keyOf(level1: string, level2: string) {
  return `${level1}\n${level2}`
}

function rememberNames(data: Taxonomy) {
  const drafts: Record<string, string> = {}
  for (const node of data.tree) {
    drafts[keyOf(node.level1, '')] = node.level1
    for (const child of node.children) drafts[keyOf(node.level1, child)] = child
  }
  renameDrafts.value = drafts
}

async function load() {
  error.value = ''
  taxonomy.value = await getTaxonomy()
  rememberNames(taxonomy.value)
  if (!childParent.value && taxonomy.value.tree[0]) childParent.value = taxonomy.value.tree[0].level1
  if (!synonymLevel1.value && taxonomy.value.tree[0]) synonymLevel1.value = taxonomy.value.tree[0].level1
}

async function run(action: () => Promise<Taxonomy>, done: string) {
  error.value = ''
  message.value = ''
  try {
    taxonomy.value = await action()
    rememberNames(taxonomy.value)
    message.value = done
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : String(reason)
  }
}

function addLevel1() {
  const name = level1Name.value.trim()
  if (!name) { error.value = '请填写一级品类名称'; return }
  return run(() => addTaxonomyCategory(name), '已添加一级品类').then(() => { level1Name.value = '' })
}

function addLevel2() {
  const name = childName.value.trim()
  if (!childParent.value || !name) { error.value = '请选择一级品类并填写二级名称'; return }
  return run(() => addTaxonomyCategory(childParent.value, name), '已添加二级品类').then(() => { childName.value = '' })
}

function rename(level1: string, level2: string) {
  const draft = (renameDrafts.value[keyOf(level1, level2)] || '').trim()
  if (!draft) { error.value = '品类名称不能为空'; return }
  const newLevel1 = level2 ? level1 : draft
  const newLevel2 = level2 ? draft : ''
  return run(() => renameTaxonomyCategory(level1, level2, newLevel1, newLevel2), '已改名')
}

function remove(level1: string, level2 = '') {
  const label = level2 ? `${level1}>${level2}` : level1
  if (!window.confirm(`删除品类「${label}」？已有资料仍在使用时会拒绝删除。`)) return
  return run(() => deleteTaxonomyCategory(level1, level2), '已删除品类')
}

function addSynonym() {
  if (!phrase.value.trim() || !synonymLevel1.value) { error.value = '请填写原始叫法和要归入的品类'; return }
  const children = childOptions.value
  if (children.length && !synonymLevel2.value) { error.value = '这个一级品类还有二级，请选到二级'; return }
  return run(
    () => saveTaxonomySynonym(phrase.value.trim(), synonymLevel1.value, children.length ? synonymLevel2.value : '', synonymSleeve.value, synonymFabric.value),
    '已保存同义词',
  ).then(() => { phrase.value = '' })
}

onMounted(() => { load().catch(reason => { error.value = reason instanceof Error ? reason.message : String(reason) }) })
</script>

<template>
  <main class="categories-page">
    <div class="page-head">
      <div>
        <h2>品类设置</h2>
        <p class="meta">品类是一张固定表。导入资料只会归入表中的品类，不能在导入时新建。新增、改名和删除只在这里进行。待归类是系统品类，不能改名或删除。</p>
      </div>
    </div>
    <p v-if="error" class="error">{{ error }}</p>
    <p v-if="message" class="meta">{{ message }}</p>
    <section v-if="taxonomy" aria-labelledby="category-table-title">
      <h3 id="category-table-title">固定品类</h3>
      <div class="field-row">
        <div class="field"><label for="new-level1">新的一级品类</label><input id="new-level1" v-model="level1Name" type="text" /></div>
        <button class="btn" type="button" @click="addLevel1">添加一级</button>
      </div>
      <div class="field-row">
        <div class="field"><label for="child-parent">挂到一级</label>
          <select id="child-parent" v-model="childParent">
            <option v-for="node in taxonomy.tree" :key="node.level1" :value="node.level1">{{ node.level1 }}</option>
          </select>
        </div>
        <div class="field"><label for="new-level2">新的二级品类</label><input id="new-level2" v-model="childName" type="text" /></div>
        <button class="btn" type="button" @click="addLevel2">添加二级</button>
      </div>
      <p v-if="addChildOptions.length" class="meta">「{{ childParent }}」目前的二级：{{ addChildOptions.join('、') }}</p>
      <table class="detail-table">
        <thead><tr><th>一级</th><th>二级</th><th>名称</th><th></th></tr></thead>
        <tbody>
          <template v-for="node in taxonomy.tree" :key="node.level1">
            <tr>
              <td>{{ node.level1 }}</td>
              <td>—</td>
              <td><input v-model="renameDrafts[`${node.level1}\n`]" type="text" :aria-label="`重命名${node.level1}`" /></td>
              <td>
                <button class="btn" type="button" @click="rename(node.level1, '')">改名</button>
                <button class="btn" type="button" @click="remove(node.level1)">删除</button>
              </td>
            </tr>
            <tr v-for="child in node.children" :key="`${node.level1}-${child}`">
              <td>{{ node.level1 }}</td>
              <td>{{ child }}</td>
              <td><input v-model="renameDrafts[`${node.level1}\n${child}`]" type="text" :aria-label="`重命名${node.level1}${child}`" /></td>
              <td>
                <button class="btn" type="button" @click="rename(node.level1, child)">改名</button>
                <button class="btn" type="button" @click="remove(node.level1, child)">删除</button>
              </td>
            </tr>
          </template>
          <tr>
            <td>{{ taxonomy.unsorted }}</td>
            <td>—</td>
            <td colspan="2">系统品类，导入时对不上的资料放在这里。</td>
          </tr>
        </tbody>
      </table>
    </section>
    <section v-if="taxonomy" aria-labelledby="synonym-title">
      <h3 id="synonym-title">同义词</h3>
      <p class="meta">本厂档案的一级品类、AI 输出、网图和品牌资料里的叫法，都先查这张表，再写成固定品类加上袖长和面料。</p>
      <div class="field-row">
        <div class="field"><label for="synonym-phrase">原始叫法</label><input id="synonym-phrase" v-model="phrase" type="text" /></div>
        <div class="field"><label for="synonym-level1">归入一级</label>
          <select id="synonym-level1" v-model="synonymLevel1">
            <option v-for="node in taxonomy.tree" :key="node.level1" :value="node.level1">{{ node.level1 }}</option>
          </select>
        </div>
        <div v-if="childOptions.length" class="field"><label for="synonym-level2">归入二级</label>
          <select id="synonym-level2" v-model="synonymLevel2">
            <option value="">请选择</option>
            <option v-for="name in childOptions" :key="name" :value="name">{{ name }}</option>
          </select>
        </div>
        <div class="field"><label for="synonym-sleeve">袖长</label>
          <select id="synonym-sleeve" v-model="synonymSleeve">
            <option value="">不指定</option>
            <option v-for="name in taxonomy.sleeves" :key="name" :value="name">{{ name }}</option>
          </select>
        </div>
        <div class="field"><label for="synonym-fabric">面料</label>
          <select id="synonym-fabric" v-model="synonymFabric">
            <option value="">不指定</option>
            <option v-for="name in taxonomy.fabrics" :key="name" :value="name">{{ name }}</option>
          </select>
        </div>
        <button class="btn primary" type="button" @click="addSynonym">保存同义词</button>
      </div>
      <table class="detail-table">
        <thead><tr><th>原始叫法</th><th>固定品类</th><th>袖长</th><th>面料</th><th>来源</th><th></th></tr></thead>
        <tbody>
          <tr v-for="item in taxonomy.synonyms" :key="item.phrase">
            <td>{{ item.phrase }}</td>
            <td>{{ item.label }}</td>
            <td>{{ item.sleeve_length || '—' }}</td>
            <td>{{ item.fabric || '—' }}</td>
            <td>{{ item.learned ? '审核时记住' : '初始对照' }}</td>
            <td><button class="btn" type="button" @click="run(() => deleteTaxonomySynonym(item.phrase), '已删除同义词')">删除</button></td>
          </tr>
        </tbody>
      </table>
    </section>
  </main>
</template>
