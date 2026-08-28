<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { api } from './api'

const route = useRoute()
const online = ref(false)
const sidebarOpen = ref(false)
const title = computed(() => route.meta.title || 'Agent Console')
const section = computed(() => route.meta.section || '')

const nav = [
  { label: '总览', items: [{ to: '/', icon: '⌁', text: '运行概览' }] },
  { label: '消息策略', items: [
    { to: '/rules', icon: '◫', text: '监听与转发' },
    { to: '/prompts', icon: '¶', text: 'Prompt 管理' },
  ] },
  { label: '知识与确认', items: [
    { to: '/memories', icon: '◇', text: '长期记忆' },
    { to: '/clarifications', icon: '?', text: '待确认事项' },
  ] },
  { label: '系统', items: [{ to: '/operations', icon: '≡', text: '日程与审计' }] },
]

onMounted(async () => {
  try { await api.health(); online.value = true } catch (_) { online.value = false }
})
</script>

<template>
  <div class="app-shell">
    <aside class="sidebar" :class="{ open: sidebarOpen }">
      <div class="brand">
        <div class="brand-mark">微</div>
        <div><strong>WeChat Agent</strong><span>重要消息控制台</span></div>
      </div>
      <nav>
        <section v-for="group in nav" :key="group.label" class="nav-section">
          <p>{{ group.label }}</p>
          <RouterLink v-for="item in group.items" :key="item.to" :to="item.to" @click="sidebarOpen = false">
            <i>{{ item.icon }}</i><span>{{ item.text }}</span>
          </RouterLink>
        </section>
      </nav>
      <div class="sidebar-foot">
        <span class="status-dot" :class="{ online }"></span>
        <div><strong>{{ online ? 'API 已连接' : 'API 未连接' }}</strong><small>本地管理服务</small></div>
      </div>
    </aside>
    <main class="main-area">
      <header class="topbar">
        <button class="mobile-menu" @click="sidebarOpen = !sidebarOpen">☰</button>
        <div><span>{{ section }}</span><h1>{{ title }}</h1></div>
        <div class="topbar-note"><span class="pill neutral">LOCAL</span><small>配置修改后需重启 Agent</small></div>
      </header>
      <div class="page-container"><RouterView /></div>
    </main>
    <div v-if="sidebarOpen" class="backdrop" @click="sidebarOpen = false"></div>
  </div>
</template>
