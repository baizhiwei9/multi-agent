"""统一模型调用层：一个动作 `llm/chat`，两种后端（HTTP / MCP），默认不烧 token。

为什么单独抽一层
--------------
之前三处模型调用各写各的（`audit.py::llm_from_env`、`game_server_spec.py` 的
`GAME_TEST_LLM_URL`、`adversary.py::decide` 复用前者），都读环境变量、都要自己处理
"没配怎么办""回包解析失败怎么办"。换一个端点就要改三处。

统一之后：

    流水线步骤：{"capability": "llm", "action": "chat",
                "params": {"prompt": "...", "system": "...", "backend": "http"}}

调用方只认 `llm/chat`，后端怎么连是环境变量的事。

两种后端
-------
| backend | 配置 | 说明 |
|---|---|---|
| `http` | `LLM_URL`（或复用 `AUDIT_LLM_URL`）、`LLM_MODEL`、`LLM_TOKEN` | OpenAI 兼容 `/v1/chat/completions` |
| `mcp`  | `LLM_MCP_COMMAND`、`LLM_MCP_TOOL` | 起一个 MCP server 子进程，`tools/call` 一问一答 |

选后端的优先级：参数 `backend` → 环境变量 `LLM_BACKEND` → 有 `LLM_MCP_COMMAND` 就用 mcp → 否则 http。

三条硬规矩
----------
1. **失败不炸链**：任何异常（没配、超时、回包解析失败）都返回 `degraded=True` + `reason`，
   由调用方决定降级（例如退回规则模板），绝不让整条流水线失败；
2. **限额在 limits 里**：`llm.max_prompt_chars` / `max_output_chars` / `timeout_s`，
   超了截断并标记，不静默放过；
3. **不写仓库**：本层只出文本。要改代码，让模型产 `edits` JSON，由零 token 的
   apply/语法/回放门去落地（见 `integrations/game_server_bugfix.py` 的 `propose`）。

MCP 的成本要说清
--------------
`mcp` 后端每次调用会起一个子进程（`MCPClient.start` → `tools/call` → `close`），
适合"一次问答"，不适合高频短调用；要高频就把 MCP server 挂在 `mcp-agent` 上常驻，
再用 Hub 路由（见 `multi_agent/mcp.py::MCPAgent`）。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Optional, Tuple

LLM_ACTIONS = ("chat",)

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_CONFIG_REL = os.path.join("config", "llm.json")
_LOCAL_CONFIG = "llm.local.json"   # 与主配置同目录，不进仓库，专门放 Key
_BUILTIN_CONFIG = {
    "enabled": False,
    "default": {"backend": "http", "url": "", "model": "", "token": "",
                "timeout_s": 120},
    "profiles": {},
    "mcp": {"command": "", "tool": ""},
}


def config_path() -> str:
    """模型配置文件位置：`MULTI_AGENT_LLM_CONFIG` → 仓库 `config/llm.json`。"""
    env = os.environ.get("MULTI_AGENT_LLM_CONFIG")
    if env:
        return env
    return os.path.join(_REPO_ROOT, _CONFIG_REL)


def _read_json(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _merge(base: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
    """逐键合并；`default` / `mcp` 深一层，`profiles` 深两层（按 profile 名合并）。"""
    out = dict(base)
    for key, value in extra.items():
        if key in ("default", "mcp") and isinstance(value, dict):
            out[key] = {**(out.get(key) or {}), **value}
        elif key == "profiles" and isinstance(value, dict):
            profiles = dict(out.get("profiles") or {})
            for name, prof in value.items():
                if isinstance(prof, dict):
                    profiles[name] = {**(profiles.get(name) or {}), **prof}
                else:
                    profiles[name] = prof
            out["profiles"] = profiles
        else:
            out[key] = value
    return out


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    """读模型配置：内置默认 ← `config/llm.json` ← 同目录 `llm.local.json`。

    本地覆盖文件的用途是**直接填 Key**：主配置里 `token` 写 `${ENV:LLM_TOKEN}` 占位，
    真正的 Key 放 `config/llm.local.json`（已在 `.gitignore`），不用 export、也不会误提交。
    也可以反过来——主配置留空、只在本地文件里写 `{"profiles": {"chat": {"token": "sk-..."}}}`。

    显式传 `path` 时不合并本地文件（测试与多环境切换用）。
    """
    target = path or config_path()
    out: Dict[str, Any] = {}
    for key, value in _BUILTIN_CONFIG.items():
        out[key] = dict(value) if isinstance(value, dict) else value
    out = _merge(out, _read_json(target))

    local = ""
    if not path:
        local = os.path.join(os.path.dirname(os.path.abspath(target)), _LOCAL_CONFIG)
        if os.path.isfile(local):
            out = _merge(out, _read_json(local))
        else:
            local = ""
    out["_path"] = target
    out["_local_path"] = local
    return out


def _deref(value: Any) -> str:
    """`${ENV:VAR}` → 环境变量值；其它原样返回（所以可以直接填 Key）。"""
    if not isinstance(value, str):
        return "" if value is None else str(value)
    text = value.strip()
    if text.startswith("${ENV:") and text.endswith("}"):
        name = text[6:-1].strip()
        return os.environ.get(name, "")
    return text


def _resolve_token(value: Any) -> Tuple[str, str]:
    """返回 `(Key, 来源)`。来源取值：`inline`（配置文件里直接写）/ `env:VAR` / `none`。"""
    raw = _deref(value).strip()
    if not raw:
        return "", "none"
    text = str(value).strip() if isinstance(value, str) else ""
    if text.startswith("${ENV:"):
        name = text[6:-1].strip()
        return raw, f"env:{name}"
    return raw, "inline"


def profile_config(name: str = "chat") -> Dict[str, Any]:
    """`default` + `profiles[name]` 合并并解引用；返回 `{backend,url,model,token,token_source,...}`。

    `token` 没在配置里填时，按顺序回落 `LLM_TOKEN` / `AUDIT_LLM_TOKEN`（兼容旧部署）。
    """
    cfg = load_config()
    merged = dict(cfg.get("default") or {})
    prof = (cfg.get("profiles") or {}).get(name)
    if isinstance(prof, dict):
        merged.update({k: v for k, v in prof.items() if not str(k).startswith("_")})
    token, token_source = _resolve_token(merged.get("token"))
    if not token:
        for var in ("LLM_TOKEN", "AUDIT_LLM_TOKEN"):
            if os.environ.get(var):
                token, token_source = os.environ[var], f"env:{var}"
                break
    out = {
        "backend": str(_deref(merged.get("backend")) or "http").lower(),
        "url": _deref(merged.get("url")),
        "model": _deref(merged.get("model")),
        "token": token,
        "token_source": token_source,
        "timeout_s": merged.get("timeout_s"),
    }
    mcp = cfg.get("mcp") or {}
    out["command"] = _deref(mcp.get("command"))
    out["tool"] = _deref(mcp.get("tool"))
    return out


def describe_config() -> Dict[str, Any]:
    """给 CLI / 文档用：哪些 profile 配了端点、Key 从哪来（**不回显 Key**）。"""
    cfg = load_config()
    profiles: Dict[str, Any] = {}
    for name in sorted(set(list((cfg.get("profiles") or {}).keys()) + ["chat"])):
        prof = profile_config(name)
        profiles[name] = {
            "backend": prof["backend"],
            "url": prof["url"],
            "model": prof["model"],
            "token_env": ("configured" if prof["token"] else ""),
            "token_source": prof["token_source"],
        }
    return {"path": cfg.get("_path"), "local_path": cfg.get("_local_path") or "",
            "enabled": bool(cfg.get("enabled")),
            "profiles": profiles,
            "mcp": {"command": bool(profile_config()["command"]),
                    "tool": bool(profile_config()["tool"])}}


def status() -> Dict[str, Any]:
    """自检入口（CLI `llm-status`）：配置在哪、本地 Key 文件有没有、谁配齐了。

    只报"有没有 / 从哪来"，不回显 Key 本身。
    """
    info = describe_config()
    ready = [n for n, p in info["profiles"].items() if p["url"]]
    info["ready_profiles"] = ready
    info["hint"] = (
        "没有可用端点：把 url/model 填进 config/llm.json，Key 写 config/llm.local.json"
        "（或 export LLM_TOKEN）；也可以在 llm.local.json 里整块覆盖 profiles.chat。"
    )
    return info


def _limits():
    from ..limits import load_limits

    return load_limits().llm


# ═══════════════════════════════════════════════════════════ 后端
def http_caller(url: Optional[str] = None, model: Optional[str] = None,
                token: Optional[str] = None,
                timeout: Optional[float] = None,
                profile: str = "chat") -> Optional[Callable[[str], str]]:
    """OpenAI 兼容端点的调用器。

    取值优先级：显式参数 → 配置文件 `profiles[name]` → 旧环境变量
    （`LLM_URL` / `AUDIT_LLM_URL`）。Key 只从 `${ENV:...}` 或环境变量取，配置文件里不存明文。
    """
    cfg = profile_config(profile)
    url = url or cfg.get("url") or os.environ.get("LLM_URL") or os.environ.get("AUDIT_LLM_URL")
    if not url:
        return None
    url = _normalize_url(url)
    model = (model or cfg.get("model") or os.environ.get("LLM_MODEL")
             or os.environ.get("AUDIT_LLM_MODEL", ""))
    token = (token or cfg.get("token") or os.environ.get("LLM_TOKEN")
             or os.environ.get("AUDIT_LLM_TOKEN", ""))
    timeout = float(timeout or _limits().timeout_s)

    def _call(prompt: str) -> str:
        body = json.dumps({"model": model, "stream": False,
                           "messages": [{"role": "user", "content": prompt}]},
                          ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, data=body, method="POST", headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", errors="replace"))
        try:
            return str(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError):
            return json.dumps(data, ensure_ascii=False)
    return _call


def _normalize_url(url: str) -> str:
    """容错：给 base_url 也认，自动补 `/v1/chat/completions`。"""
    u = str(url).strip().rstrip("/")
    if u.endswith("/chat/completions"):
        return u
    if u.endswith("/v1"):
        return u + "/chat/completions"
    return u + "/v1/chat/completions" if "/v1" not in u else u


def mcp_caller(command: Optional[str] = None, tool: Optional[str] = None,
               timeout: Optional[float] = None) -> Optional[Callable[[str], str]]:
    """MCP stdio server 的调用器：`tools/call` 一个工具，取回文本。"""
    cfg = profile_config()
    command = command or cfg.get("command") or os.environ.get("LLM_MCP_COMMAND")
    tool = tool or cfg.get("tool") or os.environ.get("LLM_MCP_TOOL")
    if not command or not tool:
        return None
    timeout = float(timeout or _limits().timeout_s)

    def _call(prompt: str) -> str:
        from .mcp import MCPClient

        client = MCPClient(command, timeout=min(30.0, timeout))
        try:
            client.start()
            res = client.call_tool(tool, {"prompt": prompt})
            return _text_from_mcp(res)
        finally:
            client.close()
    return _call


def _text_from_mcp(res: Any) -> str:
    if isinstance(res, str):
        return res
    if isinstance(res, dict):
        blocks = res.get("content")
        if isinstance(blocks, list):
            parts = [str(b.get("text") or "") for b in blocks
                     if isinstance(b, dict) and b.get("type", "text") == "text"]
            if any(parts):
                return "\n".join(p for p in parts if p)
        for key in ("text", "output", "result", "message"):
            if isinstance(res.get(key), str):
                return res[key]
    return json.dumps(res, ensure_ascii=False)


def resolve(params: Optional[Dict[str, Any]] = None,
            caller: Optional[Callable[[str], str]] = None
            ) -> Tuple[Optional[Callable[[str], str]], str, str]:
    """决定这次走哪个后端：返回 `(调用器, 后端名, 说明)`；调用器为 None 表示没配。"""
    if callable(caller):
        return caller, "injected", ""
    p = params or {}
    profile = str(p.get("profile") or "chat")
    prof = profile_config(profile)
    backend = str(p.get("backend") or prof.get("backend")
                  or os.environ.get("LLM_BACKEND") or "").strip().lower()
    if not backend:
        backend = "mcp" if (prof.get("command") or os.environ.get("LLM_MCP_COMMAND")) else "http"
    if backend == "mcp":
        fn = mcp_caller(p.get("command"), p.get("tool"), p.get("timeout_s"))
        if fn is None:
            return None, "mcp", "mcp 后端未配置：config/llm.json 的 mcp.command+tool，或 LLM_MCP_*"
        return fn, "mcp", ""
    fn = http_caller(p.get("url"), p.get("model"), p.get("token"), p.get("timeout_s"),
                     profile=profile)
    if fn is None:
        return None, "http", (f"端点未配置：config/llm.json 的 profiles.{profile}.url，"
                             "或 LLM_URL / AUDIT_LLM_URL")
    return fn, "http", ""


# ═══════════════════════════════════════════════════════════ 动作
def chat(params: Optional[Dict[str, Any]] = None,
         ctx: Optional[Dict[str, Any]] = None,
         caller: Optional[Callable[[str], str]] = None) -> Dict[str, Any]:
    """`llm/chat`：一次问答。失败一律 `degraded=True`，由调用方决定怎么降级。

    带 `tools` 时走工具调用循环（function call）：模型要调工具就本地执行，结果以
    `role="tool"` 回给模型，最多 `max_tool_rounds` 轮（默认 4），超了截断并标记。
    工具实现由调用方通过 `tool_impls`（名字 → `callable(args) -> 可 JSON 化结果`）注入，
    本层不决定模型能碰什么，只管收发与边界。
    """
    p = params or {}
    prompt = str(p.get("prompt") or p.get("text") or "")
    if not prompt.strip():
        return _fail("prompt 为空", "none")
    try:
        lim = _limits()
        max_prompt, max_out = int(lim.max_prompt_chars), int(lim.max_output_chars)
    except Exception:  # noqa: BLE001 - 限额文件坏了也不能让调用失败
        max_prompt, max_out = 8000, 8000

    prompt_cut = len(prompt) > max_prompt
    prompt = prompt[:max_prompt]
    system = str(p.get("system") or "").strip()
    full = f"{system}\n\n{prompt}" if system else prompt

    tools = p.get("tools") or []
    if isinstance(tools, (list, tuple)) and tools:
        return _chat_with_tools(p, prompt, system, list(tools),
                                p.get("tool_impls") or {}, p.get("tool_caller"),
                                max_out, prompt_cut)

    fn, backend, note = resolve(p, caller)
    if fn is None:
        return _fail(note or "未配置模型端点", backend, prompt_cut)

    try:
        text = fn(full)
    except urllib.error.HTTPError as e:
        return _fail(f"HTTP {e.code}：{_short(e.read())}", backend, prompt_cut)
    except urllib.error.URLError as e:
        return _fail(f"连不上端点：{e.reason}", backend, prompt_cut)
    except Exception as e:  # noqa: BLE001 - 模型调用失败不炸流水线
        return _fail(f"{type(e).__name__}: {e}", backend, prompt_cut)

    text = str(text or "")
    out_cut = len(text) > max_out
    kept = text[:max_out]
    return {
        "ok": bool(kept.strip()),
        "text": kept,
        "chars": len(kept),
        "est_tokens": int((len(full) + len(kept)) * 0.75),
        "backend": backend,
        "model": str(p.get("model") or os.environ.get("LLM_MODEL")
                     or os.environ.get("AUDIT_LLM_MODEL") or ""),
        "truncated": prompt_cut or out_cut,
        "prompt_truncated": prompt_cut,
        "output_truncated": out_cut,
        "degraded": False,
        "reason": "",
    }


def _tool_resolve(p: Dict[str, Any],
                  tool_caller: Optional[Callable[..., Dict[str, Any]]]
                  ) -> Tuple[Optional[Callable[..., Dict[str, Any]]], str, str]:
    """工具模式的收发器：注入优先，其次用配置里的 http 端点。"""
    if callable(tool_caller):
        return tool_caller, "injected", ""
    profile = str(p.get("profile") or "chat")
    prof = profile_config(profile)
    url = str(p.get("url") or prof.get("url") or "").strip()
    if not url:
        return None, "http", (f"工具模式需要端点：config/llm.json 的 profiles.{profile}.url，"
                             "或由调用方传 tool_caller（测试/示例用）")
    model = str(p.get("model") or prof.get("model") or "")
    token = str(p.get("token") or prof.get("token") or "")
    timeout = float(p.get("timeout_s") or _limits().timeout_s)

    def _call(messages, tools) -> Dict[str, Any]:
        body = json.dumps({"model": model, "messages": messages, "tools": tools,
                           "stream": False}, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(_normalize_url(url), data=body, method="POST",
                                     headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", errors="replace"))
        msg = (data.get("choices") or [{}])[0].get("message") or {}
        return {"content": msg.get("content") or "",
                "tool_calls": msg.get("tool_calls") or []}

    return _call, "http", ""


def _chat_with_tools(p: Dict[str, Any], prompt: str, system: str,
                     tools: List[Dict[str, Any]], impls: Dict[str, Any],
                     tool_caller: Optional[Callable[..., Dict[str, Any]]],
                     max_out: int, prompt_cut: bool) -> Dict[str, Any]:
    """有界工具调用循环。四类异常都降级成工具结果回给模型，不炸整轮。"""
    try:
        max_rounds = max(0, int(p.get("max_tool_rounds") or 4))
    except (TypeError, ValueError):
        max_rounds = 4
    fn, backend, note = _tool_resolve(p, tool_caller)
    if fn is None:
        return _fail(note or "工具模式未配置端点", backend, prompt_cut)

    messages: List[Dict[str, Any]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    trace: List[Dict[str, Any]] = []

    for round_no in range(max_rounds + 1):
        try:
            resp = fn(messages, tools) or {}
        except urllib.error.HTTPError as e:
            return _fail(f"HTTP {e.code}：{_short(e.read())}", backend, prompt_cut, trace)
        except urllib.error.URLError as e:
            return _fail(f"连不上端点：{e.reason}", backend, prompt_cut, trace)
        except Exception as e:  # noqa: BLE001
            return _fail(f"{type(e).__name__}: {e}", backend, prompt_cut, trace)

        content = str(resp.get("content") or "")
        calls = resp.get("tool_calls") or []
        if not calls:
            kept = content[:max_out]
            return {"ok": bool(kept.strip()), "text": kept, "chars": len(kept),
                    "est_tokens": int((len(prompt) + len(kept)) * 0.75),
                    "backend": backend, "model": str(p.get("model") or ""),
                    "truncated": prompt_cut or len(content) > max_out,
                    "prompt_truncated": prompt_cut,
                    "output_truncated": len(content) > max_out,
                    "degraded": False, "reason": "",
                    "tool_rounds": round_no, "tool_trace": trace,
                    "tools_offered": [str((t.get("function") or {}).get("name"))
                                      for t in tools]}

        if round_no >= max_rounds:
            kept = content[:max_out]
            return {"ok": bool(kept.strip()), "text": kept, "chars": len(kept),
                    "est_tokens": 0, "backend": backend, "model": str(p.get("model") or ""),
                    "truncated": True, "prompt_truncated": prompt_cut,
                    "output_truncated": False, "degraded": True,
                    "reason": f"超过工具轮次上限 {max_rounds}，停止调用",
                    "tool_rounds": round_no, "tool_trace": trace,
                    "tools_offered": [str((t.get("function") or {}).get("name"))
                                      for t in tools]}

        messages.append({"role": "assistant", "content": content, "tool_calls": calls})
        for idx, call in enumerate(calls):
            call = call if isinstance(call, dict) else {}
            fn_spec = call.get("function") if isinstance(call.get("function"), dict) else {}
            name = str(fn_spec.get("name") or "")
            raw_args = fn_spec.get("arguments")
            call_id = str(call.get("id") or f"call-{round_no}-{idx}")
            args, bad_args = None, False
            if isinstance(raw_args, str):
                try:
                    args = json.loads(raw_args or "{}")
                except ValueError:
                    bad_args = True
            elif isinstance(raw_args, dict):
                args = raw_args
            else:
                args = {}

            if bad_args:
                outcome = {"error": "bad_arguments", "detail": "arguments 不是合法 JSON"}
            elif name not in impls or not callable(impls.get(name)):
                outcome = {"error": "unknown_tool", "name": name,
                           "available": sorted(impls.keys())}
            else:
                try:
                    outcome = impls[name](args or {})
                except Exception as e:  # noqa: BLE001 - 工具自己炸了也只回错误
                    outcome = {"error": f"{type(e).__name__}: {e}"}

            text_out = json.dumps(outcome, ensure_ascii=False, default=str)
            failed = isinstance(outcome, dict) and bool(outcome.get("error"))
            trace.append({"round": round_no, "tool": name, "args": args,
                          "ok": not failed, "result_chars": len(text_out),
                          "error": (outcome.get("error") if isinstance(outcome, dict) else "")})
            messages.append({"role": "tool", "tool_call_id": call_id,
                             "content": text_out[:2000]})

    return _fail("工具循环异常结束", backend, prompt_cut, trace)


def _fail(reason: str, backend: str = "",
          prompt_truncated: bool = False,
          tool_trace: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    return {"ok": False, "text": "", "chars": 0, "est_tokens": 0,
            "backend": backend, "model": "", "truncated": bool(prompt_truncated),
            "prompt_truncated": bool(prompt_truncated), "output_truncated": False,
            "degraded": True, "reason": reason,
            "tool_rounds": 0, "tool_trace": list(tool_trace or [])}


def _short(data: Any, limit: int = 200) -> str:
    try:
        s = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else str(data)
    except Exception:  # noqa: BLE001
        s = ""
    return s[:limit]


def run_action(action: str, params: Optional[Dict[str, Any]] = None,
               ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if action == "chat":
        return chat(params, ctx)
    raise ValueError(f"llm 不支持的动作：{action}")


def register(runtime: Any, capability: str = "llm",
             actions: tuple = LLM_ACTIONS) -> None:
    """把 `llm/chat` 挂到一个 Agent 上。"""
    for action in actions:
        runtime.add_handler(capability, action,
                            (lambda a: (lambda p, c: run_action(a, p, c)))(action))