# PaddleOCR-VL 文档解析网页版 · 完整部署指南

> 适用环境：**Ubuntu 24.04 LTS + RTX 2080（8GB 显存）+ NVIDIA 驱动 580**
> 目标效果：局域网内任意设备打开浏览器 → 上传图片/PDF → 返回 Markdown → 一键下载

## 整体架构

```
浏览器（PC / 手机）
      │  http://服务器IP:7860      用户网页
      │  http://服务器IP:7861      管理后台（密码登录）
      ▼
Gradio 网页界面（web_ocr.py） / 管理后台（admin.py）  ← 本包提供，几乎不占显存
      │  http://127.0.0.1:8080/layout-parsing 和 /restructure-pages
      ▼
PaddleOCR-VL API 服务（paddlex --serve）← 版面分析等产线流程，跑在 RTX 2080 上
      │  http://127.0.0.1:8081（VLRecognition 的 llama-cpp-server 后端）
      ▼
llama.cpp llama-server（PaddleOCR-VL GGUF 量化模型）← VLM 识别推理
```

**为什么这样搭**：RTX 2080 是 Turing 架构（Compute Capability 7.5），vLLM / SGLang / FastDeploy
都要求 CC ≥ 8.0，无法使用；且 8GB 显存放不下 FP16 完整 VLM。因此分两段：
- 版面分析等产线流程用 **PaddlePaddle 原生推理**（CC ≥ 7.0 即可，paddlex --serve，端口 8080）；
- VLM 识别用 **llama.cpp + GGUF 量化模型**（llama-server，端口 8081，默认 Q5_K_M），
  通过 paddlex 的 llama-cpp-server 后端接入。
驱动 580 向下兼容 CUDA 12.x 的 PaddlePaddle / llama.cpp 轮子，无需更换驱动。
推理**一次只处理一个请求**——网页端已通过队列排队解决（见 web_ocr.py）。

## 文件清单

| 文件 | 作用 |
|---|---|
| `README.md` | 本指南 |
| `web_ocr.py` | Gradio 用户网页（图片+PDF 上传、Markdown 预览、6 个导出按钮、请求队列、内置 MCP，7860 端口） |
| `admin.py` | 管理后台（密码登录、服务开关、运行监控、默认设置、VL 精度切换、服务状态、日志查看、修改密码，7861 端口） |
| `web_common.py` | 用户网页与管理后台共享的配置读写模块 |
| `web_config.json` | 运行配置（API 地址、超时、各功能默认勾选状态、VL 量化精度、管理密码哈希） |
| `wordrender.py` | 本地 DOCX 渲染模块（将 Markdown + 图片渲染为原生 Word 表格） |
| `requirements-web.txt` | 网页端 Python 依赖 |
| `serving_extra.yaml` | 产线服务优化配置片段（关可视化、限制 PDF 页数） |
| `start_all.sh` / `stop_all.sh` | 一键启动 / 停止四个服务（llama-server + API + 用户网页 + 管理后台） |
| `start_llama_server.sh` | 启动 llama.cpp llama-server（GGUF 量化 VLM 后端，8081 端口），启动前自动调用 model_manager 按需准备模型 |
| `model_manager.py` | 模型管理脚本：自动下载官方 PaddleOCR-VL 模型 → 转换为 GGUF → 按需量化到所选精度 |
| `tools/llama.cpp/` | 本地 CUDA 编译的 llama.cpp（含 llama-server / llama-quantize / convert_hf_to_gguf.py） |
| `tools/gguf/` | PaddleOCR-VL 的 GGUF 量化模型（FP16 / Q8_0 / Q5_K_M / Q4_K_M + mmproj），由 model_manager 按需生成 |
| `systemd/` | 开机自启的 systemd 单元（可选，含 api / web / admin 单元） |

---

## 第 0 步：前置检查

```bash
nvidia-smi        # 确认能看到 RTX 2080，驱动版本 580.x
df -h ~           # 确认家目录剩余空间充足（首次运行会自动下载官方模型 + 生成 GGUF，建议预留 ≥ 15GB）
```

## 第 1 步：系统依赖与虚拟环境

```bash
sudo apt update
sudo apt install -y python3.12-venv curl poppler-utils

python3 -m venv ~/.venv_paddleocr
source ~/.venv_paddleocr/bin/activate
```

> 说明：`poppler-utils` 提供 `pdftoppm`，用于网页端 PDF 解析后的页面图片预览；缺失时解析功能不受影响，但 PDF 结果预览图无法生成。

## 第 2 步：安装 PaddlePaddle + PaddleOCR-VL

```bash
# GPU 版 PaddlePaddle（CUDA 12.6 构建，驱动 580 兼容）
python -m pip install paddlepaddle-gpu==3.2.1 -i https://www.paddlepaddle.org.cn/packages/stable/cu126/

# PaddleOCR-VL 基础包
python -m pip install -U "paddleocr[doc-parser]"

# 服务化部署插件（paddlex 随 paddleocr 一并安装）
paddlex --install serving
```

验证：

```bash
python -c "import paddle; paddle.utils.run_check()"   # 输出 PaddlePaddle is installed successfully 即正常
```

## 第 3 步：生成并优化产线配置

```bash
# 建议建一个工作目录，把本包所有文件放进去
mkdir -p ~/paddleocr-vl-web && cd ~/paddleocr-vl-web

# 生成默认产线配置文件
paddlex --get_pipeline_config PaddleOCR-VL        # 生成 PaddleOCR-VL.yaml

# 追加优化配置（关闭可视化、限制 PDF 最大页数）
# 注意：若 PaddleOCR-VL.yaml 中已有 Serving 字段，请手动合并，不要出现两个顶层 Serving
cat serving_extra.yaml >> PaddleOCR-VL.yaml
```

## 第 4 步：准备 llama.cpp + GGUF 量化 VLM 后端（自动下载 + 按需转换量化）

本部署的 VLM 识别走 llama.cpp。`start_llama_server.sh` 启动前会自动调用
`model_manager.py`，实现「**缺什么下什么、选什么量化什么**」，无需手工准备：

1. **CUDA 编译的 llama.cpp**：`tools/llama.cpp/build-cuda/bin/llama-server`、
   `llama-quantize`，以及 `convert_hf_to_gguf.py` 转换脚本（本仓库已内置）；
2. **官方模型自动下载**：首次运行时自动从 ModelScope / HuggingFace 下载
   `PaddlePaddle/PaddleOCR-VL-1.6` 完整官方模型到 `tools/models/PaddleOCR-VL-1.6/`；
3. **自动转换 GGUF**：用内置 `convert_hf_to_gguf.py` 生成 FP16 主模型
   `PaddleOCR-VL-1.6-GGUF.gguf` 与投影层 `PaddleOCR-VL-1.6-GGUF-mmproj.gguf`
   （投影层**保持原精度、不量化**）；
4. **按需量化**：只在所选精度文件缺失时，用 `llama-quantize` 从 FP16 主模型量化出
   对应版本（`-Q8_0` / `-Q5_K_M` / `-Q4_K_M`）。

> **切换精度不会删除**旧精度文件、FP16 主模型或已下载的官方模型，再次切换回来时
> 秒级跳过，无需重复转换。已就绪的模型文件也直接跳过，不重复下载/转换。

手动准备 / 查看状态：

```bash
# 按需准备某个精度的 VL 模型（已存在则秒级跳过）
python model_manager.py ensure-vl --precision q4_k_m

# 首次初始化：VL 模型 + 产线子模型（版面检测 / 方向分类 / 矫正等无需量化的模型）
python model_manager.py ensure-all --precision q5_k_m

# 查看各模型文件就绪状态
python model_manager.py status
```

可单独验证 llama-server（会自动按需准备所选精度的模型）：

```bash
bash start_llama_server.sh q5_k_m    # 默认 q5_k_m，可选 fp16 / q8_0 / q5_k_m / q4_k_m
```

## 第 5 步：启动 API 服务并测试

```bash
paddlex --serve --pipeline ~/paddleocr-vl-web/PaddleOCR-VL.yaml --device gpu:0
```

看到 `Uvicorn running on http://0.0.0.0:8080` 即成功。**首次运行会自动下载模型**，耐心等待。
（API 服务的 VLM 识别会通过 `llama-cpp-server` 后端调用 8081 的 llama-server，故第 4 步需先就绪。）

另开一个终端测试（保持服务运行）：

```bash
curl -X POST http://127.0.0.1:8080/layout-parsing \
  -H "Content-Type: application/json" \
  -d '{"file": "https://paddle-model-ecology.bj.bcebos.com/paddlex/imgs/demo_image/paddleocr_vl_demo.png", "fileType": 1}' \
  | head -c 500
```

返回 `"errorCode":0` 即服务正常。测试完可先 `Ctrl+C` 停掉，后面用脚本统一后台启动。

## 第 6 步：安装网页端并启动

```bash
source ~/.venv_paddleocr/bin/activate
cd ~/paddleocr-vl-web

python -m pip install -r requirements-web.txt
```

一键后台启动四个服务（llama-server + API + 用户网页 + 管理后台）：

```bash
bash start_all.sh
```

输出会列出两个入口地址（日志在 `logs/` 目录，停止用 `bash stop_all.sh`）：

- 用户网页：`http://服务器IP:7860`
- 管理后台：`http://服务器IP:7861`（管理密码 admin123）

## 第 7 步：防火墙放行（如启用 ufw）

```bash
sudo ufw allow 7860/tcp                                # 用户网页，按需放行
sudo ufw allow from 192.168.1.0/24 to any port 7861    # 管理后台，仅放行内网网段（改成你的网段）
# 8080 / 8081 只对本机网页端开放，无需对外放行
```

## 第 8 步（可选）：开机自启（systemd）

```bash
# 1. 编辑 systemd/ 下三个 .service 文件，把所有 YOUR_USER 替换为你的用户名
# 2. 安装并启用
sudo cp systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now paddleocr-vl-api paddleocr-vl-web paddleocr-vl-admin

# 查看状态 / 日志
systemctl status paddleocr-vl-api
journalctl -u paddleocr-vl-api -f
```

> 注意：systemd 仅托管 api / web / admin 三个服务；llama-server（8081）需另行通过
> `bash start_llama_server.sh` 或开机脚本启动，否则 VLM 识别后端不可用。

---

## 网页端使用说明

页面分「扫描」和「参数设置」两个页签。

### 扫描页

1. 打开 `http://服务器IP:7860`
2. 左侧上传**图片**（jpg/png/bmp/tiff/webp）或 **PDF**，支持拖拽文件/文件夹、含子目录
3. 点击「开始处理」→ 右侧识别结果区显示 Markdown 预览，可切换「Markdown / 渲染 / 源码 / 原图」四个视图
4. 右上角 6 个图标按钮：
   - **下载 Markdown**：下载 zip（内含 imgs 图片目录 + 对应 .md）
   - **导出 Word**：输出单个 .docx；多页 PDF 默认经 `/restructure-pages` 合并为单个 .docx
   - **导出 HTML**：下载 zip（内含 imgs + .html）
   - **导出 JSON**：下载 zip（内含 imgs + .json）
   - **复制 Markdown**：复制结果到剪贴板
   - **全屏**：结果区全屏查看
5. 底部为运行日志（自动滚动、可折叠、可清空）

> 勾选「PDF 每页输出单独文件」后，多页 PDF 的四个导出按钮（Markdown / Word / HTML / JSON）
> 都改为输出「每页一个独立文件」的 zip（每页 `page_XX/` 子目录含对应文件与 imgs）。

### 参数设置页

「参数设置」页签直接暴露产线参数，所有修改**仅对本次扫描生效**，
不写回 `web_config.json`，刷新页面即恢复后台默认值：

**模型参数**

| 参数 | 说明 |
|---|---|
| 上下文长度 | VLM 单页最大生成 token（留空 = 后端默认） |
| 显存占用比（单图最大像素） | 当前 llama-cpp-server 后端不支持（仅 vllm-server 生效），保持「不限制」即可 |
| 采样温度 temperature | VLM 采样参数，输出异常（重复、幻觉）时可调 |
| top_p 核采样 | VLM 采样参数 |

**识别参数（功能开关）**

| 参数 | 说明 |
|---|---|
| 文档方向分类 | 自动校正倒置/旋转的文档图片 |
| 文本图像矫正 | 拉平卷曲/折痕导致的不规则形变 |
| 印章识别 | 提取文档中的印章内容 |
| 图表解析 | 解析嵌入式数据图表（开启后耗时明显增加） |
| 版面分析 | 自动检测分栏、表格、标题、图像区域 |
| 跨栏分栏合并 | 将跨栏/交错排列的文本合并为连续段落 |
| 图像块内 OCR | 对图片内嵌文字再做一次 OCR |
| 块内容格式化 | 将表格/公式等结构化内容渲染为 Markdown 格式 |
| 单图识别类型 | 关闭版面分析后生效：ocr / formula / table / seal / chart / spotting |
| 版面检测阈值 | 0~1，调低抓更多块、调高减少误检 |
| 最小像素总量 | 当前 llama-cpp-server 后端不支持（仅 vllm-server 生效），保持「留空」即可 |
| 重复惩罚 | VLM 采样参数 |

**PDF 与高级选项**

| 参数 | 说明 |
|---|---|
| PDF 每页输出单独文件 | 多页 PDF 按页拆分为独立结果文件 |
| 导出图表区域为图片 | 将识别出的图表区域单独导出为图片文件（打包进导出 zip 的 charts/ 目录） |

这些参数全部对应服务端 `/layout-parsing` 的原生请求字段（PDF 每页拆分、图表导出为网页端本地处理）。

## 产线功能开关说明（当前部署默认状态）

PaddleOCR-VL 产线各功能在本部署中的默认开/关如下：

| 功能 | 默认状态 | 说明 |
|---|---|---|
| 版面分析（PP-DocLayoutV3 检测 + 阅读排序） | ✅ 始终开启 | 产线核心，检测标题/表格/公式/文本块并排序 |
| VLM 识别（PaddleOCR-VL-1.6 GGUF） | ✅ 始终开启 | 对裁剪子图逐块识别，llama.cpp + GGUF 量化推理（8081） |
| 内部异步队列（use_queues） | ✅ 默认开启 | PDF 渲染 / 版面分析 / VLM 分线程流水线，提升多页效率 |
| 跨栏版面块合并（merge_layout_blocks） | ✅ 默认开启 | 合并跨栏、上下交错分栏的检测框 |
| Markdown 忽略页眉页脚等标签 | ✅ 默认开启 | number/footnote/header/footer/aside_text 不写入 md |
| 服务可视化图返回（Serving.visualize） | ❌ 已关闭 | 本部署在 serving_extra.yaml 中关闭，降低显存与传输开销 |
| PDF / 多页 TIFF 页数上限 | ⚠️ 限 50 页 | serving_extra.yaml 中 max_num_input_imgs: 50 |
| 文档方向分类（旋转矫正） | ❌ 默认关闭 | 产线已加载预处理模型，网页/管理后台勾选即可启用 |
| 文本图像矫正（弯曲/畸变） | ❌ 默认关闭 | 产线已加载预处理模型，网页/管理后台勾选即可启用 |
| 印章识别 | ✅ 默认开启 | 网页/管理后台可关 |
| 图表解析 | ✅ 默认开启 | 网页/管理后台可关（开启后耗时明显增加） |
| 块内容格式化（use_format_block） | ❌ 默认关闭 | 网页/管理后台可开 |
| 图片块内文字识别（use_ocr_for_image_block） | ❌ 默认关闭 | 网页/管理后台可开 |
| PDF 每页输出单独文件 | ❌ 默认关闭 | 网页/管理后台可开 |
| 导出图表区域为图片 | ❌ 默认关闭 | 网页/管理后台可开 |

## 管理后台（7861 端口）

打开 `http://服务器IP:7861`，输入管理密码（**`admin123`**，登录后可在「安全设置」中修改）。

六个功能页：

1. **服务开关**（核心控制）：
   - **🔌 总扫描服务开关**：**默认关闭**。首次登录管理后台后需手动开启，用户网页和 MCP 才能解析；
     关闭时两个入口都会拒绝请求并提示"服务未开启"
   - **🌐 网页服务子开关** / **🤖 MCP 服务子开关**：在总开关开启的前提下，可单独关闭某一入口
   - 开关为**软门控**：paddlex 进程常驻（模型加载要几分钟，不适合频繁启停），
     关闭只是拒绝请求，**即时生效、无需重启**
2. **运行监控**：当前扫描进度（文件名 + 百分比 + 阶段）、**排队等待数量**、
   累计提交/完成/失败统计，**每 5 秒自动刷新**
3. **默认设置**：用户网页各功能开关的默认勾选状态（含 PDF 每页输出、导出图表区域）、
   单图最大像素默认值、后端 API 地址、请求超时、结果缓存保留天数，
   以及 **VLM 量化精度热切换**（fp16 / q8_0 / q5_k_m / q4_k_m，点击即按需准备目标精度模型并重启 llama-server；
   若目标精度文件缺失会先自动转换/量化，切换时不删除其它精度文件）。
   保存后：默认值在用户网页**刷新页面**时生效，API 地址与超时**立即生效**
4. **服务状态**：API 服务（8080）与 llama-server（8081）连通性检查、GPU 型号/显存/利用率/温度（nvidia-smi）
5. **日志查看**：api.log / web.log / admin.log / llama.log 尾部内容
6. **安全设置**：修改管理密码（至少 6 位，SHA-256 存储在 web_config.json）

**排队数量的统计口径**：用户点击"开始解析"即计数（submitted），任务出队开始处理时计数
（started），二者之差即排队数。MCP 调用不经过网页点击，故排队数只反映网页端队列。

**升级注意**：从旧版本升级后，由于新增的总开关默认为**关闭**，请先登录管理后台开启，
否则用户网页会显示"扫描服务当前关闭"横幅。

**设计取舍**（哪些设置不放管理后台）：
- 产线 YAML（模型路径、运行设备、Serving 字段）仍手工编辑——改错会导致服务起不来，不适合网页化；
- 显存调优（maxPixels 默认值可管；VLM 量化精度已在后台「默认设置」中热切换）；
- 管理后台只覆盖"服务开关 + 运行监控 + 用户功能默认值 + VL 精度 + 连接参数 + 运维观察 + 密码"。

安全提示：管理后台**不要对公网开放**。如启用 ufw，仅放行内网网段访问 7861，例如：
`sudo ufw allow from 192.168.1.0/24 to any port 7861`。

## Agent 接入（内置 MCP 服务器）

网页脚本已开启 Gradio 内置 MCP 服务器（需安装 `gradio[mcp]`，requirements-web.txt 已包含）：

- MCP 接入地址：`http://服务器IP:7860/gradio_api/mcp/`（Streamable HTTP）
- 工具名：`paddleocr_vl`（与官方 PaddleOCR MCP 的 PaddleOCR-VL 工具同名），仅暴露这一个工具；参数只有 `file_path`，其余产线参数采用管理后台「默认设置」中的后端默认值
- Cherry Studio / Cursor 配置示例：

```json
{
  "mcpServers": {
    "paddleocr-vl-web": {
      "url": "http://服务器IP:7860/gradio_api/mcp/"
    }
  }
}
```

- Claude Desktop（仅支持 stdio）需经 mcp-remote 桥接：

```json
{
  "mcpServers": {
    "paddleocr-vl-web": {
      "command": "npx",
      "args": ["mcp-remote", "http://服务器IP:7860/gradio_api/mcp/", "--transport", "streamable-http"]
    }
  }
}
```

注意：通过 MCP 调用时，`file_path` 只支持以下两种形式：

1. **公网 http(s) URL**——文件需托管在**公网可访问**的地址上。由于 Gradio 内置
   **SSRF 防护**（`safehttpx` 校验），指向本机 / 内网 / 私有 IP 的 URL 一律被拦截，
   直接报 `Hostname xxx failed validation`，因此以下地址均**不可用**：
   `http://127.0.0.1:...`、`http://localhost:...`、`http://192.168.x.x:...`、
   `http://10.x.x.x:...`，也包括 `upload_file_to_gradio` 返回的同机 URL。
2. **base64 data URI**（`data:image/png;base64,....`）——最通用、推荐的方式。
   在 Agent / 客户端本地把文件内容编码为 base64 后作为 `file_path` 传入，
   MCP 服务器端会自动解码为临时文件处理，不受 SSRF 限制。
   编码方法示例：

   ```bash
   # Linux / macOS（获取可用于 file_path 的 data URI）
   base64 -w0 test_scan.png | sed 's#^#data:image/png;base64,#'

   # 也可用 Python 生成
   python3 -c "import base64;print('data:image/png;base64,'+base64.b64encode(open('test_scan.png','rb').read()).decode())"
   ```

   若 Agent 客户端自身能读取本地文件（如 Cherry Studio 的本地附件），通常会
   自动用 data URI 形式传给工具，无需手动编码。
如需关闭内置 MCP：启动前设 `GRADIO_MCP_SERVER=False`。
另一条可选路线是官方 paddleocr-mcp（自托管模式指向 8080 服务），二者可并存。

## 常见问题（FAQ）

**Q1. 首次解析很慢？**
正常。首次运行要下载模型（约 3~5GB）：首次启动 llama-server 时会自动下载官方
PaddleOCR-VL 模型并转换为 GGUF（首次按需量化会额外耗时数分钟~数十分钟），产线子模型
（版面检测等）首次由 paddlex 自动下载。之后每次解析图片约数秒~十几秒，多页 PDF 按页累加。

> 提示：若 `model_manager.py ensure-all` 中的产线子模型预热因网络等原因失败，
> paddlex --serve 会在首次解析请求时现场下载，可能导致首次请求超时（默认 600 秒）。
> 此时可手动重试 `python model_manager.py ensure-all`，或适当调大管理后台的「请求超时」。

**Q2. 显存溢出（OOM）怎么办？**
- 当前 llama-cpp-server 后端不支持「显存占用比 / 最小像素总量」参数，无需调整这两项；
- 确认没有其它程序占用显存（`nvidia-smi`）；
- 在管理后台「默认设置」把 VLM 量化精度切换为更低档（如 q4_k_m），减少显存占用。

**Q3. 多人同时使用会怎样？**
网页端已内置队列（`default_concurrency_limit=1`），请求自动排队，不会把显存打爆；
排队上限 32 个。想提升吞吐需要换 CC ≥ 8.0 的显卡上 vLLM。

**Q4. 外网能访问吗？**
Gradio 本身不建议直接暴露公网。如需外网访问，在前面加 Nginx + HTTPS + Basic Auth，
或使用内网穿透工具（cpolar / frp 等），并务必加访问密码。

**Q5. 想改网页端口？**
`GRADIO_SERVER_PORT=8000 bash start_all.sh`，或改 systemd 单元里的 `GRADIO_SERVER_PORT`。

**Q6. API 服务能否给其它程序调用？**
可以，直接 POST `http://服务器IP:8080/layout-parsing`，协议见官方文档
（`file` 传 Base64 或 URL，`fileType` 0=PDF / 1=图片）。生产环境建议用防火墙只放行需要的来源。

## 参考资料

- PaddleOCR-VL 官方教程：<https://github.com/PaddlePaddle/PaddleOCR/blob/main/docs/version3.x/pipeline_usage/PaddleOCR-VL.md>
- PaddlePaddle 安装文档：<https://www.paddlepaddle.org.cn/install/quick>
- llama.cpp 官方仓库（CUDA 编译 / GGUF 转换与量化 / llama-server）：<https://github.com/ggml-org/llama.cpp>
- PaddleOCR-VL GGUF 使用说明：见 `tools/gguf/README.md`

---

## 附录：各文件完整源码

> 以下与本目录下同名文件内容一致，如个别文件丢失可直接从这里复制恢复。

### A. web_ocr.py

见同目录 `web_ocr.py`（Gradio 网页界面完整源码，含扫描/参数设置两页签、6 个导出按钮、请求队列与内置 MCP）。

### B. serving_extra.yaml

```yaml
Serving:
  visualize: False
  extra:
    max_num_input_imgs: 50
```

### C. start_all.sh

见同目录 `start_all.sh`（一键后台启动 llama-server + API + 用户网页 + 管理后台，含就绪等待）。

### D. systemd 单元

见 `systemd/paddleocr-vl-api.service`、`systemd/paddleocr-vl-web.service` 和
`systemd/paddleocr-vl-admin.service`，使用前记得把所有 `YOUR_USER` 替换为你的用户名。
