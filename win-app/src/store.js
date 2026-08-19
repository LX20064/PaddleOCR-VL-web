// store.js —— 全局响应式状态
import { reactive } from 'vue'

export const store = reactive({
  settings: null,        // 应用设置
  sys: null,             // 系统信息（GPU 等）
  backend: null,         // 后端服务状态
  setup: null,           // 环境安装状态
  results: {},           // 识别结果 {path: {md, images, imgUris, download}}（跨模块共享缓存）
  acquireTray: [],       // 已获取页面托盘 [{path,name,source,uri}]（获取文档页）
  ocrInbox: [],          // 待识别文件路径 [path]（获取页/摄像头 → 文档识别）
  running: false,        // 是否正在批量处理
  stopRequested: false,  // 是否请求停止批量
  logs: [],
})

// 图片 base64 头嗅探 MIME
function mimeOf(b64) {
  if (!b64) return 'image/png'
  const h = b64.slice(0, 32)
  if (h.startsWith('/9j/')) return 'image/jpeg'
  if (h.startsWith('iVBORw0KGgo')) return 'image/png'
  if (h.startsWith('R0lGOD')) return 'image/gif'
  if (h.startsWith('UklGR')) return 'image/webp'
  return 'image/png'
}

// Markdown 内嵌图片：<img src="imgs/x.jpg"> → data URI
export function inlineImages(md, images) {
  if (!md || !images) return md || ''
  return md.replace(/src="([^"]+)"/g, (m, src) => {
    const b64 = images[src]
    return b64 ? `src="data:${mimeOf(b64)};base64,${b64}"` : m
  })
}

export function statusLabel(s) {
  return { wait: '等待', run: '处理中', done: '完成', fail: '失败', stop: '已停止' }[s] || s
}

export function log(msg) {
  const t = new Date().toLocaleTimeString('zh-CN', { hour12: false })
  store.logs.push(`[${t}] ${msg}`)
  if (store.logs.length > 500) store.logs.splice(0, store.logs.length - 500)
}
