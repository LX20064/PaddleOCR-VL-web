// main.js —— Electron 主进程入口
// 职责：无边框 Mica 窗口、设置持久化、后端服务管理、Python sidecar 调度、IPC
const { app, BrowserWindow, ipcMain, dialog, shell, session, clipboard } = require('electron')
const path = require('path')
const fs = require('fs')
const os = require('os')
const { spawn } = require('child_process')

const { ServiceManager } = require('./services')

// ---------------- 路径解析（开发 / 打包共用） ----------------
const isPackaged = app.isPackaged
const appRoot = path.join(__dirname, '..')                       // win-app 根（dev）
const pyScriptsDir = isPackaged
  ? path.join(process.resourcesPath, 'python')
  : path.join(__dirname, 'python')
const projectRoot = path.join(__dirname, '..', '..')             // PaddleOCR-VL 根（dev 时）

// ---------------- 离线整合包环境（打包在 resources/offline，开发时 win-app/offline） ----------------
const offlineRoot = () => isPackaged
  ? path.join(process.resourcesPath, 'offline')
  : path.join(appRoot, 'offline')

function bundledPython() {
  return path.join(offlineRoot(), 'python', 'python.exe')
}

// 是否为离线整合包分发：offline 结构存在即视为整合包（无论完整性）。
// 与 bundledReady() 分离：整合包模式下只做「缺失项提示」，绝不引导在线下载安装。
function offlinePresent() {
  return fs.existsSync(bundledPython())
}

// 整合包是否完整可用：Python + llama-server + paddlex 模型 + 至少一个 GGUF 模型
function bundledReady() {
  const modelsDir = path.join(offlineRoot(), 'models', 'gguf')
  let hasModel = false
  try {
    hasModel = fs.existsSync(modelsDir) &&
      fs.readdirSync(modelsDir).some((f) => f.endsWith('.gguf'))
  } catch (_) { /* 目录不可读视为无模型 */ }
  return fs.existsSync(bundledPython()) &&
    fs.existsSync(path.join(offlineRoot(), 'llama.cpp', 'llama-server.exe')) &&
    fs.existsSync(path.join(offlineRoot(), 'paddlex-models')) &&
    hasModel
}

// ---------------- 设置持久化（无第三方依赖，直接写 JSON） ----------------
const settingsPath = () => path.join(app.getPath('userData'), 'settings.json')

const DEFAULT_SETTINGS = {
  apiUrl: 'http://127.0.0.1:8080/layout-parsing',
  timeout: 600,
  precision: 'fp16',
  device: 'auto',            // auto | gpu | cpu（auto 按显卡计算能力自动选择）
  paddleDevice: 'auto',      // Paddle 文档解析设备，与 device 平行（auto 同规则：CC≥7.5 用 GPU，否则 CPU）
  maxParallel: 1,
  keepServicesAfterQuit: false,
  rememberLastModule: false,          // 启动时恢复上次所在页面（默认关：总是打开首页）
  // VLM 与后端模型参数
  ctxSize: 32768,                    // llama-server 上下文长度
  noCudaGraph: false,                // 关闭 CUDA Graph（省显存但稍慢）
  pdfRenderDpi: 288,                 // PDF 渲染 DPI（影响识别清晰度与速度）
  // 默认保存位置（空串表示按下方默认规则生成）
  photoDir: '',                     // 图片：图片库/本机照片
  scanDir: '',                      // 扫描结果：文档/已扫描的文档
  ocrDir: '',                       // OCR 输出：文档/OCR扫描结果
  pdfOutDir: '',                    // 图片转 PDF 输出：文档/PDF输出
  // 识别参数默认值（直接映射产线 /layout-parsing 字段，None=后端默认）
  defaults: {
    use_seal: false,
    use_chart: false,
    use_orientation: false,
    use_unwarping: false,
    use_ocr_image_block: false,
    use_format_block: false,
    use_layout_mode: true,
    use_merge_blocks: true,
    pdf_per_page: false,
    export_chart: false,
    max_pixels: 0,
    min_pixels: 0,
    max_new_tokens: 16384,
    repetition_penalty: 1.0,
    cache_keep_days: 3,
    // 输出行为
    keep_source_dir: false,            // 保持来源子目录结构（相对路径输出）
    skip_existing: false,              // 跳过已存在的结果文件
  },
}

function readSettings() {
  const base = JSON.parse(JSON.stringify(DEFAULT_SETTINGS))
  try {
    const f = settingsPath()
    if (fs.existsSync(f)) {
      const user = JSON.parse(fs.readFileSync(f, 'utf-8'))
      base.apiUrl = user.apiUrl || base.apiUrl
      base.timeout = Number(user.timeout) || base.timeout
      base.precision = user.precision || base.precision
      base.device = user.device || base.device
      base.paddleDevice = user.paddleDevice || base.paddleDevice
      base.maxParallel = Number(user.maxParallel) || 1
      base.keepServicesAfterQuit = !!user.keepServicesAfterQuit
      base.rememberLastModule = !!user.rememberLastModule
      base.ctxSize = Number(user.ctxSize) || base.ctxSize
      base.noCudaGraph = !!user.noCudaGraph
      base.pdfRenderDpi = Number(user.pdfRenderDpi) || base.pdfRenderDpi
      base.photoDir = user.photoDir || ''
      base.scanDir = user.scanDir || ''
      base.ocrDir = user.ocrDir || ''
      base.defaults = Object.assign(base.defaults, user.defaults || {})
    }
  } catch (e) { /* 忽略损坏配置 */ }
  // 限制在合理范围，避免用户乱填把服务搞挂
  base.ctxSize = Math.max(1024, Math.min(131072, base.ctxSize))
  base.pdfRenderDpi = Math.max(72, Math.min(600, base.pdfRenderDpi))
  base.timeout = Math.max(10, Math.min(3600, base.timeout))
  base.maxParallel = Math.max(1, Math.min(8, base.maxParallel))
  return base
}

function saveSettings(partial) {
  const cur = readSettings()
  const merged = deepMerge(cur, partial || {})
  fs.mkdirSync(path.dirname(settingsPath()), { recursive: true })
  fs.writeFileSync(settingsPath(), JSON.stringify(merged, null, 2), 'utf-8')
  return merged
}

function deepMerge(base, override) {
  const out = { ...base }
  for (const k of Object.keys(override)) {
    const v = override[k]
    if (v === null) {
      // null 值 = 恢复默认：删除对应键（顶层与嵌套一致），readSettings 会回退到默认值
      delete out[k]
    } else if (v && typeof v === 'object' && !Array.isArray(v) && base[k] && typeof base[k] === 'object' && !Array.isArray(base[k])) {
      out[k] = deepMerge(base[k], v)
    } else {
      out[k] = v
    }
  }
  return out
}

// ---------------- 路径帮助 ----------------
function backendDir() {
  // 整合包模式：Python / llama.cpp / 模型 全部来自包内离线环境
  return offlineRoot()
}
function venvPython() {
  return bundledPython()
}
function resolvePaddleRoot() {
  // 打包版：资源目录内自带 PaddleOCR-VL.yaml（服务启动用）与 wordrender.py；开发时用项目根
  return isPackaged ? path.join(process.resourcesPath, 'backend') : projectRoot
}

// scan_worker 需要 wordrender.py 可导入（打包版在 resources/python，开发版在项目根）
function parsePythonRoot() {
  return isPackaged ? pyScriptsDir : resolvePaddleRoot()
}

// 确保目录存在
function ensureDir(d) {
  try { fs.mkdirSync(d, { recursive: true }) } catch (_) { /* 忽略 */ }
  return d
}

// 默认保存位置（读取设置，未设置则按规则生成，并自动创建目录）
function defaultPhotoDir() {
  const s = readSettings()
  const d = s.photoDir || path.join(app.getPath('pictures'), '本机照片')
  return ensureDir(d)
}
function defaultScanDir() {
  const s = readSettings()
  const d = s.scanDir || path.join(app.getPath('documents'), '已扫描的文档')
  return ensureDir(d)
}
function defaultOcrDir() {
  const s = readSettings()
  const d = s.ocrDir || path.join(app.getPath('documents'), 'OCR扫描结果')
  return ensureDir(d)
}
function defaultPdfDir() {
  const s = readSettings()
  const d = s.pdfOutDir || path.join(app.getPath('documents'), 'PDF输出')
  return ensureDir(d)
}

// 解析结果输出目录（使用设置中的 OCR 输出目录）
function outputsRoot() {
  return defaultOcrDir()
}

// ---------------- 服务管理器 ----------------
const services = new ServiceManager({
  getSettings: readSettings,
  getBackendDir: backendDir,
  getVenvPython: venvPython,
  getPaddleRoot: resolvePaddleRoot,
  isBundled: offlinePresent,
  getUserData: () => app.getPath('userData'),
  emit: (payload) => {
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents.send('ev:backend-status', payload)
    }
  },
})

// ---------------- 窗口 ----------------
let mainWindow = null
function createWindow() {
  // Win11：非透明窗口才能获得 DWM 圆角 + 系统 Acrylic 材质（透明窗口两者都会被忽略）
  // Win10 1809+：Electron 无系统材质 API，走透明窗口 + SetWindowCompositionAttribute(ACCENT) 实现亚克力
  const win11 = Number(os.release().split('.')[2] || 0) >= 22000
  const win = new BrowserWindow({
    width: 1380,
    height: 900,
    minWidth: 1000,
    minHeight: 680,
    frame: false,
    roundedCorners: win11,   // Win11 系统圆角；Win10 由 CSS 圆角模拟
    hasShadow: true,         // 系统阴影，圆角轮廓更自然
    transparent: !win11,     // Win11 关透明（让材质+圆角生效）；Win10 开透明（配合 ACCENT 亚克力）
    backgroundColor: '#00000000', // 必须透明，否则 DWM 亚克力会被纯色遮盖
    backgroundMaterial: win11 ? 'acrylic' : undefined, // 构造时直接声明，比事后调用更可靠
    icon: isPackaged ? path.join(process.resourcesPath, 'icon.ico') : path.join(appRoot, 'build', 'icon.ico'),
    show: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  })

  // 窗口背景材质：Win11 用 Electron 原生 Acrylic；Win10 1809+ 用 SetWindowCompositionAttribute 实现
  const { applyWindowMaterial, bindMaterialRefresh } = require('./acrylic')
  applyWindowMaterial(win)
  bindMaterialRefresh(win)

  win.once('ready-to-show', () => win.show())

  const devUrl = process.env.VITE_DEV_SERVER_URL
  if (devUrl) {
    win.loadURL(devUrl)
  } else {
    win.loadFile(path.join(appRoot, 'dist', 'index.html'))
  }
  mainWindow = win
  win.on('closed', () => { mainWindow = null })

  // 应用启动时：若离线整合包完整，则后台自动预热模型（llama-server + paddlex）
  // 预热不阻塞 UI，模型加载完成后首次识别可直接复用，显著降低等待时间
  if (bundledReady()) {
    console.log('[warmup] 检测到完整离线包，开始后台预热模型...')
    services.start().then((r) => {
      console.log('[warmup]', r.ok ? '预热成功' : ('预热失败：' + r.error))
    }).catch((e) => {
      console.error('[warmup] 预热异常', e)
    })
  }
}

// 窗口控制
ipcMain.on('win:min', () => mainWindow && mainWindow.minimize())
ipcMain.on('win:max', () => {
  if (!mainWindow) return
  mainWindow.isMaximized() ? mainWindow.unmaximize() : mainWindow.maximize()
})
ipcMain.on('win:close', () => {
  if (mainWindow && mainWindow.isFullScreen()) mainWindow.setFullScreen(false)
  mainWindow && mainWindow.close()
})
// 致命错误场景（如整合包缺文件）：直接退出应用，引导用户重新安装
ipcMain.on('app:quit', () => app.quit())

// ---------------- Python sidecar 执行 ----------------
// 登记所有存活的 sidecar 子进程，退出时统一清理，避免识别中途关窗口遗留孤儿 python.exe
const activeChildren = new Set()

function runPython(args, { env = {}, stdin = null, onEvent = null, timeout = 0 } = {}) {
  // 启动一个 venv python 脚本，按 JSONL 行流式返回；stdout 每行一个 JSON 事件
  // stdin：需要向脚本传入 JSON 时使用（避免 JSON 经 Windows 命令行传递时引号被破坏）
  // onEvent：非 result 事件（如 progress）解析后立即回调，实现真正的流式转发
  // timeout：毫秒；超过时限（如 WIA 驱动挂起）强制 kill 子进程并以超时错误 reject，
  //          防止 UI 永久 loading。默认 0 = 不限时（保持原行为）。
  return new Promise((resolve, reject) => {
    const py = venvPython()
    if (!fs.existsSync(py)) {
      reject(new Error('后端环境未就绪：未找到 ' + py + '。请确认离线整合包完整后重新启动应用。'))
      return
    }
    const child = spawn(py, args, {
      cwd: isPackaged ? app.getPath('userData') : appRoot,
      env: { ...process.env, PYTHONIOENCODING: 'utf-8', ...env },
      windowsHide: true,
      stdio: ['pipe', 'pipe', 'pipe'],
    })
    activeChildren.add(child)
    // stdin 流在进程启动失败（如 python.exe 被杀软拦截）时会 emit error(EPIPE)，
    // 未捕获会直接 throw 导致主进程崩溃。
    child.stdin.on('error', () => {})
    if (stdin !== null) {
      child.stdin.write(stdin)
    }
    child.stdin.end()
    let buf = ''
    let final = null
    let settled = false
    const events = []
    let timer = null
    if (timeout > 0) {
      timer = setTimeout(() => {
        if (settled) return
        settled = true
        activeChildren.delete(child)
        try { child.kill() } catch (e) { /* 进程可能已退出 */ }
        const mins = Math.round((timeout / 60000) * 10) / 10
        reject(new Error(`操作超时（${mins} 分钟无响应），已中止。请检查设备与驱动状态后重试。`))
      }, timeout)
    }
    child.stdout.on('data', (d) => {
      buf += d.toString('utf-8')
      let idx
      while ((idx = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, idx).trim()
        buf = buf.slice(idx + 1)
        if (!line) continue
        try {
          const ev = JSON.parse(line)
          if (ev.t === 'result') final = ev
          else {
            events.push(ev)
            if (onEvent) onEvent(ev)
          }
        } catch (e) { /* 忽略非 JSON 行 */ }
      }
    })
    child.stderr.on('data', (d) => {
      const msg = d.toString('utf-8').trim()
      if (msg && mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.webContents.send('ev:log', { kind: 'python', msg })
      }
    })
    child.on('error', (e) => {
      if (settled) return
      settled = true
      if (timer) clearTimeout(timer)
      activeChildren.delete(child)
      reject(e)
    })
    child.on('close', (code) => {
      if (settled) return
      settled = true
      if (timer) clearTimeout(timer)
      activeChildren.delete(child)
      if (final) resolve({ ...final, events })
      else reject(new Error(`Python 脚本退出码 ${code}`))
    })
  })
}

// ---------------- IPC：系统 / 设置 ----------------
ipcMain.handle('sys:info', async () => {
  const gpu = await services.probeGpu()
  return {
    platform: process.platform,
    isPackaged,
    appVersion: app.getVersion(),
    electron: process.versions.electron,
    node: process.versions.node,
    gpu,
    winBuild: Number(os.release().split('.')[2] || 0), // Windows build 号（22000=Win11）
  }
})
ipcMain.handle('settings:get', () => readSettings())
ipcMain.handle('settings:save', (_e, partial) => {
  const merged = saveSettings(partial)
  // 服务地址等变更后立即刷新后端状态
  services.emitStatus()
  return merged
})
// 获取三处默认保存位置（返回前自动创建目录）
ipcMain.handle('sys:default-dirs', () => ({
  photoDir: defaultPhotoDir(),
  scanDir: defaultScanDir(),
  ocrDir: defaultOcrDir(),
  pdfOutDir: defaultPdfDir(),
}))
ipcMain.handle('sys:reveal', (_e, p) => { if (p) shell.showItemInFolder(String(p)) })
// 在系统文件浏览器中打开目录（跨平台：shell.openPath 对 Windows / macOS / Linux 均可用）。
// 目录不存在时先创建再打开，返回结构化结果便于前端提示。
ipcMain.handle('sys:open-dir', async (_e, dir) => {
  if (!dir || typeof dir !== 'string') return { ok: false, error: '未指定目录' }
  try {
    if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true })
    const err = await shell.openPath(dir)
    return err ? { ok: false, error: err } : { ok: true }
  } catch (e) {
    return { ok: false, error: e.message }
  }
})
ipcMain.handle('sys:open-external', (_e, url) => {
  // 仅放行 http/https 外链，避免渲染进程传 file:// 等协议打开本地程序
  if (url && /^https?:\/\//i.test(String(url))) shell.openExternal(String(url))
})

// ---------------- IPC：文件对话框 ----------------
ipcMain.handle('dlg:files', async (_e, opts) => {
  // opts.images = true 时仅允许选择图片（工具箱"图片→PDF/格式转换"场景，避免误选 PDF）
  const imagesOnly = !!(opts && opts.images)
  const r = await dialog.showOpenDialog(mainWindow, {
    properties: ['openFile', 'multiSelections'],
    filters: imagesOnly
      ? [{ name: '图片', extensions: ['jpg', 'jpeg', 'png', 'bmp', 'tif', 'tiff', 'webp'] }]
      : [{ name: '图片与 PDF', extensions: ['jpg', 'jpeg', 'png', 'bmp', 'tif', 'tiff', 'webp', 'pdf'] }],
  })
  return r.canceled ? [] : r.filePaths
})
ipcMain.handle('dlg:folder', async () => {
  const r = await dialog.showOpenDialog(mainWindow, {
    properties: ['openDirectory'],
  })
  return r.canceled ? null : r.filePaths[0]
})
ipcMain.handle('dlg:dir', async () => {
  const r = await dialog.showOpenDialog(mainWindow, { properties: ['openDirectory', 'createDirectory'] })
  return r.canceled ? null : r.filePaths[0]
})

// 列举目录下受支持的文件（含子目录）
const SUPPORTED_EXTS = new Set(['.jpg', '.jpeg', '.png', '.bmp', '.tif', '.tiff', '.webp', '.pdf'])
ipcMain.handle('fs:list-files', (_e, dir, recursive) => {
  try {
    const out = []
    const walk = (d) => {
      for (const f of fs.readdirSync(d, { withFileTypes: true })) {
        // 跳过符号链接/junction，防止 Windows 目录成环导致无限递归
        if (f.isSymbolicLink()) continue
        const full = path.join(d, f.name)
        if (f.isDirectory()) {
          if (recursive) walk(full)
        } else if (SUPPORTED_EXTS.has(path.extname(f.name).toLowerCase())) {
          out.push(full)
        }
      }
    }
    walk(dir)
    return out
  } catch (e) { return [] }
})

// 图片 → base64 data URI（渲染进程无法直接读本地文件）
const MIME_BY_EXT = { '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp', '.bmp': 'image/bmp', '.tif': 'image/tiff', '.tiff': 'image/tiff' }
ipcMain.handle('img:data', async (_e, p) => {
  try {
    let file = String(p)
    const ext = path.extname(file).toLowerCase()
    // 浏览器 <img> 不支持 TIFF：先用 PIL 转成 PNG 再返回，否则大图/缩略图会破图
    if (ext === '.tif' || ext === '.tiff') {
      const tmp = path.join(app.getPath('temp'), `paddleocr_tif2png_${Date.now()}.png`)
      const r = await runPython([path.join(pyScriptsDir, 'image_tool.py'), '--in', file, '--convert', 'png', '--out', tmp, '--ops', '-'], { stdin: JSON.stringify([]) })
      if (!r.ok) return null
      file = tmp
    }
    const buf = fs.readFileSync(file)
    const mime = MIME_BY_EXT[path.extname(file).toLowerCase()] || 'image/png'
    return `data:${mime};base64,${buf.toString('base64')}`
  } catch (e) { return null }
})

// ---------------- IPC：后端服务管理 ----------------
ipcMain.handle('backend:status', () => services.status())
ipcMain.handle('backend:start', async () => {
  const r = await services.start()
  return r
})
ipcMain.handle('backend:warmup', async () => {
  const r = await services.start()
  return r
})
ipcMain.handle('backend:stop', async () => services.stop())
ipcMain.handle('backend:log', (_e, kind) => services.readLog(kind))

// ---------------- IPC：解析 / 预览 / 扫描仪 ----------------
ipcMain.handle('scan:parse', async (_e, req) => {
  // 请求 JSON 经 stdin 传入（Windows 命令行传参会被引号破坏）
  const args = [path.join(pyScriptsDir, 'scan_worker.py'), 'parse', req.filePath, '-', parsePythonRoot(), outputsRoot()]
  return runPython(args, {
    stdin: JSON.stringify(req),
    onEvent: (ev) => {
      if (ev.t === 'progress' && mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.webContents.send('ev:progress', { file: req.filePath, ...ev })
      }
    },
  })
})

// 用已缓存的识别结果（md/images）直接导出，跳过重新识别，避免每次导出都重跑 VLM
ipcMain.handle('export:render', async (_e, req) => {
  const args = [path.join(pyScriptsDir, 'scan_worker.py'), 'render', req.filePath, '-', parsePythonRoot(), outputsRoot()]
  return runPython(args, {
    stdin: JSON.stringify(req),
    onEvent: (ev) => {
      if (ev.t === 'progress' && mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.webContents.send('ev:progress', { file: req.filePath, ...ev })
      }
    },
  })
})

ipcMain.handle('scan:pdf-preview', async (_e, pdfPath, maxPages) => {
  // 预览图写入系统临时目录（paddleocr_preview_ 前缀），退出时统一清理，避免在 PDF 旁残留 .pdf_preview
  const previewDir = path.join(os.tmpdir(), `paddleocr_preview_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`)
  const args = [path.join(pyScriptsDir, 'pdf_preview.py'), pdfPath, previewDir]
  // 可选 --max-pages N：只渲染前 N 页（大图预览仅需首页，避免整本 PDF 全量渲染）
  if (maxPages) args.push('--max-pages', String(maxPages))
  return runPython(args)
})

// 复制文本到剪贴板（生产环境 file:// 非安全上下文下 navigator.clipboard 不可用）
ipcMain.handle('clipboard:write', (_e, text) => {
  clipboard.writeText(String(text || ''))
})

// ---------------- IPC：扫描仪 ----------------
ipcMain.handle('scan:devices', async () => {
  const p = runPython([path.join(pyScriptsDir, 'scan_device.py'), 'devices'])
  return p.then((r) => r)
})

// 默认扫描保存位置：文档/已扫描的文档
ipcMain.handle('scan:default-dir', async () => defaultScanDir())

ipcMain.handle('scan:preview', async (_e, devId, dpi) => {
  const args = [path.join(pyScriptsDir, 'scan_device.py'), 'preview', String(devId), '--dpi', String(dpi || 100)]
  // 预览通常几秒内返回；实测 300dpi 约 90 秒。5 分钟超时防止 WIA 驱动挂起导致按钮永久 loading。
  // 失败（含超时）统一返回 {ok:false,error}，避免渲染进程收到 "Error invoking remote method" 前缀的脏文案。
  try {
    return await runPython(args, { timeout: 5 * 60 * 1000 })
  } catch (e) {
    return { ok: false, error: String((e && e.message) || e) }
  }
})

ipcMain.handle('scan:run', async (_e, opt) => {
  const args = [
    path.join(pyScriptsDir, 'scan_device.py'), 'scan', String(opt.deviceId),
    '--dpi', String(opt.dpi), '--mode', opt.mode, '--format', opt.format,
    '--source', opt.source, '--outdir', opt.outdir,
    ...(opt.pages ? ['--pages', String(opt.pages)] : []),
    ...(opt.enhance ? ['--enhance'] : []),
    ...(opt.name ? ['--name', opt.name] : []),
  ]
  try {
    return await runPython(args, {
      // 整卷 ADF 多页 + 增强处理较慢（实测单页 300dpi 约 90 秒），30 分钟为硬上限，
      // 仅用于拦截驱动挂起等无响应场景，正常扫描不受影响
      timeout: 30 * 60 * 1000,
      onEvent: (ev) => {
        if (ev.t === 'progress' && mainWindow && !mainWindow.isDestroyed()) {
          mainWindow.webContents.send('ev:scan-progress', ev)
        }
      },
    })
  } catch (e) {
    return { ok: false, error: String((e && e.message) || e) }
  }
})

// ---------------- IPC：PDF 合并（扫描后免 OCR 直接导出） ----------------
ipcMain.handle('pdf:merge', async (_e, opt) => {
  const args = [
    path.join(pyScriptsDir, 'merge_to_pdf.py'), '--out', String(opt.out),
    '--dpi', String(opt.dpi || 300), ...(opt.pages || []),
  ]
  return runPython(args)
})

// ---------------- IPC：图像工具（旋转/裁剪/调色/滤镜/格式转换/扫描仪效果） ----------------
ipcMain.handle('img:apply', async (_e, opt) => {
  // 用 path.extname 取源文件扩展名（split('.') 会被目录名中的点误导）
  const ext = (opt.convert || path.extname(String(opt.src || '')).slice(1) || 'png').replace('jpeg', 'jpg')
  const out = opt.out || path.join(app.getPath('temp'),
    `paddleocr_imgtool_${Date.now()}_${Math.random().toString(36).slice(2, 6)}.${ext}`)
  const args = [
    path.join(pyScriptsDir, 'image_tool.py'), '--in', String(opt.src),
    '--ops', '-', '--out', out,
  ]
  if (opt.convert) args.push('--convert', String(opt.convert))
  // 扫描仪效果的"自动转正 / PPDocLayout 智能取景"需要 paddlex 服务
  const apiUrl = (opt.api || readSettings().apiUrl || '').trim()
  if (apiUrl) args.push('--api', apiUrl)
  return runPython(args, { stdin: JSON.stringify(opt.ops || []) })
})

// 保存图片数据（摄像头拍照等场景：渲染进程只有 dataURL）
ipcMain.handle('img:save-data', async (_e, opt) => {
  try {
    const data = String(opt.data || '')
    const m = data.match(/^data:image\/(\w+);base64,(.+)$/s)
    if (!m) return { ok: false, error: '无效的图片数据' }
    const ext = (m[1] === 'jpeg' ? 'jpg' : m[1]) || 'png'
    const dir = opt.dir || app.getPath('temp')
    fs.mkdirSync(dir, { recursive: true })
    // path.basename 防路径穿越（name 含 ../ 会被剥离），文件名不允许带目录
    const name = path.basename(opt.name || 'paddleocr_cam_' + Date.now()) + '.' + ext
    let file = path.join(dir, name)
    if (fs.existsSync(file)) file = path.join(dir, Date.now() + '_' + name)
    fs.writeFileSync(file, Buffer.from(m[2], 'base64'))
    return { ok: true, file }
  } catch (e) {
    return { ok: false, error: String(e && e.message || e) }
  }
})

// 判断路径是否存在（渲染进程用于避免覆盖重名文件等场景）
ipcMain.handle('sys:path-exists', (_e, p) => {
  try { return !!p && fs.existsSync(String(p)) } catch (_) { return false }
})

// ---------------- IPC：保存对话框 ----------------
ipcMain.handle('dlg:save', async (_e, opt) => {
  const r = await dialog.showSaveDialog(mainWindow, {
    title: opt.title || '保存',
    defaultPath: opt.defaultPath || 'untitled',
    filters: opt.filters || [{ name: '文件', extensions: ['*'] }],
  })
  return r.canceled ? null : r.filePath
})

// ---------------- IPC：环境安装向导 ----------------
ipcMain.handle('setup:status', async () => {
  const py = venvPython()
  const exists = fs.existsSync(py)
  const models = services.findModels()
  return {
    venvReady: exists,
    pythonExe: py,
    llamaReady: fs.existsSync(services.findLlamaServer()),
    models,
    backendDir: backendDir(),
    gpu: await services.probeGpu(),
    bundled: offlinePresent(),
    bundledComplete: bundledReady(),
    bundledDir: offlineRoot(),
  }
})

// 关键文件缺失 → 弹出系统级原生模态对话框（Windows MB_ICONERROR 风格，兼容 Win10/11）
ipcMain.handle('sys:missing-files', async () => {
  if (!mainWindow || mainWindow.isDestroyed()) return null
  const { response } = await dialog.showMessageBox(mainWindow, {
    type: 'error',                                   // 错误图标（标准红色叉）
    title: '文件缺失错误',
    message: '应用缺少必要文件，无法正常使用。请重新安装应用程序以恢复完整文件。',
    buttons: ['确定'],
    defaultId: 0,
    cancelId: 0,
    noLink: true,                                    // 传统按钮，避免命令链接样式跨系统不一致
  })
  return response
})

// 扫描/预览/图像工具/拍照产生的临时文件落在 %TEMP% 下，应用退出时统一清理，避免残留累积。
function cleanupTempScans() {
  try {
    const tmp = os.tmpdir()
    for (const name of fs.readdirSync(tmp)) {
      if (name.startsWith('paddleocr_scan_') || name.startsWith('paddleocr_preview_') ||
          name.startsWith('paddleocr_imgtool_') || name.startsWith('paddleocr_cam_') ||
          name.startsWith('paddleocr_tif2png_')) {
        fs.rmSync(path.join(tmp, name), { recursive: true, force: true })
      }
    }
  } catch (_) { /* 清理失败可忽略 */ }
  // PDF 页面预览缩略图缓存（OCR 输出目录/.previews）随退出清空：
  // 预览图随时可按需重新生成，长期累积无意义。killActiveChildren 已先执行，无进程正在写入。
  try {
    fs.rmSync(path.join(defaultOcrDir(), '.previews'), { recursive: true, force: true })
  } catch (_) { /* 清理失败可忽略 */ }
}

// 退出时杀掉仍存活的 Python sidecar 子进程，避免遗留孤儿 python.exe 继续写已清理的目录
function killActiveChildren() {
  for (const child of activeChildren) {
    try { child.kill() } catch (_) { /* 已退出 */ }
  }
  activeChildren.clear()
}

// ---------------- 生命周期 ----------------
const gotLock = app.requestSingleInstanceLock()
if (!gotLock) {
  app.quit()
} else {
  // 注意：不要调用 app.setAppUserModelId！
  // 设置 AUMID 后 Windows 任务栏会放弃使用窗口图标，转而查找该 AUMID 关联的
  // 已注册快捷方式/图标；便携版/免安装运行时没有注册快捷方式，任务栏会回退为
  // 「通用可执行文件」图标。应用为单实例（requestSingleInstanceLock），无分组
  // 需求，不设置 AUMID 时任务栏直接采用窗口图标（自定义蓝色），显示正常。
  app.on('second-instance', () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore()
      mainWindow.focus()
    }
  })

  app.whenReady().then(() => {
    // 启动时清理上次异常退出（崩溃/强制结束）残留的临时文件：
    // 正常退出已在 before-quit 清理；此处窗口创建前兜底清一次，避免残留长期累积。
    // 此刻无任何 sidecar 进程运行，%TEMP%\paddleocr_* 与 outputs\.previews 均为可再生成的临时产物。
    cleanupTempScans()

    // 摄像头 / 麦克风权限：允许渲染进程访问摄像头（拍照选项卡）
    const ses = session.defaultSession
    ses.setPermissionRequestHandler((_wc, permission, cb) => {
      cb(permission === 'media' || permission === 'mediaKeySystem')
    })
    ses.setPermissionCheckHandler((_wc, permission) =>
      permission === 'media' || permission === 'mediaKeySystem')

    createWindow()
    // 定期刷新后端状态（渲染进程也会轮询，这里兜底推送）
    setInterval(() => services.emitStatus(), 5000)
    // 环境已就绪时自动拉起 OCR 服务（llama-server + paddlex serve），扫描后即可直接识别
    if (fs.existsSync(venvPython())) {
      services.start().then((r) => {
        if (!r.ok) console.warn('[auto-start] OCR 服务启动失败：' + (r.error || ''))
      })
    }
    app.on('activate', () => {
      if (BrowserWindow.getAllWindows().length === 0) createWindow()
    })
  })

  app.on('window-all-closed', () => {
    const s = readSettings()
    if (!s.keepServicesAfterQuit) services.stop()
    if (process.platform !== 'darwin') app.quit()
  })

  app.on('before-quit', () => {
    const s = readSettings()
    if (!s.keepServicesAfterQuit) services.stop()
    killActiveChildren()
    cleanupTempScans()
  })
}
