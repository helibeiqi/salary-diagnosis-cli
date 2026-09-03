# -*- coding: utf-8 -*-
"""
src/agents_adapter.py — 把 11 个薪酬诊断工具封装为 OpenAI Agents SDK 的 Agent（备选编排后端）
================================================================================================

设计定位（与 src/mcp_adapter.py 同源、并列）
------------------------------------------------
两者都是「编排层可插拔」战略在 salary-diagnosis-cli 上的落地，共享同一套确定性引擎
（src/tools/registry.py 的 11 个工具）：
- mcp_adapter.py  把工具暴露给「任意 MCP 客户端」（Claude Desktop / WorkBuddy / Cursor）；
- agents_adapter.py 把工具暴露给「OpenAI Agents SDK」（Agent + Runner 的模型驱动
  function-calling 循环），作为 dsh 的 TS 插件那层胶水的**备选编排后端**。

关键纪律（与 MCP 适配器完全一致，红线不可破）
------------------------------------------------
1. 工具定义 100% 复用 registry.TOOL_SPECS + to_json_schema —— 绝不重复维护 schema。
2. 工具执行 100% 走 registry.call_tool —— 计算口径与路由契约（schemas/registry 主体）零改动。
3. 模型是「配置项」而非代码：provider 取 config.yaml 的 models.providers
   （ollama-local / vllm-local 等本地 provider 优先），默认回退到环境变量
   OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL，可指向**任意 OpenAI 兼容端点**。
   绝不硬编码云端模型、绝不自动上云（是否上云由 registry 内部数据分级 + config.yaml
   的 local 标记决定——本适配器只负责把工具交给编排器，碰不到全量薪资、不做算术）。
4. 本文件**不在顶层 import 任何云端 SDK**——`from agents import ...` 全部延迟到函数内，
   因此即使运行环境未安装 `openai-agents`，本模块也可被 import / py_compile，
   只有真正 build_agent / run 时才需要 SDK。

运行（需先安装 SDK）
--------------------
    pip install openai-agents
    # 指向本地 provider（以 config.yaml 的 ollama-local 为例，deepseek-r1:14b）
    export OPENAI_BASE_URL=http://localhost:11434/v1
    export OPENAI_API_KEY=sk-placeholder
    export OPENAI_MODEL=deepseek-r1:14b
    python src/agents_adapter.py "加载 data/sample_salary.csv，并做现状诊断"

校验（离线、无需 SDK）
----------------------
    python -c "import src.agents_adapter as a; a.build_agent_tools.__doc__"
    python -c "import src.agents_adapter as a; print(a._smoke_registry_call())"
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any, Dict, List, Optional

# —— 路径引导：保证无论从哪个 cwd 启动都能 import src.tools.registry ——
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from src.tools import registry  # noqa: E402

CONTRACT_VERSION = getattr(registry, "CONTRACT_VERSION", "0.0.0")

DEFAULT_INSTRUCTIONS = (
    "你是薪酬诊断助手，负责按用户意图调用薪酬诊断工具链。"
    "调用工具前先理解需求；load_salary_data 必须在其他分析工具之前调用；"
    "字段映射只修改 load_salary_data 返回的 ambiguous_columns 白名单内的列，其余照抄；"
    "结果以中文、结构化、便于 HR 领导阅读的方式呈现——不臆测、不编造数据。"
)


# ---------------------------------------------------------------------------
# 模型 / 端点解析（模型是配置项，fail-open）
# ---------------------------------------------------------------------------
def _resolve_model(provider: Optional[str] = None):
    """解析编排用的模型与端点。

    优先级：环境变量 OPENAI_MODEL/BASE_URL/KEY > config.yaml 的 models.providers。
    返回 (model, base_url, api_key)。任何缺失都有安全兜底，绝不因配置问题抛错。
    """
    model = os.environ.get("OPENAI_MODEL")
    base_url = os.environ.get("OPENAI_BASE_URL")
    api_key = os.environ.get("OPENAI_API_KEY")
    try:
        from src.tools import config_access
        cfg = config_access.load_config()
        models = cfg.get("models", {}) or {}
        prov_name = provider or models.get("default_provider")
        prov = (models.get("providers", {}) or {}).get(prov_name, {}) or {}
        model = model or prov.get("model")
        base_url = base_url or prov.get("base_url")
        if api_key is None and prov.get("api_key_env"):
            api_key = os.environ.get(prov["api_key_env"])
    except Exception:  # noqa: BLE001 - 配置不可用时 fail-open
        pass
    # 兜底默认值：本地 provider 场景下 api_key 可为占位符
    model = model or "gpt-4o-mini"
    base_url = base_url or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"
    api_key = api_key or os.environ.get("OPENAI_API_KEY") or "sk-placeholder"
    return model, base_url, api_key


# ---------------------------------------------------------------------------
# 工具封装：11 个 registry 工具 → OpenAI Agents SDK FunctionTool
# ---------------------------------------------------------------------------
def _make_on_invoke(tool_name: str):
    """为每个工具构造 on_invoke_tool：反序列化 JSON 参数 → registry.call_tool → 序列化信封。

    用 asyncio.to_thread 把同步的 registry.call_tool 放到线程跑，避免其内部的 pandas
    计算阻塞事件循环（Agent 循环串行 await 各工具，不会并发，线程安全无虞）。
    """
    async def on_invoke(ctx, input_json: str) -> str:
        args = json.loads(input_json) if input_json else {}
        envelope = await asyncio.to_thread(registry.call_tool, tool_name, args)
        return json.dumps(envelope, ensure_ascii=False, default=str)
    return on_invoke


def _new_function_tool(name: str, description: str, schema: Dict[str, Any], on_invoke) -> Any:
    """构造 FunctionTool，兼容 strict_json_schema 参数有无的 SDK 版本差异。"""
    try:
        from agents import FunctionTool
    except ImportError as exc:  # 未装 SDK 时给出可执行的提示
        raise RuntimeError(
            "未安装 OpenAI Agents SDK，请先 `pip install openai-agents` 后重试"
        ) from exc
    try:
        return FunctionTool(
            name=name,
            description=description,
            params_json_schema=schema,
            on_invoke_tool=on_invoke,
            strict_json_schema=False,  # 容忍嵌套 additionalProperties（如 mapping 对象）
        )
    except TypeError:
        # 旧版 SDK 无 strict_json_schema 形参
        return FunctionTool(
            name=name,
            description=description,
            params_json_schema=schema,
            on_invoke_tool=on_invoke,
        )


def build_agent_tools() -> List[Any]:
    """把 registry 的 11 个工具规格转为 OpenAI Agents SDK 的 FunctionTool 列表。"""
    tools: List[Any] = []
    for spec in registry.TOOL_SPECS:
        schema = registry.to_json_schema(spec.parameters)
        tools.append(_new_function_tool(
            name=spec.name,
            description=spec.description,
            schema=schema,
            on_invoke=_make_on_invoke(spec.name),
        ))
    return tools


def build_agent(provider: Optional[str] = None, instructions: str = DEFAULT_INSTRUCTIONS):
    """构建编排 Agent：模型来自 _resolve_model，工具来自 build_agent_tools。"""
    from agents import Agent
    model, base_url, api_key = _resolve_model(provider)
    # 注入到 OpenAI 客户端读取的环境变量（SDK 在构造 client 时读取）
    os.environ.setdefault("OPENAI_BASE_URL", base_url)
    os.environ.setdefault("OPENAI_API_KEY", api_key)
    tools = build_agent_tools()
    return Agent(name="salary-diagnosis-agent", instructions=instructions, tools=tools, model=model)


# ---------------------------------------------------------------------------
# 运行入口
# ---------------------------------------------------------------------------
async def run_query(prompt: str, provider: Optional[str] = None) -> str:
    """异步：用 Runner 跑一轮编排，返回模型最终文本输出。"""
    from agents import Runner
    agent = build_agent(provider)
    result = await Runner.run(agent, prompt)
    return result.final_output


def run(prompt: str, provider: Optional[str] = None) -> str:
    """同步入口：asyncio.run(run_query(...))。"""
    return asyncio.run(run_query(prompt, provider))


def _smoke_registry_call() -> Dict[str, Any]:
    """离线自检：验证适配器实际使用的 registry.call_tool 路径可用（无需 SDK）。"""
    envelope = registry.call_tool("load_salary_data", {"file_path": "data/sample_salary.csv"})
    return {"ok": envelope.get("ok"), "isError": envelope.get("isError"),
            "tool_count": len(registry.TOOL_SPECS)}


def main() -> None:
    prompt = sys.argv[1] if len(sys.argv) > 1 else "加载 data/sample_salary.csv 并做现状诊断"
    provider = sys.argv[2] if len(sys.argv) > 2 else None
    print(run(prompt, provider))


if __name__ == "__main__":
    main()
