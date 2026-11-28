#!/usr/bin/env bash
# 一键启动侦探团：Hub(127.0.0.1:8787) + worker，并打印贴进 DSH 的 MCP 配置。
#
#   bash examples/detective_panel/start_panel.sh
#   DETECTIVES=8 bash examples/detective_panel/start_panel.sh     # 起 8 个并发 worker
#   LLM_TOKEN=sk-xxx bash examples/detective_panel/start_panel.sh # 真跑（DeepSeek）
#
# 不设 LLM_TOKEN 也能起，但发言会是「模拟·未配模型」，只验证链路。
set -euo pipefail
cd "$(dirname "$0")/../.."          # 回到仓库根

export PANEL_TOKEN="${PANEL_TOKEN:-$(python -c 'import secrets;print(secrets.token_hex(16))')}"
export LLM_TOKEN="${LLM_TOKEN:-}"

INSTANCES="${DETECTIVES:-2}"

echo "── 模型端点自检 ─────────────────────────────"
python - <<'PY'
import os
from multi_agent.integrations.llm import status

# 自检必须认环境变量：llm.chat() 的解析顺序是
# profile(url/model) → LLM_URL/LLM_MODEL（AUDIT_* 亦可）→ LLM_TOKEN。
# 以前只看 config 里的 profiles，明明设了环境变量也打印“未配置”，会误导排查。
s = status()
env_url = os.environ.get("LLM_URL") or os.environ.get("AUDIT_LLM_URL") or ""
env_model = os.environ.get("LLM_MODEL") or os.environ.get("AUDIT_LLM_MODEL") or ""
env_token = os.environ.get("LLM_TOKEN") or os.environ.get("AUDIT_LLM_TOKEN") or ""
for name in ("detective_fast", "detective_deep", "judge_pro"):
    p = (s.get("profiles") or {}).get(name) or {}
    url = p.get("url") or env_url
    model = p.get("model") or env_model
    token = p.get("token") or env_token
    ok = bool(url and model)
    src = "config" if p.get("url") else ("env" if env_url else "-")
    print(f"  {name:<15} {'就绪' if ok else '未配置'}  model={model or '-'} "
          f"key={'有' if token else '无'}  来源={src}")
    if not ok:
        print("                  → 未配置时发言是「模拟·未配模型」，只验证链路")
PY
echo "─────────────────────────────────────────────"

exec python examples/detective_panel/serve_panel.py --instances "$INSTANCES"