// afterPack 钩子：在打包完成后、NSIS 压缩前，用 rcedit 给主 exe 设置自定义图标与版本信息。
// 原因：win.signAndEditExecutable:false 时 electron-builder 不会修改 exe 图标（保持 Electron 默认图标），
// 而改为 true 需要下载 winCodeSign 且其 7z 解压在非管理员环境下会因符号链接权限失败。
// 该钩子直接调用随项目携带的 rcedit-x64.exe，无需网络下载与管理员权限。
const { execFileSync } = require('child_process')
const path = require('path')
const fs = require('fs')

exports.default = async function afterPack(context) {
  const { appOutDir, packager } = context
  const appInfo = packager.appInfo
  const projectDir = packager.projectDir

  const exeName = `${appInfo.productFilename}.exe`
  const exePath = path.join(appOutDir, exeName)
  const rceditPath = path.join(projectDir, 'scripts', 'rcedit-x64.exe')
  const iconPath = path.join(projectDir, 'build', 'icon.ico')

  if (!fs.existsSync(exePath)) {
    console.log(`[applyIcon] skip: ${exePath} not found`)
    return
  }
  if (!fs.existsSync(rceditPath)) {
    throw new Error(`[applyIcon] rcedit not found: ${rceditPath}`)
  }
  if (!fs.existsSync(iconPath)) {
    throw new Error(`[applyIcon] icon not found: ${iconPath}`)
  }

  const args = [
    exePath,
    '--set-version-string', 'FileDescription', appInfo.description || '',
    '--set-version-string', 'ProductName', appInfo.productName,
    '--set-version-string', 'InternalName', appInfo.productName,
    '--set-file-version', appInfo.version,
    '--set-product-version', appInfo.version,
  ]
  if (appInfo.copyright) {
    args.push('--set-version-string', 'LegalCopyright', appInfo.copyright)
  }
  args.push('--set-icon', iconPath)

  console.log(`[applyIcon] rcedit ${exeName}`)
  execFileSync(rceditPath, args, { stdio: 'inherit' })
  console.log(`[applyIcon] done`)
}
