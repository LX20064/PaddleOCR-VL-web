<template>
  <div class="rv">
    <!-- 头部：文件名 + 导出操作 -->
    <div class="rv-head" v-if="item">
      <span class="rv-name" :title="item.path">{{ item.name }}</span>
      <span class="rv-status" :class="'st-' + item.status">{{ statusLabel(item.status) }}</span>
      <span style="flex:1"></span>
      <span style="flex:0 0 32px"></span>
      <slot name="actions"></slot>
      <el-button size="small" :disabled="!hasResult" @click="$emit('export', 'docx')">Word</el-button>
      <el-button size="small" :disabled="!hasResult" @click="$emit('export', 'html')">HTML</el-button>
      <el-button size="small" :disabled="!hasResult" @click="$emit('export', 'json')">JSON</el-button>
      <el-button size="small" :disabled="!hasResult" @click="$emit('export', 'zip_md')">Markdown</el-button>
      <el-button size="small" :disabled="!hasResult" @click="copyMd">复制Markdown</el-button>
    </div>

    <!-- 视图切换 -->
    <div class="rv-tabs" v-if="item">
      <button :class="{ active: tab === 'render' }" @click="tab = 'render'">渲染</button>
      <button :class="{ active: tab === 'md' }" @click="tab = 'md'">Markdown</button>
      <button :class="{ active: tab === 'img' }" @click="tab = 'img'">原图</button>
    </div>

    <!-- 内容 -->
    <div class="rv-body selectable">
      <div v-if="!item" class="empty-hint">{{ emptyHint }}</div>
      <template v-else-if="tab === 'render'">
        <div v-if="!hasResult" class="empty-hint">
          {{ item.status === 'fail' ? '识别失败：' + (item.error || '') : pendingHint }}
        </div>
        <div v-else class="markdown-body" v-html="renderHtml"></div>
      </template>
      <pre v-else-if="tab === 'md'" class="md-src">{{ result?.md || '' }}</pre>
      <div v-else-if="tab === 'img'" style="text-align:center">
        <div v-if="!result" class="empty-hint">{{ pendingHint }}</div>
        <el-button v-else-if="result.imgUris === null" size="small" @click="$emit('load-images')" style="margin:20px 0">加载原图预览</el-button>
        <template v-else-if="result.imgUris.length">
          <img v-for="(u, i) in result.imgUris" :key="i" :src="u" style="max-width:100%;margin-bottom:10px;border-radius:6px" />
        </template>
        <div v-else class="empty-hint">无原图可预览</div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { marked } from 'marked'
import katex from 'katex'
import DOMPurify from 'dompurify'
import 'katex/dist/katex.min.css'
import { statusLabel, inlineImages } from '../store'

// 匹配 LaTeX 公式（与 wordrender.py 的 _MATH_RE 一致）：$$...$$ / \[...\] / \(...\) / $...$
const MATH_RE = /(?<!\\)\$\$(?:(?!\$\$)[\s\S])*?(?<!\\)\$\$|(?<!\\)\\\[(?:(?!\\\])[\s\S])*?(?<!\\)\\\]|(?<!\\)\\\((?:(?!\\\))[\s\S])*?(?<!\\)\\\)|(?<![\\$])\$(?!\$)(?:\\.|[^$\r\n])+?(?<!\\)\$(?!\$)/g
// 纯字母数字占位符（避免 marked 把它当成 __粗体__ 等 Markdown 语法）
const MATH_TOKEN = `M${Math.random().toString(36).slice(2).toUpperCase()}Z`

// 先把 Markdown 里的公式替换成占位符，返回 { text, blocks }，blocks[i] 为对应 KaTeX HTML
function protectMath(md) {
  if (!md) return { text: md, blocks: [] }
  const blocks = []
  const text = md.replace(MATH_RE, (m) => {
    let display = false
    let tex = m
    if (m.startsWith('$$')) { display = true; tex = m.slice(2, -2) }
    else if (m.startsWith('\\[')) { display = true; tex = m.slice(2, -2) }
    else if (m.startsWith('\\(')) { tex = m.slice(2, -2) }
    else { tex = m.slice(1, -1) } // $...$
    try {
      const html = katex.renderToString(tex.trim(), { displayMode: display, throwOnError: false })
      const token = `${MATH_TOKEN}${blocks.length}`
      blocks.push(html)
      return token
    } catch (_) {
      return m // 渲染失败保留原文
    }
  })
  return { text, blocks }
}

const props = defineProps({
  item: Object,          // 队列项 {path,name,status,error,...}
  result: Object,        // store.results[path]
  emptyHint: { type: String, default: '从左侧选择一个文件查看识别结果' },
  pendingHint: { type: String, default: '尚未识别' },
})
defineEmits(['export', 'open-download', 'load-images'])

const tab = ref('render')
const hasResult = computed(() => !!props.result?.md)
const renderHtml = computed(() => {
  const c = props.result
  if (!c?.md) return ''
  const { text, blocks } = protectMath(c.md)
  let html = marked.parse(inlineImages(text, c.images))
  // 用函数替换：blocks[i] 含 $ 时 String.replace 的替换串有特殊语义，函数替换最稳
  for (let i = 0; i < blocks.length; i++) html = html.replace(`${MATH_TOKEN}${i}`, () => blocks[i])
  // OCR 结果来自不可信文档，可能夹带 <img onerror>/<iframe> 等事件属性与危险标签，
  // 统一用 DOMPurify 净化（默认保留 KaTeX 所需的 style/class）。
  return DOMPurify.sanitize(html)
})

// 把当前识别结果的原始 Markdown 复制到剪贴板（纯前端操作，不触发任何扫描/识别）
async function copyMd() {
  const md = props.result?.md
  if (!md) return
  try {
    await window.api.copyText(md)
    ElMessage.success('Markdown 已复制')
  } catch (_) {
    ElMessage.error('复制失败，请重试')
  }
}

// 切换文件时回到渲染视图
watch(() => props.item?.path, () => { tab.value = 'render' })
</script>

<style scoped>
.rv { display: flex; flex-direction: column; height: 100%; min-height: 0; }
.rv-head {
  display: flex; align-items: center; gap: 6px; flex-wrap: wrap;
  padding: 8px 12px; border-bottom: 1px solid var(--border);
}
.rv-name { font-size: 13px; font-weight: 600; max-width: 260px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.rv-status { font-size: 12px; color: var(--muted); }
.rv-status.st-done { color: var(--ok); }
.rv-status.st-fail { color: var(--err); }
.rv-status.st-run { color: var(--accent); }
.rv-tabs { display: flex; gap: 4px; padding: 0 12px; border-bottom: 1px solid var(--border); flex-shrink: 0; }
.rv-tabs button {
  border: none; background: none; cursor: pointer; padding: 8px 14px;
  font-size: 13px; color: var(--muted); border-radius: 8px 8px 0 0; font-weight: 500;
}
.rv-tabs button.active { color: var(--accent); box-shadow: inset 0 -2px 0 var(--accent); }
.rv-body { flex: 1; overflow-y: auto; padding: 14px; min-height: 0; }
.rv-body .markdown-body { padding: 0; }
</style>
