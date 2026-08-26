"""ComfyUI HTTP 客户端：异步请求 + 模型/LoRA/CLIP 清单自动同步缓存.

2026-08-11 v2.0.0 重构：
- 改用 httpx.AsyncClient，全部异步，避免阻塞 AstrBot 事件循环
- 资源清单（UNET/LoRA/CLIP）自动同步并缓存到插件数据目录，离线回退缓存
- 端点/端口/超时由外部（配置）注入，不再写死
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx

from astrbot.api import logger

# ZeroTier 虚拟局域网偶尔抽风（瞬时丢包/路径切换），这类连接级异常可安全重试：
# - ConnectError/ConnectTimeout：TCP 连接未建立，请求肯定没发出去，重试无副作用
# - ReadTimeout：GET 幂等可重试；但 POST /prompt 例外（响应丢了不代表服务端没收到，
#   重试可能重复提交出双图），提交只对"连接未建立"类异常重试
RETRYABLE_EXC: tuple[type[Exception], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
)
SUBMIT_RETRYABLE_EXC: tuple[type[Exception], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
)
# 重试次数与退避（秒）：attempts 次尝试，间隔 backoff * 2^i
RETRY_ATTEMPTS = 3
RETRY_BACKOFF = 0.8


# /object_info 中各节点类的资源字段（class_type -> 字段名）
RESOURCE_FIELDS: dict[str, str] = {
    "UNETLoader": "unet_name",
    "LoraLoaderModelOnly": "lora_name",
    "CLIPLoader": "clip_name",
    "VAELoader": "vae_name",
}

# rgthree 的 Power Lora Loader：插槽字段 lora_1..lora_N 里也提供可选 LoRA 列表
POWER_LORA_CLASS = "Power Lora Loader (rgthree)"
# 模型文件扩展名（识别 object_info 里的资源文件名）
MODEL_EXTS = (".safetensors", ".ckpt", ".pt", ".pth", ".sft", ".bin")


class ComfyUIClient:
    """封装与 ComfyUI 的全部 HTTP 交互。"""

    def __init__(
        self,
        host: str,
        port: int,
        timeout: float = 300.0,
        cache_file: Path | None = None,
        cache_ttl: int = 600,
    ) -> None:
        self.host = host
        self.port = port
        self.base_url = f"http://{host}:{port}"
        self.timeout = timeout
        self.cache_file = cache_file
        self.cache_ttl = cache_ttl
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url, timeout=httpx.Timeout(self.timeout)
            )
        return self._client

    async def close(self) -> None:
        """关闭底层连接，插件卸载（terminate）时调用。"""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request_with_retry(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
        timeout: float | None = None,
        attempts: int = RETRY_ATTEMPTS,
        retry_exc: tuple[type[Exception], ...] = RETRYABLE_EXC,
        label: str = "",
    ) -> httpx.Response | None:
        """带退避重试的 HTTP 请求；仅对连接级异常（ZeroTier 抽风）重试。

        4xx/5xx 等明确失败不重试。最终失败返回 None。
        """
        last_exc: Exception | None = None
        for i in range(attempts):
            try:
                resp = await self.client.request(
                    method, path, params=params, json=json_body, timeout=timeout
                )
                resp.raise_for_status()
                return resp
            except retry_exc as e:
                last_exc = e
                if i < attempts - 1:
                    await asyncio.sleep(RETRY_BACKOFF * (2**i))
                    continue
            except httpx.HTTPError as e:
                last_exc = e
                break
        where = f" {label}" if label else ""
        logger.error(
            f"[ComfyUIDirect] {method} {self.base_url}{path}{where} 失败 "
            f"({attempts} 次尝试): {last_exc}"
        )
        return None

    async def _get(self, path: str, params: dict | None = None) -> Any:
        """GET（幂等，自动重试），失败返回 None。"""
        resp = await self._request_with_retry("GET", path, params=params)
        if resp is None:
            return None
        try:
            return resp.json()
        except ValueError as e:
            # 空响应体/非 JSON（如 ComfyUI 刚启动未就绪时）按失败处理
            logger.warning(f"[ComfyUIDirect] GET {self.base_url}{path} 响应非 JSON: {e}")
            return None

    async def get_object_info(self) -> dict | None:
        return await self._get("/object_info")

    async def ping(self, timeout: float = 5.0) -> bool:
        """轻量连通性探测（用于 WebUI 状态灯），ZeroTier 抽风时重试 2 次。"""
        for i in range(2):
            try:
                resp = await self.client.get("/system_stats", timeout=timeout)
                if resp.status_code == 200:
                    return True
            except RETRYABLE_EXC:
                if i == 0:
                    await asyncio.sleep(0.8)
                    continue
            except httpx.HTTPError:
                pass
            return False
        return False

    async def submit_prompt_detail(self, workflow: dict) -> tuple[str | None, str | None]:
        """提交工作流，返回 (prompt_id, 错误信息)。成功时错误信息为 None。

        400 时解析 ComfyUI 的 {"error": {...}, "node_errors": {...}} 结构，
        把真实失败原因（缺节点/模型不存在等）带回来，而不是笼统报"无法连接"。

        重试策略（ZeroTier 抽风防护）：只对"TCP 连接未建立"的异常重试
        （ConnectError/ConnectTimeout = 请求肯定没发出去，安全）；ReadTimeout
        不重试——服务端可能已收下任务，重试会重复出图，改为提示"可能已提交"。
        """
        last_err: Exception | None = None
        for i in range(2):
            try:
                resp = await self.client.post("/prompt", json={"prompt": workflow})
            except SUBMIT_RETRYABLE_EXC as e:
                last_err = e
                if i == 0:
                    logger.warning(
                        f"[ComfyUIDirect] POST /prompt 连接失败（ZeroTier 抽风?），"
                        f"重试: {e}"
                    )
                    await asyncio.sleep(1.2)
                    continue
            except httpx.ReadTimeout as e:
                # 响应超时：任务可能已提交，不重试，让上层查队列确认
                logger.error(
                    f"[ComfyUIDirect] POST /prompt 响应超时，任务可能已提交，"
                    f"请查队列确认: {e}"
                )
                return None, f"提交请求超时，任务可能已在排队（请用 comfyui_queue 确认是否重复）"
            except httpx.HTTPError as e:
                last_err = e
                break
            break
        if last_err is not None:
            logger.error(f"[ComfyUIDirect] POST {self.base_url}/prompt 失败: {last_err}")
            return None, f"无法连接 ComfyUI（{self.base_url}）：{last_err}"
        if resp.status_code != 200:
            msg = f"ComfyUI 返回 HTTP {resp.status_code}"
            try:
                body = resp.json()
                err = body.get("error", {})
                node_errors = body.get("node_errors") or {}
                if err:
                    msg = f"{err.get('message', msg)}"
                if node_errors:
                    detail = next(iter(node_errors.values()))
                    if isinstance(detail, dict) and detail.get("errors"):
                        e0 = detail["errors"][0]
                        msg += f"（节点 {e0.get('node_id', '?')}: {e0.get('message', '?')}）"
            except (ValueError, AttributeError):
                pass
            logger.error(f"[ComfyUIDirect] 提交失败: {msg}")
            return None, msg
        try:
            data = resp.json()
        except ValueError:
            return None, "ComfyUI 响应格式异常"
        pid = data.get("prompt_id")
        if not pid:
            logger.error("[ComfyUIDirect] 未获取到 prompt_id")
            return None, "ComfyUI 未返回 prompt_id"
        return pid, None

    async def poll_history(self, prompt_id: str) -> dict | None:
        """查询执行历史（outputs 部分）；未完成/不存在返回 None。"""
        history = await self._get(f"/history/{prompt_id}")
        if history and prompt_id in history:
            return history[prompt_id].get("outputs", {})
        return None

    async def get_history_entry(self, prompt_id: str) -> dict | None:
        """查询完整历史条目（含 status/message，用于识别执行失败）。"""
        history = await self._get(f"/history/{prompt_id}")
        if history and prompt_id in history:
            return history[prompt_id]
        return None

    async def list_history(self, max_items: int = 12) -> list[dict]:
        """最近执行记录，用于 WebUI 从 ComfyUI 导入工作流。"""
        history = await self._get("/history")
        if not isinstance(history, dict):
            return []
        rows: list[dict] = []
        for pid, entry in history.items():
            if not isinstance(entry, dict):
                continue
            prompt = entry.get("prompt")
            number = 0
            wf = None
            if isinstance(prompt, list) and len(prompt) >= 3:
                try:
                    number = int(prompt[0])
                except (TypeError, ValueError):
                    number = 0
                if isinstance(prompt[2], dict):
                    wf = prompt[2]
            rows.append(
                {
                    "prompt_id": pid,
                    "number": number,
                    "status": (entry.get("status") or {}).get("status_str") or "",
                    "workflow": wf,
                    "has_workflow": isinstance(wf, dict) and bool(wf),
                }
            )
        rows.sort(key=lambda r: r.get("number") or 0, reverse=True)
        return rows[:max_items]

    async def get_queue(self) -> dict | None:
        """GET /queue → {"queue_running": [...], "queue_pending": [...]}。"""
        return await self._get("/queue")

    async def get_system_stats(self) -> dict | None:
        """GET /system_stats → 系统/设备/显存信息。"""
        return await self._get("/system_stats")

    # /view_metadata 各资源类型的候选目录（safetensors 头部元数据）
    METADATA_FOLDERS: dict[str, list[str]] = {
        "unet_name": ["unet", "checkpoints"],
        "lora_name": ["loras"],
        "clip_name": ["clip"],
        "vae_name": ["vae"],
    }

    async def get_model_metadata(
        self, filename: str, folders: list[str] | None = None
    ) -> dict | None:
        """GET /view_metadata/{folder}?filename=：读 safetensors 头部 __metadata__。

        按资源目录候选逐个尝试；无元数据头/文件不存在/空响应返回 None。
        folders 传 None 时尝试全部资源目录，否则只试指定目录（避免逐目录 404）。
        """
        if folders is None:
            folders = [f for names in self.METADATA_FOLDERS.values() for f in names]
        for folder in folders:
            meta = await self._get(f"/view_metadata/{folder}", {"filename": filename})
            if meta:
                return meta
        return None

    # ------------------------------------------------------------------
    # LoRA 触发词：从 safetensors 头部元数据提取，随清单缓存
    # ------------------------------------------------------------------

    # 触发词候选元数据键（civitai 训练器写入 __metadata__）
    TRIGGER_KEYS = ("ss_activation_tags", "ss_tag_frequency", "ss_dataset_tags")
    # tag_frequency 按出现次数取 top N 展示
    TRIGGER_TOP_N = 12

    @staticmethod
    def _extract_trigger_words(meta: dict | None) -> tuple[list[str], str]:
        """从 safetensors 头部元数据提取 LoRA 触发词。

        优先级：ss_activation_tags（作者显式指定）> ss_tag_frequency（按频率 top N）
        > ss_dataset_tags（训练集全部 tag 截断）。返回 (触发词列表, 来源标识)。
        """
        if not meta:
            return [], ""

        def _clean(tag: str) -> str:
            # 清洗训练器写入的脏字符（BOM/零宽/空白）
            return tag.strip(" \ufeff\u200b\u200c").strip()

        activation = meta.get("ss_activation_tags")
        if activation:
            tags = [_clean(t) for t in str(activation).split(",") if _clean(t)]
            if tags:
                return tags, "activation"
        freq = meta.get("ss_tag_frequency")
        if freq:
            try:
                data = json.loads(freq) if isinstance(freq, str) else freq
                if isinstance(data, dict):
                    def _cnt(v: Any) -> float:
                        try:
                            return float(v)
                        except (TypeError, ValueError):
                            return 0.0
                    counts: dict[str, float] = {}
                    for k, v in data.items():
                        if isinstance(v, dict):
                            # kohya 标准格式: {class: {tag: count}}，合并各 class
                            for t, c in v.items():
                                t = _clean(t)
                                if t:
                                    counts[t] = counts.get(t, 0.0) + _cnt(c)
                        else:
                            k = _clean(k)
                            if k:
                                counts[k] = counts.get(k, 0.0) + _cnt(v)
                    ranked = sorted(counts.items(), key=lambda x: x[1], reverse=True)
                    # 频率 >1 的才可能是激活词（频率 1 多为杂项 tag）
                    tags = [
                        t
                        for t, c in ranked[: ComfyUIClient.TRIGGER_TOP_N]
                        if c > 1
                    ]
                    if tags:
                        return tags, "tag_frequency"
            except (ValueError, TypeError):
                pass
        dataset = meta.get("ss_dataset_tags")
        if dataset:
            tags = [_clean(t) for t in str(dataset).split(",") if _clean(t)]
            if tags:
                return tags[: ComfyUIClient.TRIGGER_TOP_N], "dataset"
        return [], ""

    async def _fetch_lora_trigger_words(
        self, lora_names: list[str]
    ) -> dict[str, dict]:
        """并发读各 LoRA 的 safetensors 头部，提取触发词。

        返回 {文件名: {"trigger_words": [...], "source": "activation|tag_frequency|dataset"}}。
        单个失败只记 warning 跳过，不阻塞整个清单同步。
        """
        sem = asyncio.Semaphore(8)
        out: dict[str, dict] = {}

        async def one(name: str) -> None:
            async with sem:
                try:
                    meta = await self.get_model_metadata(name, folders=["loras"])
                    words, source = self._extract_trigger_words(meta)
                    if words:
                        out[name] = {"trigger_words": words, "source": source}
                except Exception as e:
                    logger.warning(f"[ComfyUIDirect] 读取 {name} 触发词失败: {e}")

        await asyncio.gather(*(one(n) for n in lora_names))
        return out

    async def get_embeddings(self) -> list[str] | None:
        """GET /embeddings → 嵌入模型名列表（去扩展名）。"""
        return await self._get("/embeddings")

    async def free_memory(self, unload_models: bool = True, free_cache: bool = True) -> bool:
        """POST /free：卸载模型/清空执行器缓存，释放显存。"""
        body: dict = {}
        if unload_models:
            body["unload_models"] = True
        if free_cache:
            body["free_memory"] = True
        try:
            resp = await self.client.post("/free", json=body)
            return resp.status_code == 200
        except httpx.HTTPError as e:
            logger.error(f"[ComfyUIDirect] 释放显存失败: {e}")
            return False

    async def upload_image(self, filename: str, content: bytes) -> tuple[str | None, str | None]:
        """POST /upload/image：上传图片到 ComfyUI input 目录，返回 (文件名, 错误)。"""
        try:
            resp = await self.client.post(
                "/upload/image",
                files={"image": (filename, content, "image/png")},
                data={"overwrite": "true"},
            )
        except httpx.HTTPError as e:
            logger.error(f"[ComfyUIDirect] 上传图片失败: {e}")
            return None, f"上传失败：{e}"
        if resp.status_code != 200:
            return None, f"ComfyUI 返回 HTTP {resp.status_code}"
        try:
            data = resp.json()
        except ValueError:
            return None, "ComfyUI 响应格式异常"
        name = data.get("name") or data.get("subfolder")
        if not name:
            return None, "ComfyUI 未返回文件名"
        return name, None

    async def list_models_folder(self, folder: str) -> list[str] | None:
        """GET /models/{folder}：列出某目录下的模型文件。folder 如 loras/checkpoints。"""
        resp = await self._request_with_retry(
            "GET", f"/models/{folder}", label=f"列模型 {folder}"
        )
        if resp is None:
            return None
        try:
            data = resp.json()
        except ValueError:
            return None
        if not isinstance(data, list):
            return None
        out = []
        for it in data:
            name = it if isinstance(it, str) else (it or {}).get("name")
            if isinstance(name, str) and name.endswith(MODEL_EXTS):
                out.append(name)
        return out

    async def interrupt(self, prompt_id: str | None = None) -> bool:
        """POST /interrupt：带 prompt_id 时定向中断该任务（新版），否则全局中断。"""
        try:
            resp = await self.client.post(
                "/interrupt",
                json={"prompt_id": prompt_id} if prompt_id else {},
            )
            return resp.status_code == 200
        except httpx.HTTPError as e:
            logger.error(f"[ComfyUIDirect] 中断请求失败: {e}")
            return False

    async def delete_queue_items(self, prompt_ids: list[str]) -> bool:
        """POST /queue {"delete": [...]}：从待执行队列移除任务（对运行中任务无效）。"""
        if not prompt_ids:
            return True
        try:
            resp = await self.client.post("/queue", json={"delete": prompt_ids})
            return resp.status_code == 200
        except httpx.HTTPError as e:
            logger.error(f"[ComfyUIDirect] 移除队列任务失败: {e}")
            return False

    async def download_image(
        self,
        filename: str,
        subfolder: str = "",
        preview: str | None = None,
    ) -> bytes | None:
        """从 ComfyUI /view 下载图片（GET 幂等，ZeroTier 抽风时自动重试）。

        preview 形如 "webp;80" / "jpeg;80"：让服务端重编码小尺寸预览
        （WebUI 试跑面板用，省带宽）；None 返回原图。
        """
        params: dict = {"filename": filename, "type": "output"}
        if subfolder:
            params["subfolder"] = subfolder
        if preview:
            params["preview"] = preview
        resp = await self._request_with_retry(
            "GET", "/view", params=params, label=f"下载 {filename}"
        )
        if resp is None:
            return None
        return resp.content

    # ------------------------------------------------------------------
    # 模型/LoRA/CLIP 资源清单：自动同步 + 本地缓存
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_resources(obj: dict) -> dict[str, list[str]]:
        """从 /object_info 提取各节点类的资源清单。"""
        out: dict[str, list[str]] = {}
        for cls, field in RESOURCE_FIELDS.items():
            info = obj.get(cls, {})
            raw = info.get("input", {}).get("required", {}).get(field, [])
            # object_info 返回格式: [ [model1, model2, ...] ]
            if isinstance(raw, list) and raw and isinstance(raw[0], list):
                out[field] = list(raw[0])
            else:
                out[field] = []
        # rgthree 的 Power Lora Loader：lora 插槽是嵌套结构（如 lora_1 里含 "lora": [[...]]），
        # 递归收集所有含 lora 键名的模型文件名
        pl = obj.get(POWER_LORA_CLASS, {})
        for key, val in pl.get("input", {}).get("required", {}).items():
            if "lora" not in key.lower():
                continue
            if (
                isinstance(val, list)
                and val
                and isinstance(val[0], list)
            ):
                for name in val[0]:
                    if isinstance(name, str) and name.endswith(MODEL_EXTS) and name not in out["lora_name"]:
                        out["lora_name"].append(name)
            elif isinstance(val, dict):
                for sub_key, sub_val in val.items():
                    if "lora" not in sub_key.lower():
                        continue
                    if isinstance(sub_val, list) and sub_val and isinstance(sub_val[0], list):
                        for name in sub_val[0]:
                            if isinstance(name, str) and name.endswith(MODEL_EXTS) and name not in out["lora_name"]:
                                out["lora_name"].append(name)
        return out

    def _load_cache(self) -> dict | None:
        if not self.cache_file or not self.cache_file.exists():
            return None
        try:
            return json.loads(self.cache_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"[ComfyUIDirect] 读取模型缓存失败: {e}")
            return None

    def _save_cache(self, data: dict) -> None:
        if not self.cache_file:
            return
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            # 原子写：先写临时文件再 rename，避免并发预热/查询时写坏缓存
            tmp = self.cache_file.with_name(self.cache_file.name + ".tmp")
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(self.cache_file)
        except OSError as e:
            logger.warning(f"[ComfyUIDirect] 写入模型缓存失败: {e}")

    async def _fetch_resources(self) -> dict | None:
        """从 ComfyUI 拉取最新资源清单；任何失败（含空响应/超时）返回 None。

        单独包裹：即使某个端点异常（如 /embeddings 空响应），也不允许把异常
        抛出到工具调用层——宁可回退缓存。
        """
        try:
            obj = await self.get_object_info()
            if not obj:
                return None
            resources = self._extract_resources(obj)
            try:
                embeddings = await self.get_embeddings()
                resources["embeddings"] = embeddings or []
            except Exception as e:
                logger.warning(f"[ComfyUIDirect] 获取 embeddings 失败，忽略: {e}")
                resources["embeddings"] = []
            # LoRA 触发词：读 safetensors 头部元数据，失败不阻塞清单
            try:
                resources["lora_meta"] = await self._fetch_lora_trigger_words(
                    resources.get("lora_name", [])
                )
            except Exception as e:
                logger.warning(f"[ComfyUIDirect] 同步 LoRA 触发词失败，忽略: {e}")
                resources["lora_meta"] = {}
            return resources
        except Exception as e:
            logger.error(f"[ComfyUIDirect] 资源同步失败: {e}")
            return None

    async def list_resources(
        self, force_refresh: bool = False
    ) -> tuple[dict[str, list[str]], bool]:
        """返回 (资源清单, 是否来自缓存)。

        未过期且非强制刷新时直接用缓存；过期则重新从 ComfyUI 同步；
        ComfyUI 离线时回退本地缓存，保证清单不因本机关机而丢失。
        """
        if not force_refresh and self.cache_file and self.cache_file.exists():
            cached = self._load_cache()
            if cached and (
                self.cache_ttl <= 0
                or time.time() - cached.get("fetched_at", 0) < self.cache_ttl
            ):
                return cached["resources"], True

        resources = await self._fetch_resources()
        if resources is not None:
            data = {"fetched_at": time.time(), "resources": resources}
            self._save_cache(data)
            return data["resources"], False

        cached = self._load_cache()
        if cached:
            logger.warning("[ComfyUIDirect] ComfyUI 离线，回退本地缓存模型清单")
            return cached["resources"], True
        return {
            "unet_name": [],
            "lora_name": [],
            "clip_name": [],
            "vae_name": [],
            "embeddings": [],
        }, False

    async def warm_up_cache(self) -> None:
        """插件加载时后台预热资源清单（自动同步）。

        ComfyUI 刚启动时 /object_info 可能尚未就绪（空响应），最多重试 3 次。
        """
        for attempt in range(1, 4):
            try:
                resources = await self._fetch_resources()
                if resources is not None:
                    self._save_cache({"fetched_at": time.time(), "resources": resources})
                    return
            except Exception as e:
                logger.warning(
                    f"[ComfyUIDirect] 预热资源清单失败（第 {attempt}/3 次）: {e}"
                )
            if attempt < 3:
                await asyncio.sleep(8)
        logger.warning("[ComfyUIDirect] 预热资源清单失败（3 次尝试后放弃，将按需同步）")
