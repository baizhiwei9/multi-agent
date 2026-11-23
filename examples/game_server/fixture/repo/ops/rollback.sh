#!/usr/bin/env bash
# 回滚脚本（示例）：把服务回退到上一个镜像，并反向执行 004 的列变更。
set -euo pipefail
IMAGE="${1:-game-server:previous}"
echo "rollback to ${IMAGE}"
echo "SQL: ALTER TABLE player ADD COLUMN legacy_score INTEGER DEFAULT 0;"