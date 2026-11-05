<script setup lang="ts">
import { nextTick, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { loginSite } from '../api'

const route = useRoute()
const router = useRouter()
const password = ref('')
const passwordInput = ref<HTMLInputElement | null>(null)
const error = ref('')
const submitting = ref(false)

function nextPath() {
  const value = route.query.next
  if (typeof value !== 'string' || !value.startsWith('/') || value.startsWith('//') || value.startsWith('/login')) return '/'
  return value
}

async function submit() {
  error.value = ''
  if (!password.value) {
    error.value = '请输入密码'
    return
  }
  submitting.value = true
  try {
    await loginSite(password.value)
    await router.replace(nextPath())
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '密码不正确，请再试一次'
    password.value = ''
    await nextTick()
    passwordInput.value?.focus()
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <main class="login-page">
    <form class="login-card" @submit.prevent="submit">
      <h1>貂帅服装</h1>
      <p class="login-en">DIAOSHUAI KNIT</p>
      <span class="login-rule" aria-hidden="true"></span>
      <p class="login-sub">女装针织 · 款式图库</p>
      <label for="site-password">密码</label>
      <input id="site-password" ref="passwordInput" v-model="password" type="password" name="password" autocomplete="current-password" placeholder="请输入访问密码" autofocus />
      <button class="login-enter" type="submit" :disabled="submitting">{{ submitting ? '正在进入…' : '进入' }}</button>
      <p v-if="error" class="login-error" role="alert">{{ error }}</p>
    </form>
  </main>
</template>
