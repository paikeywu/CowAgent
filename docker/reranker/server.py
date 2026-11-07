"""
BGE Reranker v2-m3 推理服务。

对外暴露一个 HTTP 接口，供 CowAgent 的 `agent/memory/reranker.py` 通过
``rerank_provider: "http"`` 调用。返回契约与本地 ``Reranker`` 一致：
每个候选文档一个分数，顺序与入参一一对应。

为什么单独成服务而不是直接用 sentence-transformers 装进主进程：

* 主进程内存敏感（多个 Agent 会话共享一个 Python 进程）。
* 模型只加载一次，容器重启不影响主进程。
* 以后换模型（v2-m3 -> 别的 reranker）只改容器，不动主进程代码。

接口：
    POST /rerank   {"query": "...", "documents": ["...", "..."]}
                   -> {"scores": [0.1, 0.9, ...]}          # sigmoid 后的 0~1
                   -> {"scores": [...], "logits": [...]}    # 同时给原始 logit
    GET  /health   -> {"status": "ok", "model": "...", "device": "..."}
"""

import os
import time
from typing import List, Optional

import torch
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

MODEL_DIR = os.environ.get("MODEL_DIR", "/models/bge-reranker-v2-m3")
# CPU 线程数：容器里默认会读到宿主全部核数，4 核机器上过度订阅反而拖慢推理。
TORCH_THREADS = int(os.environ.get("TORCH_THREADS", "4"))
# bge-reranker-v2-m3 的最大输入长度（token），超长会被截断。
MAX_LENGTH = int(os.environ.get("MAX_LENGTH", "1024"))
# 单次请求最多打分多少个文档；超过则分批，防止一次请求打满内存。
MAX_BATCH = int(os.environ.get("MAX_BATCH", "64"))

app = FastAPI(title="BGE Reranker v2-m3", version="1.0.0")

_model = None
_device = "cpu"
_load_error: Optional[str] = None


class RerankRequest(BaseModel):
    query: str = Field(..., description="查询文本")
    documents: List[str] = Field(..., description="候选文档列表")


def load_model():
    """加载 CrossEncoder。失败时记录原因并抛出，容器由 healthcheck 判定为 unhealthy。"""
    global _model, _device, _load_error
    if _model is not None:
        return _model
    if _load_error is not None:
        raise RuntimeError(_load_error)

    try:
        if not os.path.isdir(MODEL_DIR):
            raise FileNotFoundError(
                f"模型目录不存在: {MODEL_DIR}（请挂载模型卷，或设置 MODEL_DIR）"
            )
        needed = ["config.json", "tokenizer.json", "tokenizer_config.json"]
        missing = [f for f in needed if not os.path.exists(os.path.join(MODEL_DIR, f))]
        if missing:
            raise FileNotFoundError(f"模型目录缺少文件: {missing}（位于 {MODEL_DIR}）")

        from sentence_transformers import CrossEncoder

        torch.set_num_threads(TORCH_THREADS)
        _device = "cuda" if torch.cuda.is_available() else "cpu"
        _model = CrossEncoder(MODEL_DIR, device=_device, max_length=MAX_LENGTH)
        print(f"[reranker] model loaded from {MODEL_DIR} on {_device}", flush=True)
        return _model
    except Exception as e:  # noqa: BLE001 - 需要把原因带回调用方
        _load_error = f"{type(e).__name__}: {e}"
        print(f"[reranker] failed to load model: {_load_error}", flush=True)
        raise


def sigmoid(value: float) -> float:
    """把 cross-encoder logit 压到 [0, 1]，与本地实现保持同样的数值语义。"""
    if value >= 0:
        return 1.0 / (1.0 + torch.exp(torch.tensor(-value)).item())
    exp = torch.exp(torch.tensor(value)).item()
    return exp / (1.0 + exp)


@app.get("/health")
def health():
    """健康检查：模型是否已成功加载。"""
    if _load_error is not None:
        return {"status": "error", "model": MODEL_DIR, "error": _load_error}
    if _model is None:
        return {"status": "loading", "model": MODEL_DIR}
    return {
        "status": "ok",
        "model": MODEL_DIR,
        "device": _device,
        "torch_threads": TORCH_THREADS,
        "max_length": MAX_LENGTH,
    }


@app.post("/rerank")
def rerank(req: RerankRequest):
    """给每个候选文档打相关性分。分数越高越相关。"""
    if not req.documents:
        return {"scores": [], "logits": []}
    if not req.query or not req.query.strip():
        raise HTTPException(status_code=400, detail="query 不能为空")

    model = load_model()
    pairs = [(req.query, doc) for doc in req.documents]
    t0 = time.time()
    try:
        logits = model.predict(pairs, batch_size=MAX_BATCH, show_progress_bar=False)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"推理失败: {type(e).__name__}: {e}")

    raw = [float(x) for x in logits]
    return {
        "scores": [sigmoid(x) for x in raw],
        "logits": raw,
        "elapsed_ms": round((time.time() - t0) * 1000, 1),
    }


@app.post("/warmup")
def warmup():
    """预热模型：加载权重并跑一次空批，避免首个真实请求承担加载延迟。"""
    model = load_model()
    t0 = time.time()
    model.predict([("warmup", "warmup")], show_progress_bar=False)
    return {"status": "ok", "elapsed_ms": round((time.time() - t0) * 1000, 1)}


@app.on_event("startup")
def startup():
    """启动时尝试加载；失败不阻止进程起来，交由 /health 暴露真实原因。"""
    try:
        load_model()
    except Exception:  # noqa: BLE001
        pass
