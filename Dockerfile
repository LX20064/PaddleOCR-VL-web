# syntax=docker/dockerfile:1
# ============================================================
# PaddleOCR-VL 文档解析网页版 —— 多架构 NVIDIA GPU 镜像
# 支持计算能力 CC 7.5 ~ 12.0（Turing / Ampere / Ada / Hopper / Blackwell）
#
# 构建：
#   docker build -t paddleocr-vl:cu129 .
#
# 架构说明：
#   - llama.cpp 编译为多架构 fatbin（PTX 向前 JIT + 关键架构 SASS）
#   - PaddlePaddle 使用 cu129 官方轮子（自带 CUDA/cuDNN，含 sm_75~sm_120）
#   - 宿主机驱动 >= 570（Blackwell 门槛），老卡 Turing 同样兼容
# ============================================================

# ---- 阶段 1：编译多架构 llama.cpp（需要 nvcc，故用 devel 镜像）----
FROM nvidia/cuda:12.9.0-devel-ubuntu24.04 AS builder

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        git cmake build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

# 复用项目内的多架构编译脚本（clone + checkout 锁定 commit + 编译）
COPY build_llama_cuda.sh /workspace/build_llama_cuda.sh
RUN chmod +x /workspace/build_llama_cuda.sh \
    && bash /workspace/build_llama_cuda.sh /workspace/llama.cpp \
    && rm -rf /workspace/llama.cpp/.git

# ---- 阶段 2：运行时镜像 ----
FROM nvidia/cuda:12.9.0-runtime-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip curl \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3 /usr/bin/python

WORKDIR /app

# 拷贝项目代码（.dockerignore 已排除 tools/、outputs/、logs/、.git 等）
COPY . /app

# 从 builder 拷贝编译好的 llama.cpp（含 build-cuda + convert_hf_to_gguf.py + gguf/conversion 依赖）
COPY --from=builder /workspace/llama.cpp /app/tools/llama.cpp

# 安装 PaddlePaddle（cu129，官方轮子自带 CUDA/cuDNN） + PaddleOCR + 服务化插件
ARG PADDLE_VERSION=3.2.2
RUN python -m pip install --upgrade pip \
    && python -m pip install "paddlepaddle-gpu==${PADDLE_VERSION}" \
        -i https://www.paddlepaddle.org.cn/packages/stable/cu129/ \
    && python -m pip install -U "paddleocr[doc-parser]" \
    && paddlex --install serving

# 网页端依赖 + 模型下载 / GGUF 转换依赖
# （torch 不在此显式安装：paddlex 已带 GPU torch；若缺失，model_manager 运行时自动补 CPU torch）
RUN python -m pip install -r requirements-web.txt \
    && python -m pip install modelscope huggingface_hub \
        transformers protobuf safetensors sentencepiece

# 入口脚本
COPY docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh /app/*.sh

# 8080 API / 8081 llama-server（仅内部）/ 7860 用户网页 / 7861 管理后台
EXPOSE 8080 7860 7861

ENTRYPOINT ["/app/docker-entrypoint.sh"]
