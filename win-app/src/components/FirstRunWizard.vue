<template>
  <div class="wizard-overlay">
    <div class="card wizard-card">
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:6px">
        <span class="logo" style="width:22px;height:22px;border-radius:6px;background:linear-gradient(135deg,var(--accent),var(--accent-2));display:inline-flex;align-items:center;justify-content:center;color:#fff;font-size:13px;font-weight:700">VL</span>
        <span style="font-size:16px;font-weight:700">离线整合包检测</span>
      </div>
      <p style="color:var(--muted);font-size:13px;margin:6px 0 14px">
        PaddleOCR-VL 采用离线整合包分发：Python 环境、llama.cpp 与 VLM 模型随安装包提供，无需联网安装。
      </p>

      <!-- 整合包完整可用 -->
      <div v-if="store.setup?.bundledComplete" class="bundle-ok">
        <div style="font-size:22px;font-weight:700;margin-bottom:8px">离线整合包已就绪</div>
        <div style="color:var(--muted);font-size:13px;line-height:1.8">
          Python 3.12 + PaddlePaddle + llama.cpp + VLM 模型已全部打包，无需联网安装。
          点击「开始使用」进入扫描界面，服务启动时会按显卡能力自动选择 GPU / CPU 推理。
        </div>
        <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:16px">
          <el-button type="primary" @click="$emit('close')">开始使用</el-button>
        </div>
      </div>

      <!-- 已检测到整合包但文件不完整 -->
      <div v-else-if="store.setup?.bundled" class="bundle-err">
        <div style="font-size:16px;font-weight:700;margin-bottom:8px">整合包文件不完整</div>
        <div style="color:var(--muted);font-size:13px;line-height:1.8">
          已检测到离线整合包，但缺少以下组成部分：
          <ul style="margin:8px 0 0 18px;padding:0">
            <li v-for="m in bundleMissing" :key="m">{{ m }}</li>
          </ul>
          <div style="margin-top:8px">应用缺少必要文件，无法正常使用。请重新安装应用程序以恢复完整文件。</div>
        </div>
        <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:16px">
          <el-button :loading="checking" @click="recheck">重新检查</el-button>
          <el-button type="danger" @click="$emit('exit')">退出应用</el-button>
        </div>
      </div>

      <!-- 未检测到整合包 -->
      <div v-else class="bundle-err">
        <div style="font-size:16px;font-weight:700;margin-bottom:8px">未检测到离线整合包</div>
        <div style="color:var(--muted);font-size:13px;line-height:1.8">
          未在 <code>{{ store.setup?.bundledDir }}</code> 找到离线整合包结构（需要
          <code>python/python.exe</code>、<code>llama.cpp/llama-server.exe</code>、<code>paddlex-models</code>
          与 <code>models/gguf</code>）。应用缺少必要文件，无法正常使用，请重新安装应用程序后重新启动。
        </div>
        <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:16px">
          <el-button :loading="checking" @click="recheck">重新检查</el-button>
          <el-button type="danger" @click="$emit('exit')">退出应用</el-button>
        </div>
      </div>

      <!-- 环境摘要 -->
      <div style="display:flex;gap:8px;margin-top:14px;flex-wrap:wrap">
        <el-tag :type="store.setup?.gpu ? 'success' : 'warning'" size="small" effect="plain">
          GPU：{{ store.setup?.gpu ? store.setup.gpu.name + ' · CC ' + store.setup.gpu.computeCap + ' · ' + store.setup.gpu.mem : '未检测到（将使用 CPU）' }}
        </el-tag>
        <el-tag :type="store.setup?.venvReady ? 'success' : 'info'" size="small" effect="plain">
          Python 环境：{{ store.setup?.venvReady ? '已就绪' : '未就绪' }}
        </el-tag>
        <el-tag :type="store.setup?.llamaReady ? 'success' : 'info'" size="small" effect="plain">
          llama-server：{{ store.setup?.llamaReady ? '已就绪' : '未就绪' }}
        </el-tag>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { store } from '../store'

defineEmits(['close', 'exit'])

const checking = ref(false)

// 整合包缺失项清单（bundledComplete=false 时展示）
const bundleMissing = computed(() => {
  const s = store.setup || {}
  const miss = []
  if (!s.venvReady) miss.push('Python 运行时（offline/python/）')
  if (!s.llamaReady) miss.push('llama-server.exe（offline/llama.cpp/）')
  if (!(s.models || []).some((m) => m.ready)) miss.push('VLM 模型 GGUF（offline/models/gguf/）')
  return miss.length ? miss : ['部分组件不可用']
})

async function recheck() {
  checking.value = true
  try {
    store.setup = await window.api.getSetupStatus()
    ElMessage.success(store.setup?.bundledComplete ? '检测完成：整合包已就绪' : '检测完成：文件仍不完整')
  } catch (e) {
    ElMessage.error('重新检查失败：' + (e.message || e))
  } finally {
    checking.value = false
  }
}
</script>

<style scoped>
code { background: rgba(128,134,148,0.15); padding: 1px 5px; border-radius: 4px; font-size: 12px; }
.bundle-ok {
  border: 1px solid rgba(103,194,58,0.4);
  background: rgba(103,194,58,0.08);
  border-radius: 10px;
  padding: 20px 22px;
}
.bundle-err {
  border: 1px solid rgba(230,162,60,0.5);
  background: rgba(230,162,60,0.08);
  border-radius: 10px;
  padding: 20px 22px;
}
</style>
