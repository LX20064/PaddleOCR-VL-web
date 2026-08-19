<template>
  <div class="app-shell">
    <TitleBar @open-settings="openSettings" />
    <div class="app-main">
      <template v-if="!wizardOpen && !needSetup">
        <div class="app-body">
          <!-- 左侧导航栏 -->
          <SideNav :active="module" @change="onNavigate" @open-settings="openSettings" />
          <!-- 模块内容（keep-alive 保留各模块工作状态） -->
          <div class="module-view">
            <KeepAlive>
              <HomePage v-if="module === 'home'" @navigate="onNavigate" />
              <AcquirePage v-else-if="module === 'acquire'" :tab="acquireTab" @navigate="onNavigate" />
              <DocTools v-else-if="module === 'doc'" />
              <Convert v-else-if="module === 'convert'" />
            </KeepAlive>
          </div>
        </div>
      </template>
    </div>
    <SettingsDrawer v-model="settingsOpen" />
    <FirstRunWizard v-if="wizardOpen" @close="onWizardClose" @exit="onWizardExit" />
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { store } from './store'
import TitleBar from './components/TitleBar.vue'
import SideNav from './components/SideNav.vue'
import HomePage from './components/HomePage.vue'
import AcquirePage from './components/AcquirePage.vue'
import DocTools from './components/DocTools.vue'
import Convert from './components/Convert.vue'
import SettingsDrawer from './components/SettingsDrawer.vue'
import FirstRunWizard from './components/FirstRunWizard.vue'

const settingsOpen = ref(false)
const wizardOpen = ref(false)
// 初始为 true，避免在 setup 状态返回前主界面短暂闪现
const needSetup = ref(true)
const acquireTab = ref('scanner')

// 批量识别进行中禁止打开设置：识别参数在保存后会对队列中未开始的文件生效，
// 中途修改可能导致同批次内参数不一致。入口按钮已禁用，这里再做一层拦截兜底。
function openSettings() {
  if (store.running) {
    ElMessage.warning('批量识别进行中，请先停止后再修改设置')
    return
  }
  settingsOpen.value = true
}

// 旧版本模块名兼容：scan/camera → acquire
const LEGACY = { scan: 'acquire', camera: 'acquire' }
const MODULES = ['home', 'acquire', 'doc', 'convert']
// 默认打开首页；仅当设置开启「记住上次位置」时在启动后恢复上次所在页面
const module = ref('home')

function restoreModule() {
  if (!store.settings?.rememberLastModule) return
  let saved = localStorage.getItem('app.module') || 'home'
  if (LEGACY[saved]) saved = LEGACY[saved]
  if (!MODULES.includes(saved)) saved = 'home'
  module.value = saved
}

// 页面间导航（可带获取页的子标签）
function onNavigate(m, tab) {
  if (tab) acquireTab.value = tab
  module.value = m
  localStorage.setItem('app.module', m)
}

// 本次会话内，整合包文件缺失的原生错误对话框只弹一次（避免反复触发）
let missingNotified = false
async function refreshSetup({ autoOpen = false } = {}) {
  const s = await window.api.getSetupStatus()
  store.setup = s
  // 整合包模式（离线分发）：环境随包自带，不引导在线安装。
  // 整合包不完整 = 无法使用：直接弹系统级「文件缺失错误」模态框提示重新安装，
  // 并阻塞主界面，不提供「仍然继续」入口。
  const need = !s.bundledComplete
  needSetup.value = need
  if (need && autoOpen && !missingNotified) {
    missingNotified = true
    wizardOpen.value = true
    window.api.notifyMissingFiles()
  }
}

// 点击「开始使用」（仅整合包完整时出现）：进入主界面
function onWizardClose() {
  wizardOpen.value = false
  // 防御：任何情况下只要整合包仍不完整，就不得进入主界面，直接退出
  if (!store.setup?.bundledComplete) {
    window.api.quitApp()
    return
  }
  needSetup.value = false
}

// 点击「退出应用」（整合包缺文件）：直接退出
function onWizardExit() {
  window.api.quitApp()
}

onMounted(async () => {
  // 各步骤独立兜底：任一项失败不影响整体初始化，避免主界面卡死
  try { store.sys = await window.api.getSystemInfo() } catch (e) { store.sys = {} }
  try { store.settings = await window.api.getSettings() } catch (e) { store.settings = {} }
  restoreModule()
  try { store.backend = await window.api.getBackendStatus() } catch (e) { store.backend = {} }
  try { window.api.onBackendStatus((st) => { store.backend = st }) } catch (e) {}
  // Win10（build < 22000）无系统 DWM 圆角：切换为方角铺满布局，
  // 避免窗口四角露出透明空洞、关闭按钮不贴边
  const isWin10 = store.sys?.platform === 'win32' && (store.sys.winBuild || 0) < 22000
  document.documentElement.classList.toggle('platform-win10', !!isWin10)
  try {
    await refreshSetup({ autoOpen: true })
  } catch (e) {
    // setup 状态获取失败时放行主界面，避免因单次 IPC 异常导致空白
    needSetup.value = false
  }
})
</script>
