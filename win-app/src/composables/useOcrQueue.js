// useOcrQueue.js —— OCR 任务队列通用逻辑（供扫描工作台 / 文档识别复用）
import { ref, reactive, computed, watch, onMounted, onBeforeUnmount } from 'vue'
import { ElMessage } from 'element-plus'
import { store, log } from '../store'

export function useOcrQueue() {
  const queue = reactive([])          // [{path,name,status,progress,desc,pages,time,download,error}]
  const selectedPath = ref(null)      // 以路径标识选中项，避免索引错位

  // 自动扫描：勾选仅作为「识别完成后自动按勾选格式保存」的设置项，
  // 不再自动触发识别——识别统一由「批量识别」按钮（或获取页/摄像头经 ocrInbox 入队）开始。
  const autoScan = ref(false)
  const autoFormats = ref(['docx'])   // 自动保存格式：docx / html / json / zip_md

  // 自动保存格式必须至少勾选一种：全部取消勾选时自动退回手动模式，
  // 避免「自动扫描开启但无任何格式可保存」的空转状态。
  watch(autoFormats, (list) => {
    if (!list.length) autoScan.value = false
  })

  const selectedItem = computed(() => queue.find((x) => x.path === selectedPath.value) || null)
  const selectedResult = computed(() => {
    const it = selectedItem.value
    return it ? store.results[it.path] || null : null
  })
  const hasResult = computed(() => !!selectedResult.value?.md)
  const pendingCount = computed(() => queue.filter((x) => x.status === 'wait').length)
  const stats = computed(() => ({
    done: queue.filter((x) => x.status === 'done').length,
    fail: queue.filter((x) => x.status === 'fail').length,
    stopped: queue.filter((x) => x.status === 'stop').length,
    pages: queue.reduce((s, x) => s + (x.pages || 0), 0),
    secs: queue.reduce((s, x) => s + (x.time || 0), 0),
  }))

  function pushFiles(paths, { select = false } = {}) {
    let added = 0
    for (const p of paths) {
      const existing = queue.find((x) => x.path === p)
      if (existing) {
        // 已存在但失败/已停止：重置为待识别，让重新添加即可重试（否则会成为死胡同）
        if (existing.status === 'fail' || existing.status === 'stop') {
          existing.status = 'wait'
          existing.progress = 0
          existing.desc = ''
          existing.error = ''
          added++
        }
        continue
      }
      queue.push({ path: p, name: p.split(/[\\/]/).pop(), status: 'wait', progress: 0, desc: '', pages: 0, time: 0, download: null })
      added++
    }
    if (select && paths.length) selectedPath.value = paths[paths.length - 1]
    if (added) log(`已添加 ${added} 个文件，队列共 ${queue.length} 个`)
    return added
  }


  async function addFiles() {
    const files = await window.api.chooseFiles()
    if (files.length) pushFiles(files, { select: true })
  }

  async function addFolder(includeSub = false) {
    const dir = await window.api.chooseFolder()
    if (!dir) return
    const files = await window.api.listFiles(dir, !!includeSub)
    pushFiles(files, { select: true })
  }

  function onDrop(e) {
    const files = [...(e.dataTransfer?.files || [])]
    const paths = files.map((f) => window.api.getPathForFile(f)).filter(Boolean)
    pushFiles(paths, { select: true })
  }

  function select(path) { selectedPath.value = path }

  function removeSelected() {
    const it = selectedItem.value
    if (!it) return
    delete store.results[it.path]
    const i = queue.findIndex((x) => x.path === it.path)
    queue.splice(i, 1)
    // 删除后选中相邻项，保持上下文连续
    const next = queue[Math.min(i, queue.length - 1)]
    selectedPath.value = next ? next.path : null
  }

  function clearQueue() {
    // 清空队列并自动清空全部识别结果缓存（md / base64 图片 / prunedResults），释放内存
    for (const key of Object.keys(store.results)) delete store.results[key]
    queue.splice(0)
    selectedPath.value = null
    log('已清空队列与识别缓存')
  }

  function buildParams() {
    const d = store.settings?.defaults || {}
    const n = (v) => Number(v) > 0 ? Number(v) : null
    return {
      maxPixels: d.max_pixels || 0,
      minPixels: n(d.min_pixels),
      maxNewTokens: n(d.max_new_tokens),
      repetitionPenalty: d.repetition_penalty != null ? Number(d.repetition_penalty) : null,
      useSeal: !!d.use_seal,
      useChart: !!d.use_chart,
      useOrientation: !!d.use_orientation,
      useUnwarping: !!d.use_unwarping,
      useOcrForImageBlock: !!d.use_ocr_image_block,
      formatBlockContent: !!d.use_format_block,
      useLayoutDetection: d.use_layout_mode !== false,
      promptLabel: '',
      mergeLayoutBlocks: d.use_merge_blocks !== false,
      layoutThreshold: null,
      temperature: null,
      topP: null,
      layoutNms: null, layoutUnclipRatio: null, layoutMergeBboxesMode: null,
      layoutShapeMode: null, vlmExtraArgs: null, markdownIgnoreLabels: null,
      keep_source_dir: !!d.keep_source_dir,
      skip_existing: !!d.skip_existing,
    }
  }

  function makeReq(filePath, mode, perPage, exportChart) {
    return {
      filePath,
      mode: mode || 'scan',
      perPage: !!perPage,
      exportChart: !!exportChart,
      settings: { apiUrl: store.settings?.apiUrl, timeout: store.settings?.timeout },
      params: buildParams(),
    }
  }

  async function runOne(it, mode) {
    it.status = 'run'
    it.progress = 0
    it.desc = '准备中…'
    const req = makeReq(it.path, mode, store.settings?.defaults?.pdf_per_page, store.settings?.defaults?.export_chart)
    try {
      const res = await window.api.parseFile(req)
      if (!res.ok) throw new Error(res.error || '解析失败')
      // 识别期间该文件可能已被用户移除/清空队列：不再写回缓存与选中项，
      // 避免已删除项的结果“复活”并重新占用内存。
      if (!queue.includes(it)) return false
      it.status = 'done'
      it.pages = res.pages || 1
      it.time = res.elapsed || 0
      it.download = res.download || null
      store.results[it.path] = { md: res.md, images: res.images || {}, download: res.download, prunedResults: res.prunedResults || null, imgUris: null }
      // 仅当当前没有正在查看的有效项时才自动选中，避免批量运行中每完成一项就抢占右侧视图
      if (!queue.some((x) => x.path === selectedPath.value)) selectedPath.value = it.path
      log(`✔ ${it.name}：${res.status}`)
      return true
    } catch (e) {
      // 运行期间被移除的项不再标失败、不再写日志，避免扰动清理后的队列状态
      if (!queue.includes(it)) return false
      it.status = 'fail'
      it.error = e.message
      log(`✖ ${it.name}：${e.message}`)
      return false
    }
  }

  // 等待后端服务就绪（主进程每 5s 兜底推送状态，App.vue 已实时同步 store.backend）
  function waitBackendReady(timeoutMs = 600000) {
    return new Promise((resolve) => {
      if (store.backend?.ready) return resolve(true)
      const start = Date.now()
      // 记录进入等待时是否已有服务进程：若启动中的进程随后退出（模型损坏/显存不足），
      // 提前判定失败，避免死等满 timeoutMs。
      const hadProcs = !!(store.backend?.procs?.api || store.backend?.procs?.llama)
      const timer = setInterval(() => {
        if (store.backend?.ready) { clearInterval(timer); resolve(true) }
        else if (Date.now() - start > timeoutMs) { clearInterval(timer); resolve(false) }
        else if (hadProcs && !store.backend?.procs?.api && !store.backend?.procs?.llama) {
          clearInterval(timer); resolve(false)
        }
      }, 2000)
    })
  }

  // 批量识别：并发数取设置 maxParallel（默认 1 = 串行），并持续拾取队列中的
  // wait 项，因此识别期间新加入的文件会自动续跑，直到全部完成或用户停止。
  // 开始时确保 OCR 服务就绪：仅当服务完全未启动才调用 startBackend()（services.start()
  // 幂等，已启动的进程不会重复 spawn、不会重复加载模型）；正在启动/预热中只等待就绪。
  // 即 API 服务只在启动时准备一次，之后除非服务异常退出（procs 清空）才允许重新加载。
  async function startBatch() {
    if (store.running) return
    store.running = true
    store.stopRequested = false
    try {
      if (!store.backend?.ready) {
        const b = store.backend || {}
        const loading = b.procs?.api || b.procs?.llama || b.apiUp || b.llamaUp
        if (loading) {
          log('OCR 服务准备中，等待就绪…')
          ElMessage.info('OCR 服务准备中，请稍候…')
        } else {
          log('OCR 服务未启动，正在启动（仅此一次）…')
          const r = await window.api.startBackend()
          if (!r.ok) {
            ElMessage.error(r.error || 'OCR 服务启动失败')
            return
          }
        }
        if (!(await waitBackendReady())) {
          ElMessage.error('OCR 服务启动超时或服务异常退出，请查看「设置 → 服务日志」')
          return
        }
      }
      const maxP = Math.max(1, Number(store.settings?.maxParallel) || 1)
      const initial = queue.filter((x) => x.status === 'wait').length
      if (!initial) return
      log(`▶ 开始批量识别，共 ${initial} 个文件（并发 ${maxP}）`)
      let done = 0
      // worker 池：启动 maxP 个 worker 各自循环领取 wait 项。相比「整批屏障」，
      // 单个大文件不会阻塞其他槽位空转，识别期间新加入的文件也会被立即拾取续跑。
      async function worker() {
        while (!store.stopRequested) {
          const it = queue.find((x) => x.status === 'wait')
          if (!it) break
          try {
            const ok = await runOne(it)
            if (ok) {
              done++
              await loadImagesFor(it)
              // 自动扫描：识别完成即按勾选格式保存（失败仅记日志，不中断批量）
              if (autoScan.value && autoFormats.value.length) {
                for (const f of autoFormats.value) await exportFor(it, f)
              }
            }
          } catch (e) {
            // 单个文件的意外异常（如原图预览 IPC 抛错）不应中断整个批量，
            // 否则剩余 wait 项会永远卡住且无任何提示。标记本项失败后续跑。
            if (queue.includes(it) && it.status === 'run') {
              it.status = 'fail'
              it.error = e.message
            }
            log(`✖ ${it.name}：${e.message}`)
          }
        }
      }
      // 外层 do/while：识别期间新入队的文件（此时 store.running 仍为 true，外层不会
      // 重复调用 startBatch）在 workers 全部退出后若仍为 wait，则再跑一轮，避免被遗留。
      do {
        await Promise.all(Array.from({ length: maxP }, () => worker()))
      } while (!store.stopRequested && queue.some((x) => x.status === 'wait'))
      if (store.stopRequested) {
        log(`⏹ 批量识别已停止：完成 ${done} 个`)
        ElMessage.warning(`已停止：完成 ${done} 个`)
      } else {
        log('🏁 批量识别结束')
        ElMessage.success(`批量识别结束：成功 ${done} 个`)
      }
    } finally {
      store.running = false
    }
  }

  function stopBatch() {
    store.stopRequested = true
    for (const it of queue) {
      if (it.status === 'wait') it.status = 'stop'
    }
    log('⏹ 已停止（当前正在解析的会继续跑完）')
  }

  // 按队列项导出指定格式（优先复用已识别的 md/images，避免重新跑 VLM 识别）。
  // 手动导出与自动扫描共用：成功返回 {ok:true,download,status}，失败返回 {ok:false,error}，
  // 不抛异常（自动保存时失败仅记日志，不打断批量流程）。
  async function exportFor(it, mode) {
    if (!it) return null
    const c = store.results[it.path]
    try {
      let res
      // 有缓存即可本地导出（JSON 额外需要 prunedResults，识别时已一并缓存）；
      // 仅当 JSON 且缓存缺结构化结果时才退回完整解析。
      if (c && (mode !== 'json' || c.prunedResults)) {
        // 注意：c.images / c.prunedResults 是 Pinia 响应式对象（Proxy），直接经 IPC 传会触发
        // 「An object could not be cloned」，这里深拷贝成普通对象再传。
        res = await window.api.exportRender({
          filePath: it.path, mode, md: c.md,
          images: { ...(c.images || {}) },
          prunedResults: c.prunedResults ? JSON.parse(JSON.stringify(c.prunedResults)) : undefined,
        })
      } else {
        // 无缓存或 JSON 需要结构化结果但缓存缺失 → 走完整解析
        res = await window.api.parseFile(makeReq(it.path, mode, false, false))
      }
      if (!res.ok) throw new Error(res.error)
      it.download = res.download
      if (c) c.download = res.download
      log(`✔ 已导出 ${mode}：${res.status}`)
      return { ok: true, download: res.download, status: res.status }
    } catch (e) {
      log(`✖ 导出 ${mode} 失败：${e.message}`)
      return { ok: false, error: e.message }
    }
  }

  // 以指定格式重新导出（识别结果页顶部的 Word / HTML / JSON / Markdown 按钮）
  async function exportMode(mode) {
    const r = await exportFor(selectedItem.value, mode)
    if (!r) return
    if (r.ok) {
      window.api.revealPath(r.download)
      ElMessage.success(`导出完成：${r.download}`)
    } else {
      ElMessage.error('导出失败：' + r.error)
    }
  }

  function openDownload() {
    const c = selectedResult.value
    if (c?.download) window.api.revealPath(c.download)
  }

  // 正在加载原图预览的文件路径集合：防止同一结果被并发重复加载（自动加载 + 手动点击）
  const imgLoading = new Set()

  async function loadImagesFor(it) {
    const c = store.results[it.path]
    if (!c || imgLoading.has(it.path)) return
    imgLoading.add(it.path)
    try {
      if (it.path.toLowerCase().endsWith('.pdf')) {
        const r = await window.api.renderPdfPreview(it.path)
        if (r.ok) {
          c.imgUris = []
          for (const p of r.pages) {
            const uri = await window.api.imageDataUri(p)
            if (uri) c.imgUris.push(uri)
          }
        } else c.imgUris = []
      } else {
        const uri = await window.api.imageDataUri(it.path)
        c.imgUris = uri ? [uri] : []
      }
    } finally {
      imgLoading.delete(it.path)
    }
  }

  // 加载当前选中结果的原图预览
  async function loadSelectedImages() {
    const it = selectedItem.value
    if (!it || !store.results[it.path]) return
    await loadImagesFor(it)
    const c = store.results[it.path]
    // 兜底：加载未产生任何预览时标记为空数组，避免「加载原图预览」按钮一直停留
    if (c && c.imgUris === null && !imgLoading.has(it.path)) c.imgUris = []
  }

  // 接收主进程推送的识别进度
  let progressOff = null
  onMounted(() => {
    progressOff = window.api.onProgress(({ file, frac, desc }) => {
      const it = queue.find((x) => x.path === file)
      if (it && it.status === 'run') {
        it.progress = frac || 0
        it.desc = desc || ''
      }
    }) || null
  })
  onBeforeUnmount(() => { progressOff && progressOff() })

  return {
    queue, selectedPath, selectedItem, selectedResult, hasResult,
    pendingCount, stats,
    autoScan, autoFormats,
    pushFiles, addFiles, addFolder, onDrop, select, removeSelected, clearQueue,
    runOne, startBatch, stopBatch, exportMode, exportFor, openDownload,
    loadImagesFor, loadSelectedImages,
  }
}
