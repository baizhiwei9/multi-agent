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
from multi_agent.integrations.llm import status
s = status()
for name in ("detective_fast", "detective_deep", "judge_pro"):
    p = (s.get("profiles") or {}).get(name)
    if not p:
        print(f"  {name:<15} 未配置（发言将走模拟）")
        continue
    ok = bool(p.get("url") and p.get("model"))
    print(f"  {name:<15} {'就绪' if ok else '未配置'}  model={p.get('model') or '-'} "
          f"key={p.get('token_source') or 'none'}")
PY
echo "─────────────────────────────────────────────"

exec python examples/detective_panel/serve_panel.py --instances "$INSTANCES"