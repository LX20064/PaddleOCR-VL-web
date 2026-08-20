// preload.js —— 渲染进程与主进程的安全桥（contextIsolation）
const { contextBridge, ipcRenderer, webUtils } = require('electron')

const on = (channel, cb) => {
  const listener = (_e, ...args) => cb(...args)
  ipcRenderer.on(channel, listener)
  return () => ipcRenderer.removeListener(channel, listener)
}

contextBridge.exposeInMainWorld('api', {
  // ---- 系统 / 环境 ----
  getSystemInfo: () => ipcRenderer.invoke('sys:info'),
  getSettings: () => ipcRenderer.invoke('settings:get'),
  saveSettings: (partial) => ipcRenderer.invoke('settings:save', partial),
  getDefaultDirs: () => ipcRenderer.invoke('sys:default-dirs'),
  pathExists: (p) => ipcRenderer.invoke('sys:path-exists', p),

  // ---- 后端服务管理 ----
  getBackendStatus: () => ipcRenderer.invoke('backend:status'),
  startBackend: () => ipcRenderer.invoke('backend:start'),
  warmupBackend: () => ipcRenderer.invoke('backend:warmup'),
  stopBackend: () => ipcRenderer.invoke('backend:stop'),
  getBackendLog: (kind) => ipcRenderer.invoke('backend:log', kind),
  getSetupStatus: () => ipcRenderer.invoke('setup:status'),
  notifyMissingFiles: () => ipcRenderer.invoke('sys:missing-files'),

  // ---- 解析 / 导出 / 预览（Python sidecar）----
  parseFile: (req) => ipcRenderer.invoke('scan:parse', req),
  exportRender: (req) => ipcRenderer.invoke('export:render', req),
  renderPdfPreview: (pdfPath, maxPages) => ipcRenderer.invoke('scan:pdf-preview', pdfPath, maxPages),

  // ---- WIA 扫描仪（UWP 扫描功能）----
  scanDevices: () => ipcRenderer.invoke('scan:devices'),
  scanDefaultDir: () => ipcRenderer.invoke('scan:default-dir'),
  scanPreview: (devId, dpi) => ipcRenderer.invoke('scan:preview', devId, dpi),
  scanRun: (opt) => ipcRenderer.invoke('scan:run', opt),
  pdfMerge: (opt) => ipcRenderer.invoke('pdf:merge', opt),
  imgApply: (opt) => ipcRenderer.invoke('img:apply', opt),
  saveImageData: (opt) => ipcRenderer.invoke('img:save-data', opt),
  saveFile: (opt) => ipcRenderer.invoke('dlg:save', opt),
  onScanProgress: (cb) => on('ev:scan-progress', cb),

  // ---- 文件对话框 / 系统 ----
  chooseFiles: (imagesOnly) => ipcRenderer.invoke('dlg:files', { images: imagesOnly }),
  chooseFolder: () => ipcRenderer.invoke('dlg:folder'),
  chooseDirectory: () => ipcRenderer.invoke('dlg:dir'),
  listFiles: (dir, recursive) => ipcRenderer.invoke('fs:list-files', dir, recursive),
  imageDataUri: (p) => ipcRenderer.invoke('img:data', p),
  getPathForFile: (f) => webUtils.getPathForFile(f),
  revealPath: (p) => ipcRenderer.invoke('sys:reveal', p),
  openDir: (dir) => ipcRenderer.invoke('sys:open-dir', dir),
  openExternal: (url) => ipcRenderer.invoke('sys:open-external', url),
  windowMin: () => ipcRenderer.send('win:min'),
  windowMax: () => ipcRenderer.send('win:max'),
  windowClose: () => ipcRenderer.send('win:close'),
  quitApp: () => ipcRenderer.send('app:quit'),
  copyText: (text) => ipcRenderer.invoke('clipboard:write', text),

  // ---- 事件订阅（进度 / 日志 / 服务状态变化）----
  onBackendStatus: (cb) => on('ev:backend-status', cb),
  onProgress: (cb) => on('ev:progress', cb),
  onLog: (cb) => on('ev:log', cb),
})
