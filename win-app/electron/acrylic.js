// Windows 窗口背景材质
// - Win11 22H2+：DwmSetWindowAttribute(DWMWA_SYSTEMBACKDROP_TYPE=DWMSBT_TRANSIENTWINDOW)
//   作用在整个窗口（含非客户区）， Acrylic 效果最强。
// - Win10 1809+：Electron 无该 API，通过 SetWindowCompositionAttribute(ACCENT_ENABLE_ACRYLICBLURBEHIND) 实现
//   真亚克力（桌面模糊透出）。走 koffi FFI，免编译、随包分发预编译二进制。
const os = require('os')
const { nativeTheme } = require('electron')

let ffi = null
let dwmFfi = null

// 系统构建号，如 10.0.19045 → 19045
function winBuild() {
  const parts = os.release().split('.')
  return parseInt(parts[2] || '0', 10)
}

// 初始化 koffi FFI（仅 Win10 需要；失败则静默降级）
function initFfi() {
  if (ffi !== null) return ffi
  try {
    const koffi = require('koffi')
    const user32 = koffi.load('user32')
    const AccentPolicy = koffi.struct('AccentPolicy', {
      AccentState: 'int',
      AccentFlags: 'int',
      GradientColor: 'uint32',
      AnimationId: 'int',
    })
    // 原生布局：Attribute(DWORD) + Data(指向 AccentPolicy 的指针) + SizeOfData(SIZE_T)
    // Data 必须是指针，而非内联结构体；SizeOfData 应为 sizeof(AccentPolicy)=16
    const WcaData = koffi.struct('WINDOWCOMPOSITIONATTRIBDATA', {
      Attribute: 'int',
      Data: 'AccentPolicy *',
      SizeOfData: 'size_t',
    })
    const swca = user32.func('int SetWindowCompositionAttribute(void *hwnd, WINDOWCOMPOSITIONATTRIBDATA *data)')
    ffi = { koffi, swca, WcaData, AccentPolicy }
  } catch (e) {
    ffi = false
  }
  return ffi
}

// Win10 1809+ 亚克力（ACCENT_ENABLE_ACRYLICBLURBEHIND）
function applyWin10Acrylic(win) {
  const f = initFfi()
  if (!f) return false
  try {
    const hwnd = win.getNativeWindowHandle().readBigUInt64LE(0)
    const tint = nativeTheme.shouldUseDarkColors ? 0x99000000 : 0x40ffffff
    const data = {
      Attribute: 19, // WCA_ACCENT_POLICY
      Data: {
        AccentState: 4, // ACCENT_ENABLE_ACRYLICBLURBEHIND
        AccentFlags: 2,
        GradientColor: tint,
        AnimationId: 0,
      },
      SizeOfData: f.koffi.sizeof(f.AccentPolicy),
    }
    return f.swca(hwnd, data) === 1
  } catch (e) {
    return false
  }
}

// 初始化 DWM FFI（Win11 22H2+ 使用）
function initDwmFfi() {
  if (dwmFfi !== null) return dwmFfi
  try {
    const koffi = require('koffi')
    const dwmapi = koffi.load('dwmapi')
    const IntVal = koffi.struct('IntVal', { val: 'int' })
    // DwmSetWindowAttribute(HWND hwnd, DWORD dwAttribute, void *pvAttribute, DWORD cbAttribute)
    // HRESULT 在 koffi 中用 'long'（有符号 32 位）表示；DWORD 用 'uint32'
    const dswa = dwmapi.func('long DwmSetWindowAttribute(void *hwnd, uint32 attr, IntVal *pvAttribute, uint32 cbAttribute)')
    dwmFfi = { koffi, dswa, IntVal }
  } catch (e) {
    console.error('[acrylic] initDwmFfi error', e)
    dwmFfi = false
  }
  return dwmFfi
}

// Win11 22H2+：用 DWM 系统级 Acrylic（整个窗口）
function applyWin11DwmAcrylic(win) {
  const f = initDwmFfi()
  if (!f) return false
  try {
    const hwnd = win.getNativeWindowHandle().readBigUInt64LE(0)
    const dark = nativeTheme.shouldUseDarkColors ? 1 : 0
    // DWMWA_USE_IMMERSIVE_DARK_MODE = 20
    const darkArg = { val: dark }
    f.dswa(hwnd, 20, darkArg, f.koffi.sizeof(f.IntVal))
    // DWMWA_SYSTEMBACKDROP_TYPE = 38；DWMSBT_TRANSIENTWINDOW = 3（Acrylic）
    const backArg = { val: 3 }
    const backdropOk = f.dswa(hwnd, 38, backArg, f.koffi.sizeof(f.IntVal))
    return backdropOk === 0
  } catch (e) {
    console.error('[acrylic] Win11 DWM error', e)
    return false
  }
}

// Win11 兜底：Electron 原生 API
function applyWin11Material(win) {
  try {
    win.setBackgroundMaterial('acrylic')
    return true
  } catch (e) {
    try { win.setBackgroundMaterial('mica') } catch (_) { /* 忽略 */ }
    return false
  }
}

// 按系统版本应用窗口背景材质；失败则走 CSS 毛玻璃兜底
function applyWindowMaterial(win) {
  const build = winBuild()
  if (build >= 22000) {
    const ok = applyWin11DwmAcrylic(win)
    if (!ok) return applyWin11Material(win)
    return ok
  }
  if (build >= 17763) {
    return applyWin10Acrylic(win)
  }
  return false
}

// 监听窗口状态变化，确保亚克力持续生效（Win11 最大化会禁用亚克力，还原/显示后需重新应用）
function bindMaterialRefresh(win) {
  const reapply = () => setTimeout(() => applyWindowMaterial(win), 250)
  win.on('show', reapply)
  win.on('maximize', reapply)
  win.on('unmaximize', reapply)
  win.on('restore', reapply)
}

module.exports = { applyWindowMaterial, bindMaterialRefresh, winBuild }
