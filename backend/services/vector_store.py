# ============================================================
# Chroma 向量检索：用于“相似职位”和语义召回。
# 设计取舍：
# 1) 优先用 sentence-transformers + bge-small-zh 做中文语义向量；
# 2) 本机若被应用控制策略拦截 DLL（torch/regex），自动退化为
#    纯 Python 的 384 维哈希向量（字符 bigram + 英文词），
#    仍然可检索、可持久化，不拖垮主业务；
# 3) 首次使用才初始化，避免登录/列表页被模型下载拖慢。
# ============================================================
from __future__ import annotations

import hashlib
import logging
import math
import re
import threading
from typing import Any, Iterable, Optional

from backend.config import get_settings
from backend.models import Job
from backend.services.text import clean_text

logger = logging.getLogger(__name__)

# 哈希向量维度：384 与本机 bge-small-zh 输出维度一致，切换后端时集合不冲突。
_HASH_DIM = 384
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9+#.\-]{0,30}|[\u4e00-\u9fff]")


class VectorStore:
    """Chroma 的薄封装；所有异常都在内部消化，绝不让主业务 500。"""

    def __init__(self) -> None:
        self.settings = get_settings()
        self._lock = threading.Lock()
        self._collection = None
        self._model = None
        self._backend = "uninitialized"
        self._error = ""
        self._tried = False

    @property
    def configured(self) -> bool:
        return bool(self.settings.vector_enabled)

    def _ensure_ready(self) -> bool:
        """第一次使用时初始化 Chroma + embedding；失败则关闭向量功能。"""
        if not self.configured:
            self._error = "向量检索已在配置中关闭"
            self._backend = "disabled"
            return False
        if self._collection is not None:
            return True
        if self._tried and self._collection is None:
            return False
        with self._lock:
            if self._collection is not None:
                return True
            self._tried = True
            try:
                import chromadb

                self.settings.chroma_path.mkdir(parents=True, exist_ok=True)
                client = chromadb.PersistentClient(path=str(self.settings.chroma_path))
                self._collection = client.get_or_create_collection(
                    name="jobs",
                    metadata={"hnsw:space": "cosine"},
                )
            except Exception as exc:  # noqa: BLE001 - 依赖/路径失败都走降级
                self._error = f"{exc.__class__.__name__}: {exc}"
                self._backend = "unavailable"
                logger.warning("Chroma 初始化失败：%s", self._error)
                self._collection = None
                return False

            # 只在显式开启时才尝试 bge；默认直接 hash，避免 DLL 拦截/模型下载卡住请求。
            self._backend = "hash-384"
            if self.settings.vector_try_model:
                try:
                    from sentence_transformers import SentenceTransformer

                    self._model = SentenceTransformer(self.settings.embedding_model, device="cpu")
                    self._backend = self.settings.embedding_model
                    logger.info("向量检索已就绪：%s", self._backend)
                except Exception as exc:  # noqa: BLE001 - DLL/下载失败都允许降级
                    self._model = None
                    self._backend = "hash-384"
                    self._error = f"{exc.__class__.__name__}: {exc}"
                    logger.warning("语义模型不可用，使用 hash-384 向量：%s", self._error)
            return True

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """中文按单字 + bigram，英文/数字按词；兼顾召回与无分词依赖。"""
        lowered = (text or "").lower()
        tokens = _TOKEN_RE.findall(lowered)
        # 中文 bigram 能显著改善“AI 应用交付”与“AI 交付”这类局部重合。
        chinese = re.findall(r"[\u4e00-\u9fff]+", lowered)
        for chunk in chinese:
            for idx in range(len(chunk) - 1):
                tokens.append(chunk[idx:idx + 2])
        return tokens

    @classmethod
    def _hash_embed_one(cls, text: str) -> list[float]:
        """特征哈希：把 token 映射到 384 维，带符号累加后 L2 归一化。

        这不是“神经网络语义向量”，而是无需任何模型/DLL 的稳定向量表示；
        在没有 GPU/被安全策略限制的 Windows 上，仍能支持相似职位检索。
        """
        vector = [0.0] * _HASH_DIM
        tokens = cls._tokenize(text)
        if not tokens:
            return vector
        counts: dict[str, int] = {}
        for token in tokens:
            counts[token] = counts.get(token, 0) + 1
        for token, count in counts.items():
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % _HASH_DIM
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            # 词频用 sqrt 抑制长文本霸榜；bigram 已在 token 列表中自然加权。
            vector[index] += sign * (1.0 + math.log(count))
        norm = math.sqrt(sum(value * value for value in vector))
        if norm > 0:
            vector = [value / norm for value in vector]
        return vector

    def _embed(self, texts: list[str]) -> list[list[float]]:
        """有语义模型就用模型，否则用哈希向量；两条路径都返回 list[listfloat]。"""
        if self._model is not None:
            return self._model.encode(
                texts,
                normalize_embeddings=True,
                batch_size=8,
                show_progress_bar=False,
            ).tolist()
        return [self._hash_embed_one(text) for text in texts]

    @staticmethod
    def _document(job: Job) -> str:
        """职位文档：标题/公司/城市/技能标签 + 描述前 1200 字。"""
        return clean_text(
            "\n".join(
                [
                    f"职位：{job.title}",
                    f"公司：{job.company}",
                    f"城市：{job.city}",
                    f"薪资：{job.salary_text}",
                    f"标签：{'、'.join(job.tags or [])}",
                    f"描述：{job.description}",
                ]
            ),
            2000,
        )

    def upsert_job(self, job: Job) -> bool:
        """写入/更新一条职位向量；返回是否成功。"""
        if not self._ensure_ready():
            return False
        try:
            document = self._document(job)
            embedding = self._embed([document])[0]
            assert self._collection is not None
            self._collection.upsert(
                ids=[f"job_{job.id}"],
                documents=[document],
                metadatas=[
                    {
                        "job_id": job.id,
                        "source": job.source,
                        "city": job.city or "",
                        "title": (job.title or "")[:200],
                        "company": (job.company or "")[:200],
                    }
                ],
                embeddings=[embedding],
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("职位 %s 向量写入失败：%s", job.id, exc.__class__.__name__)
            return False

    def delete_job(self, job_id: int) -> None:
        """删除职位时同步删向量，避免相似职位指向不存在的记录。"""
        if not self._ensure_ready():
            return
        try:
            assert self._collection is not None
            self._collection.delete(ids=[f"job_{job_id}"])
        except Exception:
            logger.warning("职位 %s 向量删除失败", job_id)

    def query(self, text: str, n_results: int = 8, where: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
        """按自然语言查询相似职位；返回 [{job_id, distance, metadata}]。"""
        if not text.strip() or not self._ensure_ready():
            return []
        try:
            embedding = self._embed([text[:4000]])[0]
            assert self._collection is not None
            result = self._collection.query(
                query_embeddings=[embedding],
                n_results=max(1, min(20, n_results)),
                where=where or None,
                include=["metadatas", "distances"],
            )
            ids = (result.get("ids") or [[]])[0]
            distances = (result.get("distances") or [[]])[0]
            metadatas = (result.get("metadatas") or [[]])[0]
            items = []
            for idx, item_id in enumerate(ids):
                meta = metadatas[idx] if idx < len(metadatas) else {}
                items.append(
                    {
                        "job_id": int(meta.get("job_id") or str(item_id).replace("job_", "")),
                        "distance": float(distances[idx]) if idx < len(distances) else 1.0,
                        "metadata": meta,
                    }
                )
            return items
        except Exception as exc:  # noqa: BLE001
            logger.warning("向量查询失败：%s", exc.__class__.__name__)
            return []

    def count(self) -> int:
        """索引条数；管理员系统页用。失败返回 -1 表示不可用。"""
        if not self._ensure_ready():
            return -1
        try:
            assert self._collection is not None
            return int(self._collection.count())
        except Exception:
            return -1

    def status(self) -> dict[str, Any]:
        """不触发模型加载的状态摘要，避免打开管理员页就下载 100MB。"""
        return {
            "configured": self.configured,
            "loaded": self._collection is not None,
            "backend": self._backend,
            "error": self._error,
            "model": self.settings.embedding_model,
        }

    def index_jobs(self, jobs: Iterable[Job]) -> int:
        """批量写入；返回成功条数，供管理员“重建索引”展示。"""
        success = 0
        for job in jobs:
            if self.upsert_job(job):
                success += 1
        return success


_vector_store: Optional[VectorStore] = None


def get_vector_store() -> VectorStore:
    """单例：模型只加载一次，多个请求共享。"""
    global _vector_store
    if _vector_store is None:
        _vector_store = VectorStore()
    return _vector_store
