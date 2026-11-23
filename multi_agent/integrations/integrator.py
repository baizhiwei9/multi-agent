"""集成官（integrator）：把开发产出的补丁合到主干。

为什么单独一个角色
-------------------
`dev` 只负责在工作副本改代码、产出 patch（`projects/game_server.py` 的 dev 步骤
就是这样：直接写工作副本 + 生成 `patches/<ticket>/<ws>.patch`），但“这个补丁
能不能进主干”以前没人负责——审计只管判、发布包只管打包，最后由人手 `git apply`。
integrator 把这一步收成可审计的动作，并且**只做 git 的 apply / rebase / merge，
不改任何业务代码**：冲突一律打回 dev，避免它变成第二个开发者、让审计失去意义。

动作
----
· `stage`   —— 从 base 拉集成分支，应用补丁并提交；冲突即失败并回冲突文件清单
· `verify`  —— 在集成分支上跑给定命令（测试 / 编译 / 审计脚本），收返回码
· `open_pr` —— 推分支并开 PR（gh）；默认 `dry_run`，不动远端
· `merge`   —— 四条件同时满足才合：人工批准 + verify 真跑过且通过 + 风控门未 block
                + 分支不落后 base（落后则先 rebase，冲突打回）
· `revert`  —— 回滚指定 merge commit

红线
----
- 工作树脏（有未提交改动）默认拒绝 stage / merge，避免把别人没提交的东西卷进来；
- 不 force push，不改业务文件，不自动解决冲突；
- 每次 merge 写台账：`<git-dir>/integrator-ledger.jsonl`，含 base 前后 commit、
  分支、批准人、verify 结论、gate 结论；
- git 身份逐次用环境变量注入，不动 `git config`。
"""
from __future__ import annotations

import functools
import hashlib
import json
import os
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

#: 本模块提供的动作（注册到 capability=integrator）
ACTIONS: tuple = ("stage", "verify", "open_pr", "merge", "revert")

#: 缺省提交身份（不写 git config，逐次注入环境变量）
DEFAULT_AUTHOR = "DuMate"
DEFAULT_EMAIL = "dumate@local"

MAX_PATCH_BYTES = 2 * 1024 * 1024
CMD_TIMEOUT = 180
_CONFLICT_CODES = ("UU", "AA", "DU", "UD", "AU", "UA", "DD")


# ---------- 基础设施 ----------
def _identity_env(params: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    """git 写操作要作者身份；沙箱里通常没有全局 user.email，逐次注入。"""
    p = params or {}
    name = str(p.get("author_name") or DEFAULT_AUTHOR)
    email = str(p.get("author_email") or DEFAULT_EMAIL)
    env = dict(os.environ)
    env.update({"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
                "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email})
    return env


def _run(cmd: Sequence[str], cwd: Path, timeout: int = CMD_TIMEOUT,
         input_text: Optional[str] = None,
         env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """跑一条命令，永不抛异常，统一返回 {code, out, err}。"""
    try:
        proc = subprocess.run(list(cmd), cwd=str(cwd), input=input_text,
                              capture_output=True, text=True, timeout=timeout,
                              env=env or dict(os.environ))
        return {"code": proc.returncode, "out": proc.stdout or "", "err": proc.stderr or ""}
    except subprocess.TimeoutExpired:
        return {"code": 124, "out": "", "err": f"超时 {timeout}s：{' '.join(cmd)}"}
    except FileNotFoundError as exc:
        return {"code": 127, "out": "", "err": f"命令不存在：{exc}"}


def _git(repo: Path, *args: str, input_text: Optional[str] = None,
         timeout: int = CMD_TIMEOUT,
         env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    return _run(["git", *args], repo, timeout=timeout, input_text=input_text,
                env=env or _identity_env())


def _safe(fn):
    """把配置类错误（缺参数、非 git 仓库）收成 {ok: false}，不让异常穿透到 Hub。"""
    @functools.wraps(fn)
    def wrapper(params: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        try:
            return fn(params, ctx)
        except (ValueError, OSError) as exc:
            return {"ok": False, "error": str(exc)}
    return wrapper


def _repo(params: Dict[str, Any]) -> Path:
    raw = str(params.get("repo_root") or "").strip()
    if not raw:
        raise ValueError("缺少 repo_root")
    repo = Path(raw).expanduser().resolve()
    if not repo.is_dir():
        raise ValueError(f"repo_root 不是目录：{repo}")
    if _git(repo, "rev-parse", "--git-dir")["code"] != 0:
        raise ValueError(f"不是 git 仓库：{repo}")
    return repo


def _branch_name(name: Any) -> str:
    text = str(name or "").strip()
    bad = (not text or text.startswith("-") or ".." in text or " " in text
           or "~" in text or "^" in text or ":" in text or "\\" in text or "@{" in text)
    if bad:
        raise ValueError(f"非法分支名：{name!r}")
    return text


def _clean(repo: Path) -> Dict[str, Any]:
    res = _git(repo, "status", "--porcelain")
    return {"clean": res["code"] == 0 and not res["out"].strip(),
            "lines": [l for l in res["out"].splitlines() if l.strip()][:20]}


def _head(repo: Path, ref: str = "HEAD") -> str:
    res = _git(repo, "rev-parse", ref)
    return res["out"].strip() if res["code"] == 0 else ""


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _conflict_files(repo: Path, stderr: str = "") -> List[str]:
    """冲突文件：优先读 git status，其次从 apply/rebase 的 stderr 里抠。"""
    files: List[str] = []
    res = _git(repo, "status", "--porcelain")
    for line in res["out"].splitlines():
        if len(line) > 3 and line[:2] in _CONFLICT_CODES:
            path = line[3:].strip()
            if path and path not in files:
                files.append(path)
    for line in (stderr or "").splitlines():
        marker = "error: patch failed: "
        if marker in line:
            path = line.split(marker, 1)[1].split(":", 1)[0].strip()
            if path and path not in files:
                files.append(path)
    return files


def _default_branch(params: Dict[str, Any]) -> str:
    seed = params.get("run_id") or params.get("ticket") or time.strftime("%Y%m%d-%H%M%S")
    safe = "".join(c if (c.isalnum() or c in "._-") else "-" for c in str(seed))[:60]
    return f"integration/{safe}"


def _ledger_path(repo: Path) -> Path:
    res = _git(repo, "rev-parse", "--absolute-git-dir")
    git_dir = Path(res["out"].strip()) if res["code"] == 0 else (repo / ".git")
    return git_dir / "integrator-ledger.jsonl"


def _append_ledger(repo: Path, entry: Dict[str, Any]) -> str:
    path = _ledger_path(repo)
    record = {"ts": time.time(), **entry}
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return str(path)
    except OSError:
        return ""


def _tail(res: Dict[str, Any], n: int = 800) -> str:
    return ((res.get("err") or "") + (res.get("out") or ""))[-n:]


# ---------- 动作 ----------
@_safe
def stage(params: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """从 base 拉集成分支，应用补丁并提交。

    params: repo_root / base / branch / patch | patch_text / message / allow_dirty
    """
    repo = _repo(params)
    base = _branch_name(params.get("base") or "main")
    message = str(params.get("message") or "integration: apply patch").strip()

    state = _clean(repo)
    if not state["clean"] and not params.get("allow_dirty"):
        return {"ok": False, "error": "工作树有未提交改动，拒绝在脏树上打补丁",
                "dirty": state["lines"]}

    text = params.get("patch_text")
    if text is None:
        raw_path = params.get("patch")
        if not raw_path:
            return {"ok": False, "error": "缺少 patch（文件路径）或 patch_text"}
        patch_path = Path(str(raw_path)).expanduser()
        if not patch_path.is_file():
            return {"ok": False, "error": f"补丁不存在：{patch_path}"}
        if patch_path.stat().st_size > MAX_PATCH_BYTES:
            return {"ok": False, "error": f"补丁过大（>{MAX_PATCH_BYTES} 字节）"}
        text = patch_path.read_text(encoding="utf-8", errors="replace")
    if not str(text).strip():
        return {"ok": False, "error": "补丁为空"}

    base_sha = _head(repo, base)
    if not base_sha:
        return {"ok": False, "error": f"base 分支不存在：{base}"}
    origin_ref = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")["out"].strip() or base
    branch = _branch_name(params.get("branch") or _default_branch(params))
    if _git(repo, "rev-parse", "--verify", f"refs/heads/{branch}")["code"] == 0:
        return {"ok": False, "error": f"分支已存在：{branch}"}

    created = _git(repo, "checkout", "-b", branch, base)
    if created["code"] != 0:
        return {"ok": False, "error": "创建集成分支失败", "detail": _tail(created)}

    applied = _git(repo, "apply", "-", input_text=str(text))
    detail = (applied["err"] or "").strip()
    if applied["code"] != 0:
        # 补丁带 index 行时 --3way 能基于 blob 三方合并，成功率更高
        retry = _git(repo, "apply", "--3way", "-", input_text=str(text))
        applied = retry
        detail = (retry["err"] or detail).strip()

    if applied["code"] != 0:
        conflicts = _conflict_files(repo, detail)
        _git(repo, "reset", "--hard")
        _git(repo, "checkout", origin_ref)
        _git(repo, "branch", "-D", branch)
        return {"ok": False, "error": "补丁无法应用（冲突或基线不符），已回滚，退回 dev 处理",
                "conflicts": conflicts, "detail": detail[-1500:],
                "base": base, "base_commit": base_sha}

    changed = [l[3:].strip() for l in _git(repo, "status", "--porcelain")["out"].splitlines()
               if l.strip()]
    if not changed:
        _git(repo, "checkout", origin_ref)
        _git(repo, "branch", "-D", branch)
        return {"ok": False, "error": "补丁应用后没有产生任何变更"}

    _git(repo, "add", "-A")
    commit = _git(repo, "commit", "-m", message, env=_identity_env(params))
    if commit["code"] != 0:
        _git(repo, "reset", "--hard", base_sha)
        _git(repo, "checkout", origin_ref)
        _git(repo, "branch", "-D", branch)
        return {"ok": False, "error": "集成分支提交失败", "detail": _tail(commit)}

    return {"ok": True, "branch": branch, "base": base, "base_commit": base_sha,
            "head": _head(repo), "files": changed[:50], "file_count": len(changed),
            "patch_sha256": _sha256(str(text))}


@_safe
def verify(params: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """在集成分支上跑命令，收返回码与输出尾部。

    params: repo_root / branch / commands(list) / timeout_s
    没有 commands 视为“未验证”（skipped=True），merge 会拒绝。
    """
    repo = _repo(params)
    branch = params.get("branch")
    if branch:
        branch = _branch_name(branch)
        state = _clean(repo)
        if not state["clean"]:
            return {"ok": False, "error": "工作树有未提交改动，拒绝切换分支",
                    "dirty": state["lines"]}
        checkout = _git(repo, "checkout", branch)
        if checkout["code"] != 0:
            return {"ok": False, "error": f"切换分支失败：{branch}", "detail": _tail(checkout)}

    raw = params.get("commands") or []
    if isinstance(raw, str):
        raw = [raw]
    timeout = int(params.get("timeout_s") or CMD_TIMEOUT)
    results: List[Dict[str, Any]] = []
    for item in raw:
        cmd = str(item).strip()
        if not cmd:
            continue
        res = _run(shlex.split(cmd), repo, timeout=timeout)
        results.append({"cmd": cmd, "code": res["code"], "ok": res["code"] == 0,
                        "tail": (res["out"] + res["err"])[-600:]})
    ok = all(r["ok"] for r in results) if results else True
    current = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")["out"].strip()
    return {"ok": ok, "branch": branch or current, "head": _head(repo),
            "results": results, "skipped": not results}


@_safe
def open_pr(params: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """推分支并开 PR。默认 dry_run=True，只回命令不动远端。"""
    repo = _repo(params)
    branch = _branch_name(params.get("branch") or _default_branch(params))
    base = _branch_name(params.get("base") or "main")
    title = str(params.get("title") or f"integration: {branch}")
    body = str(params.get("body") or "")
    planned = [f"git push -u origin {branch}",
               f"gh pr create --base {base} --head {branch} --title {title!r}"]
    if params.get("dry_run", True):
        return {"ok": True, "dry_run": True, "commands": planned}

    push = _run(["git", "push", "-u", "origin", branch], repo, timeout=300)
    if push["code"] != 0:
        return {"ok": False, "error": "push 失败（不做 force）", "detail": _tail(push)}
    create = _run(["gh", "pr", "create", "--base", base, "--head", branch,
                   "--title", title, "--body", body], repo, timeout=180)
    if create["code"] != 0:
        return {"ok": False, "error": "PR 创建失败", "detail": _tail(create), "pushed": True}
    url = (create["out"].strip().splitlines() or [""])[-1]
    return {"ok": True, "pushed": True, "url": url}


@_safe
def merge(params: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """四条件同时满足才合：人工批准 + verify 真跑过且通过 + gate 未 block + 不落后 base。

    params: repo_root / base / branch / approved_by / approve / verify / gate /
            message / allow_rebase
    """
    repo = _repo(params)
    base = _branch_name(params.get("base") or "main")
    branch = _branch_name(params.get("branch"))
    approved_by = str(params.get("approved_by") or "").strip()
    verdict = params.get("verify") or {}
    gate = params.get("gate") or {}

    reasons: List[str] = []
    if params.get("approve") is not True:
        reasons.append("缺少人工批准（approve 必须为 true）")
    if not approved_by:
        reasons.append("缺少批准人 approved_by")
    if not isinstance(verdict, dict) or verdict.get("ok") is not True:
        reasons.append("verify 未通过或未提供")
    elif verdict.get("skipped"):
        reasons.append("verify 未真正执行（commands 为空）")
    if isinstance(gate, dict) and str(gate.get("verdict") or "").lower() == "block":
        reasons.append("风控门 verdict=block")
    if reasons:
        return {"ok": False, "error": "合并条件未满足", "reasons": reasons}

    state = _clean(repo)
    if not state["clean"]:
        return {"ok": False, "error": "工作树有未提交改动，拒绝合并", "dirty": state["lines"]}

    behind = _git(repo, "rev-list", "--count", f"{branch}..{base}")
    if behind["code"] != 0:
        return {"ok": False, "error": f"分支不存在：{branch}"}
    need_rebase = int(behind["out"].strip() or 0) > 0
    rebased = False
    if need_rebase:
        if not params.get("allow_rebase", True):
            return {"ok": False, "error": "分支落后 base，需先 rebase",
                    "need_rebase": True, "base": base, "branch": branch}
        co = _git(repo, "checkout", branch)
        if co["code"] != 0:
            return {"ok": False, "error": f"切换分支失败：{branch}"}
        rb = _git(repo, "rebase", base, env=_identity_env(params))
        if rb["code"] != 0:
            conflicts = _conflict_files(repo, rb["err"])
            _git(repo, "rebase", "--abort")
            _git(repo, "checkout", base)
            return {"ok": False, "error": "分支落后 base 且 rebase 冲突，退回 dev 处理",
                    "conflicts": conflicts, "detail": _tail(rb)}
        rebased = True
        _git(repo, "checkout", base)

    checkout_base = _git(repo, "checkout", base)
    if checkout_base["code"] != 0:
        return {"ok": False, "error": f"切换 base 失败：{base}", "detail": _tail(checkout_base)}
    base_before = _head(repo, base)
    if not base_before:
        return {"ok": False, "error": f"base 分支不存在：{base}"}
    squash = _git(repo, "merge", "--squash", branch)
    if squash["code"] != 0:
        conflicts = _conflict_files(repo, squash["err"])
        _git(repo, "reset", "--hard", base_before)
        return {"ok": False, "error": "squash 合并冲突", "conflicts": conflicts,
                "detail": _tail(squash)}

    message = str(params.get("message") or f"integration: merge {branch}").strip()
    commit = _git(repo, "commit", "-m", message, env=_identity_env(params))
    if commit["code"] != 0:
        _git(repo, "reset", "--hard", base_before)
        return {"ok": False, "error": "合并提交失败", "detail": _tail(commit),
                "squash_out": _tail(squash, 400)}

    merge_commit = _head(repo)
    ledger = _append_ledger(repo, {
        "action": "merge", "repo": str(repo), "base": base, "branch": branch,
        "base_before": base_before, "merge_commit": merge_commit,
        "approved_by": approved_by, "verify_ok": True,
        "gate_verdict": (gate or {}).get("verdict"), "rebased": rebased,
        "patch_sha256": params.get("patch_sha256"),
    })
    return {"ok": True, "merge_commit": merge_commit, "base": base, "branch": branch,
            "base_before": base_before, "rebased": rebased, "approved_by": approved_by,
            "ledger": ledger}


@_safe
def revert(params: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """回滚指定 merge commit。"""
    repo = _repo(params)
    commit_ref = str(params.get("commit") or "").strip()
    if not commit_ref:
        return {"ok": False, "error": "缺少 commit"}
    state = _clean(repo)
    if not state["clean"]:
        return {"ok": False, "error": "工作树有未提交改动，拒绝回滚", "dirty": state["lines"]}
    res = _git(repo, "revert", "--no-edit", commit_ref, env=_identity_env(params))
    if res["code"] != 0:
        _git(repo, "revert", "--abort")
        return {"ok": False, "error": "回滚失败（可能有冲突）", "detail": _tail(res)}
    revert_commit = _head(repo)
    _append_ledger(repo, {"action": "revert", "repo": str(repo), "reverted": commit_ref,
                          "revert_commit": revert_commit})
    return {"ok": True, "reverted": commit_ref, "revert_commit": revert_commit}


def run_action(action: str, params: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None
               ) -> Dict[str, Any]:
    """动作执行体（单测可直调）：stage / verify / open_pr / merge / revert。"""
    table = {"stage": stage, "verify": verify, "open_pr": open_pr,
             "merge": merge, "revert": revert}
    fn = table.get(action)
    if fn is None:
        raise ValueError(f"integrator 不支持的动作：{action}")
    return fn(params, ctx)


def register(runtime, capability: str = "integrator",
             actions: Sequence[str] = ACTIONS) -> None:
    """把一个 Agent 注册成集成官。

    ```python
    from multi_agent.integrations.integrator import register
    register(runtime)
    ```

    它需要目标仓库的本地写权限（git 操作），因此不要和 `dev` 合在一个 Agent 上：
    dev 不应该有主干写权限，integrator 也不应该改业务代码。
    """
    for action in actions:
        runtime.add_handler(capability, action,
                            (lambda a: (lambda p, c: run_action(a, p, c)))(action))