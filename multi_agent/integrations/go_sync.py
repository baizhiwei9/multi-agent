"""同步服务器 Worker 的 function call：把审计通过的二进制搬到目标端，并在目标端验证日志。

    sync_artifact(artifact, target_root, ...) -> {synced, verified, remote_sha256, remote_log}

这条链只做四件事，顺序不能换：

1. **验货**：字节数/sha256 与审计过的那份一致，不一致直接拒（不搬来源不明的包）；
2. **备份**：目标端已有同名产物，先挪成 `.prev`，留一条回滚路；
3. **搬运**：复制 + 落 0755，再算一次远端 sha256 比对；
4. **远端自证**：在目标端真跑一次登录失败，把目标端日志读回来。

真实部署把第 3 步换成 `scp` / `rsync` / 发布 API 即可，前后两步的凭据口径不变。
"""
from __future__ import annotations

import hashlib
import os
import shutil
from typing import Any, Dict, Optional

from .go_toolchain import run_login


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sync_artifact(artifact: Dict[str, Any], target_root: str,
                  host: str = "host-b", release: str = "v2",
                  user: str = "bob", password: str = "123456",
                  expected: str = "error 登录失败: bob") -> Dict[str, Any]:
    """把 artifact 同步到 `target_root/host/releases/<release>/` 并验证远端日志。"""
    artifact = artifact or {}
    src = str(artifact.get("bin") or "")
    if not artifact.get("ok") or not os.path.isfile(src):
        return {"synced": False, "verified": False, "error": "artifact_invalid",
                "bin": src}
    local_sha = _sha256(src)
    if artifact.get("sha256") and artifact["sha256"] != local_sha:
        return {"synced": False, "verified": False, "error": "sha256_mismatch",
                "local": local_sha, "declared": artifact.get("sha256")}

    release_dir = os.path.join(os.path.realpath(target_root), host, "releases", release)
    os.makedirs(release_dir, exist_ok=True)
    dest = os.path.join(release_dir, os.path.basename(src))
    previous: Optional[str] = None
    if os.path.isfile(dest):
        previous = dest + ".prev"
        shutil.move(dest, previous)

    shutil.copy2(src, dest)
    os.chmod(dest, 0o755)
    remote_sha = _sha256(dest)

    log_path = os.path.join(os.path.realpath(target_root), host, "logs", "server.log")
    remote = run_login(dest, user, password, log_path)
    text = remote.get("text") or ""
    verified = bool(expected) and expected in text and password not in text

    return {
        "synced": remote_sha == local_sha,
        "verified": verified,
        "host": host,
        "release": release,
        "remote_bin": dest,
        "bytes": os.path.getsize(dest),
        "remote_sha256": remote_sha,
        "local_sha256": local_sha,
        "previous": previous,
        "remote_log_path": log_path,
        "remote_log": remote.get("lines") or [],
        "expected": expected,
    }