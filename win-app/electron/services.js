// services.js —— 后端服务管理器（llama-server + paddlex serve）
const { spawn } = require('child_process')
const fs = require('fs')
const path = require('path')
const http = require('http')

const API_PORT = 8080
const LLAMA_PORT = 8081

class ServiceManager {
  constructor(deps) {
    this.deps = deps
    this.procs = {}   // llama / api
    this.latched = { llama: false, api: false }  // 端口曾就绪（进程存活期间保持，识别中不被误判）
    this.quantizing = null  // 正在自动量化的目标精度（null=无）
    this.quantizePromise = null  // 量化子进程 promise（防同精度并发量化）
    this.quantizeTarget = null
    this.startPromise = null  // 在途启动 promise（防并发启动重复拉起服务进程）
    this.cpuFallback = { llama: false, api: false }  // GPU 启动/运行失败后已自动回退 CPU（本会话内只回退一次）
    this.apiWatch = null      // api 日志监控定时器（检测 parallel_for failed → 自动 CPU 回退）
    this.apiWatchPos = 0      // api.log 已读偏移
  }

  // ---------- 日志防膨胀 ----------
  // 日志长期追加无轮转，异常场景（崩溃循环刷屏、GPU 错误刷堆栈）可能短期暴涨。
  // 每次打开日志前调用：超过 maxBytes 时只保留末尾 keepBytes，其余丢弃（绝对封顶）。
  rotateLogIfNeeded(logPath, maxBytes = 10 * 1024 * 1024, keepBytes = 1024 * 1024) {
    try {
      const stat = fs.statSync(logPath)
      if (stat.size <= maxBytes) return
      const keep = Math.min(keepBytes, stat.size)
      const buf = Buffer.alloc(keep)
      const fd = fs.openSync(logPath, 'r+')
      try {
        fs.readSync(fd, buf, 0, keep, stat.size - keep)
        fs.ftruncateSync(fd, 0)
        fs.writeSync(fd, buf, 0, keep, 0)
      } finally {
        fs.closeSync(fd)
      }
    } catch (_) { /* 文件不存在或瞬时 IO 错误：跳过轮转，不阻塞启动 */ }
  }

  // ---------- GPU 探测 ----------
  probeGpu() {
    // 返回 { name, computeCap, mem, driver }；无 NVIDIA 卡或 nvidia-smi 不可用时返回 null
    return new Promise((resolve) => {
      let done = false
      const finish = (v) => { if (!done) { done = true; resolve(v) } }
      const child = spawn('nvidia-smi',
        ['--query-gpu=name,compute_cap,memory.total,driver_version', '--format=csv,noheader'],
        { windowsHide: true })
      child.stdout.on('data', (d) => {
        const line = d.toString('utf-8').trim().split('\n')[0]
        if (line) {
          const [name, cap, mem, driver] = line.split(',').map((s) => s.trim())
          finish({ name, computeCap: parseFloat(cap) || 0, mem, driver })
        } else finish(null)
      })
      child.on('error', () => finish(null))
      child.on('close', () => finish(null))
      setTimeout(() => finish(null), 6000)
    })
  }

  // ---------- 路径 ----------
  findLlamaServer() {
    // 整合包模式：llama.cpp 固定位于 offline/llama.cpp/llama-server.exe
    return path.join(this.deps.getBackendDir(), 'llama.cpp', 'llama-server.exe')
  }

  findQuantize() {
    // llama-quantize.exe 与 llama-server.exe 同目录（llama.cpp 发行包内含）
    return path.join(this.deps.getBackendDir(), 'llama.cpp', 'llama-quantize.exe')
  }

  // 运行时写入目录统一放用户数据目录（%APPDATA%\paddleocr-vl-win）：
  // 安装到 Program Files 后 resources 目录只读，向 resources\offline 写日志/量化产物会抛 EPERM。
  runtimeLogDir() {
    return path.join(this.deps.getUserData(), 'logs')
  }
  // 量化产物（llama-quantize 生成）写入用户数据目录；内置只读模型仍从 resources 读取
  runtimeModelsDir() {
    return path.join(this.deps.getUserData(), 'models', 'gguf')
  }
  bundledModelsDir() {
    return path.join(this.deps.getBackendDir(), 'models', 'gguf')
  }

  findModels() {
    // 返回 [{ precision, gguf, mmproj, ready, quantizable }]
    // 基座只保留 FP16（PaddleOCR-VL-1.6-fp16.gguf + mmproj）；q4_k_m / q5_k_m / q8_0
    // 不随安装包分发，首次选用时由 llama-quantize 从 FP16 基座自动量化（quantizable=true）。
    // 内置模型位于 resources（只读）；量化产物位于用户数据目录（可写，Program Files 安装不报 EPERM）。
    const bundled = this.bundledModelsDir()
    const runtime = this.runtimeModelsDir()
    const mmproj = path.join(bundled, 'PaddleOCR-VL-1.6-mmproj.gguf')
    const fp16 = path.join(bundled, 'PaddleOCR-VL-1.6-fp16.gguf')
    const hasBase = fs.existsSync(fp16) && fs.existsSync(mmproj)
    const out = []
    for (const p of ['fp16', 'q4_k_m', 'q5_k_m', 'q8_0']) {
      // 同精度产物优先取运行目录（用户量化过的），其次取内置目录
      const ggufRun = path.join(runtime, `PaddleOCR-VL-1.6-${p}.gguf`)
      const ggufBundled = path.join(bundled, `PaddleOCR-VL-1.6-${p}.gguf`)
      const gguf = fs.existsSync(ggufRun) ? ggufRun : (fs.existsSync(ggufBundled) ? ggufBundled : null)
      const mmOk = fs.existsSync(mmproj)
      if (p === 'fp16') {
        if (hasBase) out.push({ precision: p, ready: true, quantizable: false, gguf, mmproj })
        else if (fs.existsSync(fp16) || mmOk) out.push({ precision: p, ready: false, quantizable: false, gguf: fs.existsSync(fp16) ? fp16 : null, mmproj: mmOk ? mmproj : null })
      } else if (gguf && mmOk) {
        out.push({ precision: p, ready: true, quantizable: hasBase, gguf, mmproj })
      } else if (hasBase) {
        // FP16 基座已就绪 → 该精度可按需自动量化
        out.push({ precision: p, ready: false, quantizable: true, gguf: null, mmproj })
      }
    }
    return out
  }

  // ---------- 状态 ----------
  async status() {
    const models = this.findModels()
    const s = this.deps.getSettings()
    const [apiUp, llamaUp] = await Promise.all([checkPort(API_PORT), checkPort(LLAMA_PORT)])
    const llamaAlive = !!this.procs.llama
    const apiAlive = !!this.procs.api
    // 端口探测到就绪后锁存：进程存活期间保持就绪。识别时 API / llama 事件循环繁忙，
    // 端口探测可能 1.5s 超时被误判为"未就绪"，导致状态栏在识别期间闪烁"等待 API 服务"。
    if (llamaUp) this.latched.llama = true
    if (apiUp) this.latched.api = true
    // 本进程服务：进程存活 且（端口可连 或 已锁存就绪）；外部服务（无本进程）：端口可连即就绪
    const llamaReady = llamaUp || (llamaAlive && this.latched.llama)
    const apiReady = apiUp || (apiAlive && this.latched.api)
    return {
      apiUp: apiReady,
      llamaUp: llamaReady,
      apiUrl: s.apiUrl,
      precision: s.precision,
      models,
      llamaServer: this.findLlamaServer(),
      quantizing: this.quantizing,
      ready: apiReady && llamaReady,
      procs: {
        llama: llamaAlive,
        api: apiAlive,
      },
      cpuFallback: { ...this.cpuFallback },
    }
  }

  emitStatus() {
    this.status().then((st) => this.deps.emit({ t: 'status', ...st })).catch(() => {})
  }

  // ---------- 启动 / 停止 ----------
  // 返回 Paddle venv 的 nvidia 运行时 bin 目录列表（存在才返回）。
  // llama.cpp 编译为 CUDA 12.9 版后不再自带 cudart/cublas DLL，改为复用 Paddle 的
  // nvidia 12.9 运行时，避免安装包同时携带 llama(cu13)+Paddle(cu12.9) 两套 CUDA
  // 运行时（省 ~0.5GB）。启动 llama-server 时把这些目录前置到 PATH 即可。
  nvidiaRuntimePaths() {
    const py = this.deps.getVenvPython()
    if (!py) return []
    // venv 结构：<offline>/python/Lib/site-packages/nvidia/*/bin
    const roots = [
      path.join(path.dirname(py), 'Lib', 'site-packages', 'nvidia'),
    ]
    const bins = []
    for (const root of roots) {
      if (!fs.existsSync(root)) continue
      for (const sub of fs.readdirSync(root)) {
        const bin = path.join(root, sub, 'bin')
        if (fs.existsSync(bin)) bins.push(bin)
      }
    }
    return bins
  }

  // 启动 llama-server（useGpu=false 时以 CPU 运行：-ngl 0 + 屏蔽 CUDA 设备）
  spawnLlama(useGpu, model) {
    const s = this.deps.getSettings()
    const ctxSize = Number(s.ctxSize) || 32768
    const llamaArgs = [
      '-m', model.gguf,
      '--mmproj', model.mmproj,
      // 上下文默认 32768：足够容纳大图视觉 token + 长公式 LaTeX 输出。
      // 4096 太小，图片视觉 token + 长公式 LaTeX 输出会超出上下文被截断，
      // 导致 \frac / \genfrac / cases 等公式被退化成线性文本。
      '-c', String(ctxSize),
      '--host', '127.0.0.1',
      '--port', String(LLAMA_PORT),
      '-ngl', useGpu ? '99' : '0',
      // 贪心解码：量化模型在默认温度 0.8 下输出不稳定（图片重复、\frac 误补全等），
      // 对齐 Docker 版 --temp 0。不设 repeat-penalty（参考版 repetition_penalty=1.0），
      // 因为 LaTeX 含大量重复的 \ { } frac 等 token，复读惩罚会使其退化为线性文本。
      '--temp', '0',
      // 注意：不再传 --log-disable —— 保留 llama 日志便于排查 GPU 启动失败等现场
    ]
    if (s.noCudaGraph) llamaArgs.push('--no-cuda-graph')
    const logsDir = this.runtimeLogDir()
    const llamaLogPath = path.join(logsDir, 'llama.log')
    this.rotateLogIfNeeded(llamaLogPath)
    const llamaLog = fs.openSync(llamaLogPath, 'a')
    // llama-server 复用 Paddle 的 CUDA 12.9 运行时：把 nvidia/*/bin 前置到 PATH，
    // 让 cudart64_12.dll / cublas64_12.dll 从 Paddle 的 venv 加载（llama 目录不再自带）。
    const nvidiaBins = this.nvidiaRuntimePaths()
    const llamaEnv = {
      ...process.env,
      ...(useGpu ? {} : { CUDA_VISIBLE_DEVICES: '' }),
    }
    if (nvidiaBins.length) llamaEnv.PATH = nvidiaBins.join(';') + ';' + (llamaEnv.PATH || '')
    this.procs.llama = spawn(this.findLlamaServer(), llamaArgs, {
      stdio: ['ignore', llamaLog, llamaLog],
      windowsHide: true,
      // CPU 模式：屏蔽 CUDA 设备，让 CUDA 版 llama-server 走纯 CPU 路径
      env: llamaEnv,
    })
    // 子进程已继承 fd，关闭父进程副本避免每次 start 泄漏一个文件描述符
    fs.closeSync(llamaLog)
    this.procs.llama.on('exit', () => { this.procs.llama = null; this.latched.llama = false; this.emitStatus() })
    this.procs.llama.on('error', () => { this.procs.llama = null; this.latched.llama = false; this.emitStatus() })
  }

  // 启动 paddlex serve（useGpuPaddle=false 时以 CPU 运行）
  spawnApi(useGpuPaddle, yaml, bundled, backend) {
    const s = this.deps.getSettings()
    const py = this.deps.getVenvPython()
    const logsDir = this.runtimeLogDir()
    const apiLogPath = path.join(logsDir, 'api.log')
    this.rotateLogIfNeeded(apiLogPath)
    const apiLog = fs.openSync(apiLogPath, 'a')
    // PDF 渲染 DPI → paddlex PDF 缩放系数（scale = dpi / 72）
    const pdfDpi = Number(s.pdfRenderDpi) || 288
    const pdfScale = Math.max(1, Math.min(10, pdfDpi / 72)).toFixed(2)
    this.procs.api = spawn(py, ['-m', 'paddlex', '--serve', '--pipeline', yaml, '--device', useGpuPaddle ? 'gpu:0' : 'cpu'], {
      stdio: ['ignore', apiLog, apiLog],
      windowsHide: true,
      cwd: bundled ? backend : undefined,
      env: { ...process.env, PADDLE_PDX_PDF_RENDER_SCALE: pdfScale },
    })
    // 子进程已继承 fd，关闭父进程副本避免每次 start 泄漏一个文件描述符
    fs.closeSync(apiLog)
    this.procs.api.on('exit', () => { this.procs.api = null; this.latched.api = false; this.emitStatus() })
    this.procs.api.on('error', () => { this.procs.api = null; this.latched.api = false; this.emitStatus() })
  }

  // ---------- api 日志监控（GPU parallel_for failed → 自动切 CPU 重启） ----------
  startApiWatch() {
    this.stopApiWatch()
    const logFile = path.join(this.runtimeLogDir(), 'api.log')
    // 记录当前文件大小作为起点，只增量读取新追加的内容
    try { this.apiWatchPos = fs.statSync(logFile).size } catch (_) { this.apiWatchPos = 0 }
    this.apiWatch = setInterval(() => {
      try {
        if (!this.procs.api) return
        const size = fs.statSync(logFile).size
        if (size < this.apiWatchPos) { this.apiWatchPos = size; return }  // 文件被重建/截断
        if (size === this.apiWatchPos) return
        const fd = fs.openSync(logFile, 'r')
        const buf = Buffer.alloc(size - this.apiWatchPos)
        fs.readSync(fd, buf, 0, buf.length, this.apiWatchPos)
        fs.closeSync(fd)
        this.apiWatchPos = size
        // OpenCV 的 OpenMP worker 在 GPU 模式下崩溃的特征关键字
        if (buf.toString('utf-8').includes('parallel_for failed')) {
          this.restartApiAsCpu()
        }
      } catch (_) { /* 瞬时 IO 错误忽略，下个周期重试 */ }
    }, 3000)
  }

  stopApiWatch() {
    if (this.apiWatch) { clearInterval(this.apiWatch); this.apiWatch = null }
  }

  // parallel_for failed 出现 → kill 当前 api，以 CPU 模式重启（仅一次）
  async restartApiAsCpu() {
    if (this.cpuFallback.api) return
    this.cpuFallback.api = true
    this.stopApiWatch()
    const old = this.procs.api
    if (!old) return
    this.latched.api = false
    try { old.kill() } catch (_) {}
    // 等待旧进程退出并释放端口后重启，避免端口仍被占用导致 bind 失败
    await new Promise((res) => {
      let done = false
      const finish = () => { if (!done) { done = true; res() } }
      old.once('exit', finish)
      old.once('error', finish)
      setTimeout(finish, 3000)
    })
    const s = this.deps.getSettings()
    const root = this.deps.getPaddleRoot()
    const bundled = this.deps.isBundled ? this.deps.isBundled() : false
    const backend = this.deps.getBackendDir()
    let yaml = path.join(root, 'PaddleOCR-VL.yaml')
    if (bundled) yaml = this.writeOfflineYaml(yaml, backend)
    this.procs.api = null
    this.spawnApi(false, yaml, bundled, backend)
    // 等待新 api 端口就绪后广播状态（供前端提示"已回退 CPU"）
    const spawned = this.procs.api
    await waitPorts([API_PORT], 300000, () => spawned && !this.procs.api)
    this.emitStatus()
  }

  async start() {
    // 互斥：启动预热 / 自动启动 / 用户点击可能并发触发，
    // 共享同一次启动流程，避免重复 spawn llama-server / paddlex 导致端口冲突。
    if (this.startPromise) return this.startPromise
    const run = this._start()
    this.startPromise = run
    try {
      return await run
    } finally {
      if (this.startPromise === run) this.startPromise = null
    }
  }

  async _start() {
    const llamaExe = this.findLlamaServer()
    const py = this.deps.getVenvPython()
    const root = this.deps.getPaddleRoot()
    const s = this.deps.getSettings()
    const bundled = this.deps.isBundled ? this.deps.isBundled() : false
    const backend = this.deps.getBackendDir()
    const logsDir = this.runtimeLogDir()
    fs.mkdirSync(logsDir, { recursive: true })

    if (!fs.existsSync(llamaExe)) {
      return { ok: false, error: '未找到 llama-server.exe，请确认离线整合包完整。' }
    }
    if (!fs.existsSync(py)) {
      return { ok: false, error: '后端 Python 环境未就绪：' + py }
    }

    // ---- 端口占用检测：8080/8081 若已被占用，须是本应用保留的服务（PID 文件记录），
    //      否则报端口冲突，避免状态栏误显示"已就绪"却打到他人服务 ----
    const pids = this.readPids()
    const [apiUp0, llamaUp0] = await Promise.all([checkPort(API_PORT), checkPort(LLAMA_PORT)])
    // 端口可能被上次会话保留的服务占用：用 PID + 映像名双重校验，防止 PID 被系统回收复用
    const apiPidOk = await this.pidMatches(pids.api, 'python.exe')
    const llamaPidOk = await this.pidMatches(pids.llama, 'llama-server.exe')
    if (apiUp0 && !this.procs.api && !apiPidOk) {
      return { ok: false, error: `端口 ${API_PORT} 已被其他程序占用，请关闭占用该端口的程序后重试。` }
    }
    if (llamaUp0 && !this.procs.llama && !llamaPidOk) {
      return { ok: false, error: `端口 ${LLAMA_PORT} 已被其他程序占用，请关闭占用该端口的程序后重试。` }
    }
    // 各端口若被本应用保留的服务占用且进程存活 → 视为已就绪，直接复用，不再 spawn
    // （避免保留服务还活着时又拉起新进程，导致新进程 bind 端口失败 WinError 10048）
    const apiReuse = apiUp0 && apiPidOk && !this.procs.api
    const llamaReuse = llamaUp0 && llamaPidOk && !this.procs.llama
    if (apiReuse) this.latched.api = true
    if (llamaReuse) this.latched.llama = true
    if (apiReuse && llamaReuse) {
      this.emitStatus()
      return { ok: true, message: 'OCR 服务已就绪（复用上次保留的服务）' }
    }

    // ---- 模型就绪：仅内置 FP16 基座，其他精度首次选用时自动量化 ----
    let model = this.findModels().find((m) => m.precision === s.precision && m.ready)
    if (!model && s.precision !== 'fp16') {
      const m = this.findModels().find((x) => x.precision === s.precision)
      if (m && m.quantizable && (await this.ensureQuantized(s.precision))) {
        model = this.findModels().find((x) => x.precision === s.precision && x.ready)
      }
    }
    if (!model) {
      return { ok: false, error: `模型 PaddleOCR-VL-1.6-${s.precision} 未就绪（仅内置 FP16 基座，请确认 PaddleOCR-VL-1.6-fp16.gguf 与 mmproj 已就绪）。` }
    }

    // ---- 设备决策：llama 与 Paddle 平行，各自独立开关；无 CC≥7.5 的 NVIDIA 显卡自动回退 CPU ----
    const gpu = await this.probeGpu()
    const cap = gpu ? gpu.computeCap : 0
    const gpuOk = cap >= 7.5
    const decide = (pref) => (pref === 'auto' || pref === 'gpu' || pref === 'gpu:0') && gpuOk
    const prefLlama = String(s.device || 'auto').toLowerCase()
    const prefPaddle = String(s.paddleDevice || 'auto').toLowerCase()
    const useGpu = decide(prefLlama)
    const useGpuPaddle = decide(prefPaddle)
    const device = useGpu ? 'gpu:0' : 'cpu'
    const noGpuTip = gpu ? `CC ${gpu.computeCap}` : '无 NVIDIA GPU'
    let warn = ''
    if (prefLlama !== 'cpu' && !useGpu) {
      warn = `未检测到计算能力≥7.5 的 NVIDIA 显卡（当前 ${noGpuTip}），llama 已自动回退 CPU 推理。`
    }
    if (prefPaddle !== 'cpu' && !useGpuPaddle) {
      warn += (warn ? '\n' : '') + `未检测到计算能力≥7.5 的 NVIDIA 显卡（当前 ${noGpuTip}），Paddle 解析已自动回退 CPU。`
    }

    // ---- 产线配置：离线包将 model_dir 指向本地 paddlex-models（相对路径，cwd=offline 根）----
    let yaml = path.join(root, 'PaddleOCR-VL.yaml')
    if (bundled) yaml = this.writeOfflineYaml(yaml, backend)
    if (!fs.existsSync(yaml)) {
      return { ok: false, error: `未找到产线配置 ${yaml}，请确认项目目录正确。` }
    }

    // 1) llama-server（GPU/CPU 由调用方指定，GPU 启动失败时 _start 会自动以 CPU 重启）
    if (!this.procs.llama && !llamaReuse) {
      this.spawnLlama(useGpu, model)
    }

    // 2) paddlex serve（文档解析：布局/OCR 模型较小；设备由 paddleDevice 独立控制，
    //    与 llama 同样按 CC≥7.5 自动选 GPU、无则回退 CPU。注意：GPU 模式下曾遇到
    //    OpenCV parallel_for failed 崩溃，若复现可在设置中切回 CPU）
    if (!this.procs.api && !apiReuse) {
      this.spawnApi(useGpuPaddle, yaml, bundled, backend)
    }

    // 记录本实例服务的 PID（keepServicesAfterQuit 跨会话复用 / 停止时清理残留）
    this.writePids({
      llama: this.procs.llama ? this.procs.llama.pid : null,
      api: this.procs.api ? this.procs.api.pid : null,
    })

    this.emitStatus()
    // 等待端口就绪（最长 600s：首次加载模型较慢）。
    // 若任一刚 spawn 的服务进程在等待期间退出（模型损坏/显存不足/端口 bind 失败），
    // 立即失败返回，而非死等满 600s。
    const spawnedLlama = this.procs.llama
    const spawnedApi = this.procs.api
    const checkExited = () =>
      (spawnedLlama && !this.procs.llama) || (spawnedApi && !this.procs.api)
    let ok = await waitPorts([LLAMA_PORT, API_PORT], 600000, checkExited)

    // GPU 启动失败自动回退 CPU：llama 或 api 以 GPU 模式 spawn 后进程直接退出，
    // 说明该机器上 CUDA 环境不可用（架构不兼容/显存不足/驱动问题）。
    // 回退只做一次，且只对确实退出的那个服务重启，避免无谓地把正常服务降级。
    if (!ok && useGpu && spawnedLlama && !this.procs.llama && !this.cpuFallback.llama) {
      this.cpuFallback.llama = true
      this.spawnLlama(false, model)
      ok = await waitPorts([LLAMA_PORT, API_PORT], 600000, checkExited)
    }
    if (!ok && useGpuPaddle && spawnedApi && !this.procs.api && !this.cpuFallback.api) {
      this.cpuFallback.api = true
      this.spawnApi(false, yaml, bundled, backend)
      ok = await waitPorts([LLAMA_PORT, API_PORT], 600000, checkExited)
    }

    // 若 api 以 GPU 模式就绪，启动日志监控：OpenCV parallel_for failed 是运行时
    // 崩溃（进程不退），只能靠日志关键字捕获后自动切 CPU 重启。
    if (ok && useGpuPaddle && !this.cpuFallback.api) this.startApiWatch()

    this.emitStatus()
    let fb = ''
    if (this.cpuFallback.llama) fb += '\nllama 已自动回退 CPU 推理（GPU 启动失败）。'
    if (this.cpuFallback.api) fb += '\nPaddle 解析已自动回退 CPU（GPU 运行异常）。'
    return ok
      ? { ok: true, message: '后端服务已就绪（API 8080 / llama 8081）' + (warn ? '\n' + warn : '') + fb, device }
      : { ok: false, error: '服务启动超时，请查看设置中的服务日志。' + (warn ? '\n' + warn : '') }
  }

  // 离线包模式：生成一份 model_dir 指向本地 paddlex-models 的产线配置（相对路径，配合 cwd=offline 根）
  writeOfflineYaml(srcYaml, offlineRoot) {
    let out = fs.readFileSync(srcYaml, 'utf-8')
    const subs = [
      ['PP-DocLayoutV3', 'paddlex-models/PP-DocLayoutV3'],
      ['PP-LCNet_x1_0_doc_ori', 'paddlex-models/PP-LCNet_x1_0_doc_ori'],
      ['UVDoc', 'paddlex-models/UVDoc'],
    ]
    for (const [name, dir] of subs) {
      out = out.replace(new RegExp(`(model_name: ${name}\\s*\\n\\s*model_dir: )null`), `$1${dir}`)
    }
    const outDir = path.join(this.deps.getUserData(), 'offline-pipeline')
    fs.mkdirSync(outDir, { recursive: true })
    const p = path.join(outDir, 'PaddleOCR-VL.yaml')
    fs.writeFileSync(p, out, 'utf-8')
    return p
  }

  // 按需量化：用 llama-quantize 从 FP16 基座生成指定精度 GGUF（一次性，之后直接复用）。
  // 量化日志写入用户数据目录 logs/quantize.log，期间 status().quantizing 置位供 UI 提示。
  // 同一精度并发触发时复用同一量化 promise，避免两个进程同时写同一个 out 文件。
  async ensureQuantized(precision) {
    const qmap = { q4_k_m: 'Q4_K_M', q5_k_m: 'Q5_K_M', q8_0: 'Q8_0' }
    const type = qmap[precision]
    if (!type) return false
    // 输入用内置只读 FP16 基座，输出写入用户数据目录（Program Files 安装下 resources 只读）
    const base = this.bundledModelsDir()
    const outDir = this.runtimeModelsDir()
    const fp16 = path.join(base, 'PaddleOCR-VL-1.6-fp16.gguf')
    const out = path.join(outDir, `PaddleOCR-VL-1.6-${precision}.gguf`)
    if (fs.existsSync(out)) return true
    if (!fs.existsSync(fp16)) return false
    const quantize = this.findQuantize()
    if (!fs.existsSync(quantize)) return false
    if (this.quantizePromise && this.quantizeTarget === precision) {
      await this.quantizePromise
      return fs.existsSync(out)
    }
    this.quantizing = precision
    this.emitStatus()
    const logsDir = this.runtimeLogDir()
    fs.mkdirSync(logsDir, { recursive: true })
    fs.mkdirSync(outDir, { recursive: true })
    const quantizeLogPath = path.join(logsDir, 'quantize.log')
    this.rotateLogIfNeeded(quantizeLogPath)
    const logFile = fs.openSync(quantizeLogPath, 'a')
    // 先写 .tmp，成功后再原子改名为正式产物：避免量化中途崩溃/磁盘满留下截断的
    // .gguf，之后被 existsSync 误判为"已就绪"而永久变砖。
    const tmp = out + '.tmp'
    const p = new Promise((resolve) => {
      let closed = false
      // llama-quantize 同样链接了 CUDA 12.9 运行时，需复用 Paddle 的 nvidia DLL（同 llama-server）
      const nvidiaBins = this.nvidiaRuntimePaths()
      const quantEnv = { ...process.env }
      if (nvidiaBins.length) quantEnv.PATH = nvidiaBins.join(';') + ';' + (quantEnv.PATH || '')
      const child = spawn(quantize, [fp16, tmp, type], { stdio: ['ignore', logFile, logFile], windowsHide: true, env: quantEnv })
      child.on('exit', (code) => {
        if (!closed) { closed = true; try { fs.closeSync(logFile) } catch (_) {} }
        if (code === 0 && fs.existsSync(tmp)) {
          try { fs.renameSync(tmp, out) } catch (_) {}
        } else {
          try { fs.rmSync(tmp, { force: true }) } catch (_) {}
        }
        resolve(code === 0 && fs.existsSync(out))
      })
      child.on('error', () => {
        if (!closed) { closed = true; try { fs.closeSync(logFile) } catch (_) {} }
        try { fs.rmSync(tmp, { force: true }) } catch (_) {}
        resolve(false)
      })
    })
    this.quantizePromise = p
    this.quantizeTarget = precision
    const ok = await p
    this.quantizePromise = null
    this.quantizeTarget = null
    this.quantizing = null
    this.emitStatus()
    return ok && fs.existsSync(out)
  }

  async stop() {
    // 停止后允许下一次 start() 重新发起（在途 _start 已无存活进程，其端口等待会自然超时结束）
    this.startPromise = null
    this.stopApiWatch()
    this.cpuFallback = { llama: false, api: false }
    const live = []
    for (const k of ['llama', 'api']) {
      const p = this.procs[k]
      if (p) {
        live.push(p)
        try { p.kill() } catch (_) {}
      }
    }
    // kill 是异步的：等待本实例进程真正退出后再清 PID，避免紧接着 start()
    // 检测到端口仍被旧进程占用而误报"端口被其他程序占用"。
    if (live.length) {
      await Promise.all(live.map((p) => new Promise((res) => {
        if (p.exitCode !== null || p.signalCode !== null) return res()
        let done = false
        const finish = () => { if (!done) { done = true; res() } }
        p.once('exit', finish)
        p.once('error', finish)
        setTimeout(finish, 3000)
      })))
    }
    this.procs = {}
    // 清理上次会话保留的服务进程（keepServicesAfterQuit 场景：PID 文件记录了它们）
    const pids = this.readPids()
    for (const pid of Object.values(pids)) {
      if (pid) { try { process.kill(pid) } catch (_) {} }
    }
    this.clearPids()
    this.latched = { llama: false, api: false }
    this.emitStatus()
    return { ok: true }
  }

  // ---------- 服务 PID 持久化（保留服务跨会话复用 / 停止清理） ----------
  pidFile() { return path.join(this.deps.getUserData(), 'services-pids.json') }

  readPids() {
    try { return JSON.parse(fs.readFileSync(this.pidFile(), 'utf-8')) } catch (_) { return {} }
  }

  writePids(p) {
    try { fs.writeFileSync(this.pidFile(), JSON.stringify(p), 'utf-8') } catch (_) { /* 忽略 */ }
  }

  clearPids() {
    try { fs.rmSync(this.pidFile(), { force: true }) } catch (_) { /* 忽略 */ }
  }

  // 校验 PID 是否存活且进程映像名匹配：仅靠 process.kill(pid,0) 无法区分系统回收
  // 复用该 PID 给无关进程的情况，可能把「复用上次服务」误判成仍在运行。
  pidMatches(pid, exeName) {
    if (!pid) return Promise.resolve(false)
    return new Promise((resolve) => {
      let done = false
      const finish = (v) => { if (!done) { done = true; resolve(v) } }
      try { process.kill(pid, 0) } catch (_) { return finish(false) }
      let child
      try {
        child = spawn('tasklist', ['/FI', `PID eq ${pid}`, '/FO', 'CSV', '/NH'], { windowsHide: true })
      } catch (_) { return finish(false) }
      let out = ''
      child.stdout.on('data', (d) => { out += d.toString('utf-8') })
      child.on('error', () => finish(false))
      child.on('close', () => {
        const m = out.match(/^"([^"]+)"/m)
        finish(m ? m[1].toLowerCase() === String(exeName).toLowerCase() : false)
      })
      setTimeout(() => finish(false), 3000)
    })
  }

  readLog(kind) {
    // 仅允许读取已知服务日志，防止 kind 传 ../ 等路径穿越读取任意文件
    if (!['llama', 'api', 'quantize'].includes(kind)) return ''
    const f = path.join(this.runtimeLogDir(), `${kind}.log`)
    try {
      if (!fs.existsSync(f)) return ''
      const size = fs.statSync(f).size
      const fd = fs.openSync(f, 'r')
      const skip = Math.max(0, size - 60 * 1024)
      const buf = Buffer.alloc(size - skip)
      fs.readSync(fd, buf, 0, buf.length, skip)
      fs.closeSync(fd)
      return buf.toString('utf-8')
    } catch (e) { return '' }
  }
}

// ---------- 端口探测 ----------
function checkPort(port, timeout = 1500) {
  return new Promise((resolve) => {
    const req = http.get({ host: '127.0.0.1', port, path: '/', timeout }, (res) => {
      res.destroy(); resolve(true)
    })
    req.on('timeout', () => { req.destroy(); resolve(false) })
    req.on('error', () => resolve(false))
  })
}

function waitPorts(ports, totalMs, onExit) {
  const start = Date.now()
  return new Promise((resolve) => {
    const tick = async () => {
      const ups = await Promise.all(ports.map((p) => checkPort(p, 2000)))
      if (ups.every(Boolean)) return resolve(true)
      if (Date.now() - start > totalMs) return resolve(false)
      if (onExit && onExit()) return resolve(false)
      setTimeout(tick, 3000)
    }
    tick()
  })
}

module.exports = { ServiceManager }
