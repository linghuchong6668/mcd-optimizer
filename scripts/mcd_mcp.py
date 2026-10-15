"""麦当劳 MCP Server 的 Streamable HTTP 客户端（仅使用 Python 标准库）。

设计要点：
- 严格遵循官方接入方式：POST https://mcp.mcd.cn，Authorization: Bearer <TOKEN>
- Token 只从环境变量读取，绝不落盘、绝不写进日志（满足参赛信息安全声明）
- 内置本地缓存与 429 限流退避，避免重复请求触发风控
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

ENDPOINT = os.environ.get("MCD_MCP_URL", "https://mcp.mcd.cn")
CACHE_DIR = Path(__file__).resolve().parent.parent / ".cache"
DEFAULT_TIMEOUT = 60


class McdMcpError(RuntimeError):
    """MCP 调用失败。"""


def _token() -> str:
    token = os.environ.get("MCD_MCP_TOKEN", "").strip()
    if not token:
        raise McdMcpError(
            "未检测到 MCP Token。请先 export MCD_MCP_TOKEN=<你的麦当劳 MCP Token>，"
            "获取方式见 https://github.com/M-China/mcd-mcp-server"
        )
    return token


def _cache_path(tool: str, arguments: dict) -> Path:
    raw = json.dumps({"tool": tool, "args": arguments}, sort_keys=True, ensure_ascii=False)
    return CACHE_DIR / (hashlib.sha1(raw.encode()).hexdigest() + ".json")


def raw_call(tool: str, arguments: dict | None = None, _id: int = 1) -> dict:
    """发起一次 MCP tools/call 请求，返回服务端 JSON-RPC 结果中的结构化数据。"""
    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": _id,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments or {}},
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        ENDPOINT,
        data=payload,
        headers={
            "Authorization": f"Bearer {_token()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )

    last_error: Exception | None = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT) as response:
                body = json.loads(response.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:  # 429 限流等
            last_error = exc
            if exc.code == 429:
                time.sleep(2 ** attempt)
                continue
            raise McdMcpError(f"MCP 返回 HTTP {exc.code}: {exc.read().decode('utf-8', 'ignore')[:300]}") from exc
        except Exception as exc:  # noqa: BLE001 网络抖动
            last_error = exc
            time.sleep(1 + attempt)
    else:
        raise McdMcpError(f"MCP 调用失败（{tool}）：{last_error}")

    if "error" in body:
        raise McdMcpError(f"MCP 工具 {tool} 返回错误：{body['error']}")

    result = body.get("result", {})
    structured = result.get("structuredContent") or {}
    if structured:
        if structured.get("success") is False:
            raise McdMcpError(f"工具 {tool} 业务失败：{structured.get('message')}（code={structured.get('code')}）")
        return structured.get("data")
    # 部分工具只返回文本，交由上层解析
    text = "\n".join(c.get("text", "") for c in result.get("content", []) if isinstance(c, dict))
    return {"_text": text}


def call(tool: str, arguments: dict | None = None, cache_ttl: int = 0) -> dict:
    """带本地缓存的 MCP 调用。cache_ttl > 0 时启用磁盘缓存（秒）。"""
    arguments = arguments or {}
    if cache_ttl > 0:
        path = _cache_path(tool, arguments)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        if path.exists() and time.time() - path.stat().st_mtime < cache_ttl:
            return json.loads(path.read_text(encoding="utf-8"))
        data = raw_call(tool, arguments)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return data
    return raw_call(tool, arguments)
