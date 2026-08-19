// useBackendStatus.js —— 后端服务状态统一展示（首页 / 标题栏共用）
import { computed } from 'vue'
import { store } from '../store'

export function useBackendStatus() {
  // 状态芯片配色：ready=绿 / busy=蓝 / error=灰
  const chipClass = computed(() => {
    if (!store.backend) return ''
    if (store.backend.ready) return 'ready'
    if (store.backend.apiUp || store.backend.llamaUp) return 'busy'
    if (store.backend.procs?.llama || store.backend.procs?.api) return 'busy'
    return 'error'
  })
  const chipText = computed(() => {
    if (!store.backend) return '检测中…'
    if (store.backend.quantizing) return `正在量化 ${String(store.backend.quantizing).toUpperCase()} 模型…`
    if (store.backend.ready) {
      const fb = store.backend.cpuFallback
      if (fb?.llama && fb?.api) return 'OCR 服务已就绪（llama/Paddle 均回退 CPU）'
      if (fb?.llama) return 'OCR 服务已就绪（llama 回退 CPU）'
      if (fb?.api) return 'OCR 服务已就绪（Paddle 回退 CPU）'
      return 'OCR 服务已就绪'
    }
    // API(paddlex 8080) 已就绪但 llama 未就绪：VLM 模型仍在加载/预热
    if (store.backend.apiUp) return '正在加载模型…'
    // llama 已就绪但 API 未就绪：模型加载完成，等待连接 API 服务
    if (store.backend.llamaUp) return '等待 API 服务…'
    // 进程已拉起但端口尚未就绪：启动/预热早期
    if (store.backend.procs?.llama || store.backend.procs?.api) return '正在加载模型…'
    return 'OCR 服务未启动'
  })
  const chipTitle = computed(() => '点击一键启动 OCR 服务（llama-server + paddlex serve）')
  return { chipClass, chipText, chipTitle }
}
