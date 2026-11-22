"""执行端白名单判定：按真实路径判断目录归属，而不是字符串前缀。

为什么单独一个模块
------------------
`bootstrap` 的直连执行端和 `signal` 的执行端共用同一套判定。如果各写一份，
就会出现"这边补了、那边还漏着"的情况（PR #4 里就是两处都用了 `startswith`）。

`script.startswith(p)` 有两个经典绕过，实测都能在"配了白名单"的情况下跑目录外脚本：

    allowed = "/srv/jobs"
    /srv/jobs-evil/probe.py          # 同前缀兄弟目录
    /srv/jobs/../evil/write.py       # .. 穿越

正确做法：两边都 `resolve()`（展开 `..` 与软链接），再用 `target == base` 或
`base in target.parents` 判定。判定只看路径，不看调用方说了什么。
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Sequence, Tuple

WILDCARD = "*"


def normalize(allowed_prefixes: Optional[Sequence[str]]) -> Tuple[bool, List[Path]]:
    """把配置归一成 (是否全放开, 允许目录列表)。

    规则：
    - 没配 / 空列表 → (False, [])，调用方按 fail-closed 拒绝；
    - 列表里出现 `"*"` **任何位置** → (True, [])，显式全放开（含 ["*", "/dir"] 这种混写）；
    - 其余项按 `expanduser().resolve()` 归一，避免相对路径随 cwd 漂移。
    """
    items = [str(p).strip() for p in (allowed_prefixes or []) if str(p).strip()]
    if WILDCARD in items:
        return True, []
    return False, [Path(i).expanduser().resolve() for i in items]


def check(script: str, allowed_prefixes: Optional[Sequence[str]]) -> Tuple[bool, Optional[str]]:
    """判断某个 script 是否允许执行：返回 (放行?, 拒绝原因)。

    拒绝原因是给对端看的，要能直接照着改（缺什么、允许什么），不是"denied"两个字。
    """
    allow_all, bases = normalize(allowed_prefixes)
    if allow_all:
        return True, None
    if not bases:
        return False, ("执行端未配置白名单（fail-closed）：请传 "
                       "allowed_prefixes=['/your/dir'] 或 ['*'] 显式放开")
    if not script:
        return False, "缺少 script"

    target = Path(script).expanduser().resolve()
    for base in bases:
        if target == base or base in target.parents:
            return True, None
    return False, (f"脚本不在允许目录：{script}（解析为 {target}，"
                   f"允许 {[str(b) for b in bases]}）")