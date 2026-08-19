<template>
  <div class="convert">
    <!-- 图片 → PDF -->
    <div class="card conv-card">
      <div class="conv-title">
        <el-icon style="margin-right:6px;vertical-align:-2px"><Document /></el-icon>图片 → PDF（多页合并）
      </div>
      <div class="conv-desc">按列表顺序将多张图片合并为一个 PDF 文件</div>
      <div class="conv-list" v-if="pdfList.length">
        <div v-for="(p, i) in pdfList" :key="p" class="conv-item">
          <span class="conv-idx">{{ i + 1 }}</span>
          <span class="conv-name" :title="p">{{ p.split(/[\\/]/).pop() }}</span>
          <el-button size="small" text type="danger" @click="pdfList.splice(i, 1)">移除</el-button>
        </div>
      </div>
      <div v-else class="conv-empty">点击「添加图片」，可多次添加、按顺序合并</div>
      <div class="conv-actions">
        <el-button size="small" @click="pickPdf"><el-icon style="margin-right:3px"><Plus /></el-icon>添加图片</el-button>
        <el-button size="small" :disabled="!pdfList.length" text @click="pdfList = []">清空</el-button>
        <span style="flex:1"></span>
        <el-button type="primary" size="small" :disabled="!pdfList.length || pdfBusy" @click="exportPdf" :loading="pdfBusy">
          导出 PDF
        </el-button>
      </div>
      <div v-if="err1" class="conv-err">{{ err1 }}</div>
    </div>

    <!-- 批量格式转换 -->
    <div class="card conv-card">
      <div class="conv-title">
        <el-icon style="margin-right:6px;vertical-align:-2px"><Refresh /></el-icon>图片格式批量转换
      </div>
      <div class="conv-desc">批量将图片转换为指定格式，输出到同一目录</div>
      <div class="conv-opts">
        <span class="conv-label">目标格式</span>
        <el-select v-model="fmt" size="small" style="width:110px">
          <el-option v-for="f in ['jpg', 'png', 'tiff', 'bmp', 'webp']" :key="f" :label="f.toUpperCase()" :value="f" />
        </el-select>
        <span class="conv-label">输出目录</span>
        <el-input v-model="outDir" size="small" placeholder="选择输出文件夹" style="flex:1">
          <template #append><el-button size="small" @click="pickOut">选择</el-button></template>
        </el-input>
      </div>
      <div class="conv-list" v-if="srcList.length">
        <div v-for="(p, i) in srcList" :key="p" class="conv-item">
          <span class="conv-idx">{{ i + 1 }}</span>
          <span class="conv-name" :title="p">{{ p.split(/[\\/]/).pop() }}</span>
          <el-button size="small" text type="danger" @click="srcList.splice(i, 1)">移除</el-button>
        </div>
      </div>
      <div v-else class="conv-empty">添加需要转换格式的图片（可多选）</div>
      <div class="conv-actions">
        <el-button size="small" @click="pickSrc"><el-icon style="margin-right:3px"><Plus /></el-icon>添加图片</el-button>
        <el-button size="small" :disabled="!srcList.length" text @click="srcList = []">清空</el-button>
        <span style="flex:1"></span>
        <span v-if="doneMsg" class="conv-ok">{{ doneMsg }}</span>
        <el-button type="primary" size="small" :disabled="!srcList.length || !outDir || convBusy" @click="batchConvert" :loading="convBusy">
          开始转换
        </el-button>
      </div>
      <div v-if="err2" class="conv-err">{{ err2 }}</div>
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Document, Refresh, Plus } from '@element-plus/icons-vue'
import { log } from '../store'

const pdfList = ref([])
const srcList = ref([])
const fmt = ref('png')
const outDir = ref('')
const pdfBusy = ref(false)
const convBusy = ref(false)
const err1 = ref('')
const err2 = ref('')
const doneMsg = ref('')

async function pickPdf() {
  // 仅允许选择图片：merge_to_pdf 无法处理 PDF 输入
  const files = await window.api.chooseFiles(true)
  for (const f of files) if (!pdfList.value.includes(f)) pdfList.value.push(f)
}
async function pickSrc() {
  const files = await window.api.chooseFiles(true)
  for (const f of files) if (!srcList.value.includes(f)) srcList.value.push(f)
}
async function pickOut() {
  const d = await window.api.chooseDirectory()
  if (d) outDir.value = d
}

async function exportPdf() {
  if (!pdfList.value.length) return
  pdfBusy.value = true
  err1.value = ''
  const out = await window.api.saveFile({
    title: '导出 PDF',
    defaultPath: 'merged.pdf',
    filters: [{ name: 'PDF', extensions: ['pdf'] }],
  })
  if (out) {
    try {
      const r = await window.api.pdfMerge({ out, pages: pdfList.value.slice(), dpi: 300 })
      if (!r.ok) throw new Error(r.error)
      window.api.revealPath(out)
      log(`✔ PDF 已导出：${out}（${pdfList.value.length} 页）`)
      ElMessage.success(`PDF 导出完成（${pdfList.value.length} 页）`)
    } catch (e) {
      err1.value = e.message
    }
  }
  pdfBusy.value = false
}

async function batchConvert() {
  if (!srcList.value.length || !outDir.value) return
  convBusy.value = true
  err2.value = ''
  doneMsg.value = ''
  let okN = 0, failN = 0
  const usedNames = new Set()
  // 全部源文件路径（小写）作为保留集合：输出不能覆盖任何待处理/已处理的源文件。
  // 例如 a.jpg 转 a.png 时，若 a.png 也在列表中，不能把尚未处理的源 a.png 覆盖掉。
  const reserved = new Set(srcList.value.map((p) => p.toLowerCase()))
  for (const p of srcList.value) {
    const stem = p.replace(/\.[^.]+$/, '').split(/[\\/]/).pop()
    const dir = outDir.value.replace(/[\\/]$/, '')
    let out = `${dir}\\${stem}.${fmt.value}`
    let n = 1
    while (usedNames.has(out.toLowerCase()) || reserved.has(out.toLowerCase())) {
      out = `${dir}\\${stem}_${n++}.${fmt.value}`
    }
    usedNames.add(out.toLowerCase())
    try {
      const r = await window.api.imgApply({ src: p, convert: fmt.value, out })
      if (!r.ok) throw new Error(r.error)
      okN++
    } catch (e) {
      failN++
      log(`✖ 转换失败 ${p}：${e.message}`)
    }
  }
  doneMsg.value = `完成：成功 ${okN}，失败 ${failN}`
  log(`✔ 批量转换完成：成功 ${okN}，失败 ${failN}`)
  if (okN) window.api.revealPath(outDir.value)
  if (failN) ElMessage.warning(`转换完成：成功 ${okN} 个，失败 ${failN} 个`)
  else ElMessage.success(`全部转换成功（${okN} 个）`)
  convBusy.value = false
}
</script>

<style scoped>
.convert {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(380px, 1fr));
  gap: 12px; height: 100%; min-height: 0; overflow-y: auto; align-content: start;
}
.conv-card { padding: 16px; display: flex; flex-direction: column; }
.conv-title { font-size: 14px; font-weight: 700; }
.conv-desc { font-size: 12px; color: var(--muted); margin: 4px 0 12px; }
.conv-opts { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; flex-wrap: wrap; }
.conv-label { font-size: 13px; color: var(--muted); flex: none; }
.conv-list { max-height: 260px; overflow-y: auto; border: 1px solid var(--border); border-radius: 8px; margin-bottom: 10px; }
.conv-item { display: flex; align-items: center; gap: 8px; padding: 6px 10px; border-bottom: 1px solid var(--border); }
.conv-item:last-child { border-bottom: none; }
.conv-idx {
  flex: none; width: 20px; height: 20px; border-radius: 50%; background: rgba(128,134,148,0.15);
  font-size: 11px; color: var(--muted); display: inline-flex; align-items: center; justify-content: center;
}
.conv-name { flex: 1; font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.conv-empty { border: 2px dashed var(--border); border-radius: 8px; padding: 18px; text-align: center; color: var(--muted); font-size: 12px; margin-bottom: 10px; }
.conv-actions { display: flex; align-items: center; gap: 8px; }
.conv-err { color: var(--err); font-size: 12px; margin-top: 8px; }
.conv-ok { color: var(--ok); font-size: 12px; }
</style>
