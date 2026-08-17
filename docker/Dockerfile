# syntax=docker/dockerfile:1
# ============================================================
# PaddleOCR-VL 文档解析网页版 —— 多架构 NVIDIA GPU 镜像
# 支持计算能力 CC 7.5 ~ 12.0（Turing / Ampere / Ada / Hopper / Blackwell）
#
# 构建（在仓库根目录 PaddleOCR-VL/ 下执行）：
#   docker build -f docker/Dockerfile -t paddleocr-vl:cu129 .
#
# 架构说明：
#   - llama.cpp 编译为多架构 fatbin（PTX 向前 JIT + 关键架构 SASS）
#   - PaddlePaddle 使用 cu129 官方轮子（自带 CUDA/cuDNN，含 sm_75~sm_120）
#   - 宿主机驱动 >= 570（Blackwell 门槛），老卡 Turing 同样兼容
# ============================================================

# ---- 阶段 1：编译多架构 llama.cpp（需要 nvcc，故用 devel 镜像）----
FROM nvidia/cuda:12.9.0-devel-ubuntu22.04 AS builder

ENV DEBIAN_FRONTEND=noninteractive

# Ubuntu 22.04 自带 cmake 3.22 不认识 sm_120a，改用 pip 装新版 cmake（>=3.31 支持 sm_120）
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential python3 python3-pip \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m pip install --no-cache-dir "cmake>=3.31,<4"

WORKDIR /workspace

# 离线 llama.cpp 源码（已锁定 commit ba360ef，含转换/量化脚本；不含 .git 与旧编译产物）
COPY docker/llama.cpp-src.tar.gz /workspace/llama.cpp-src.tar.gz
COPY docker/build_llama_cuda.sh /workspace/build_llama_cuda.sh
RUN mkdir -p /workspace/llama.cpp \
    && tar -xzf /workspace/llama.cpp-src.tar.gz -C /workspace/llama.cpp \
    && rm /workspace/llama.cpp-src.tar.gz \
    && chmod +x /workspace/build_llama_cuda.sh \
    && bash /workspace/build_llama_cuda.sh /workspace/llama.cpp

# ---- 阶段 2：运行时镜像 ----
# 改用 runtime 基础镜像（仅含 CUDA 运行时库，不含 nvcc/头文件/开发库），
# 可显著减小镜像体积。CUDA 12.9 与 PaddlePaddle cu129 保持一致。
FROM nvidia/cuda:12.9.0-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip curl \
        libgl1 libglib2.0-0 libsm6 libxext6 libxrender1 libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3 /usr/bin/python

WORKDIR /app

# 内置 GGUF 量化模型（2.8G：fp16 主模型 + mmproj + q8_0/q5_k_m/q4_k_m）。
# 已包含所有可切换精度，容器内 ensure_vl_model 检测到文件存在会跳过下载/转换，实现开箱即用。
# 与源码无关，放在最前面以最大化层缓存命中。
COPY tools/gguf/1.6 /app/tools/gguf/1.6

# 内置产线子模型（版面检测 PP-DocLayoutV3 / 文档方向分类 PP-LCNet_x1_0_doc_ori / 矫正 UVDoc），
# 打进 paddlex 默认缓存目录，paddlex 服务启动时直接命中，无需联网下载。
COPY tools/paddlex-models /root/.paddlex/official_models

# 从 builder 只拷贝运行时需要的 llama.cpp 二进制与共享库
# （llama-server / llama-quantize 及 ggml/CUDA 动态库），不拷贝源码与构建产物。
# GGUF 转换脚本 convert_hf_to_gguf.py 随「模型已内置」而移除，进一步减小体积。
COPY --from=builder /workspace/llama.cpp/build-cuda/bin /app/tools/llama.cpp/build-cuda/bin

# 先单独拷贝 requirements-web.txt 并安装全部依赖；源码最后再 COPY，
# 这样修改源码不会触发 paddlepaddle / paddleocr 等大体积依赖重新安装。
ARG PADDLE_VERSION=3.2.2
COPY requirements-web.txt /app/requirements-web.txt

# 安装 PaddlePaddle（cu129，官方轮子自带 CUDA/cuDNN） + PaddleOCR + 服务化插件
RUN python -m pip install --upgrade pip \
    && python -m pip install "paddlepaddle-gpu==${PADDLE_VERSION}" \
        -i https://www.paddlepaddle.org.cn/packages/stable/cu129/ \
    && python -m pip install -U "paddleocr[doc-parser]" \
    && paddlex --install serving

# 网页端依赖。GGUF 量化模型已内置（tools/gguf/1.6），启动时 model_manager 检测到文件
# 存在即跳过下载/转换，因此不再安装仅用于 GGUF 转换的 torch / transformers 等重型依赖。
#   - modelscope / huggingface_hub / safetensors / sentencepiece / protobuf 已由
#     paddleocr / paddlex 自动安装（属于其运行期依赖）。
RUN python -m pip install -r requirements-web.txt

# PDF 页面「原图」预览依赖：poppler-utils 提供 pdftoppm（把 PDF 各页渲染为 PNG）。
# web_ocr.py 的 _render_pdf_pages 依赖 pdftoppm；镜像内缺失时该函数静默失败，
# 导致 PDF 上传后「原图」标签页空白。独立成层避免改动上方依赖层而触发大体积重下载。
RUN apt-get update && apt-get install -y --no-install-recommends \
        poppler-utils \
    && rm -rf /var/lib/apt/lists/*

# 源码最后拷贝（只影响 COPY 层及其后的入口脚本层，改动后构建接近秒级）
COPY . /app

# 入口脚本
COPY docker/docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh /app/*.sh

# 8080 API / 8081 llama-server（仅内部）/ 7860 用户网页 / 7861 管理后台
EXPOSE 8080 7860 7861

ENTRYPOINT ["/app/docker-entrypoint.sh"]
