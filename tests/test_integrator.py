"""integrator 端到端测试：全部在临时 git 仓库里跑，不碰真实仓库。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from multi_agent.integrations import integrator  # noqa: E402

ENV = {**os.environ,
       "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def _git(repo: Path, *args: str, input_text: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(repo), check=True,
                          capture_output=True, text=True, input=input_text, env=ENV)


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "game.py").write_text("def score():\n    return 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    return repo


def make_patch(repo: Path, changes: dict, base_ref: str | None = None) -> str:
    """把改动写进工作区、取 diff、再复原，返回补丁正文。"""
    for name, content in changes.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    args = ["diff", "--cached"] + ([base_ref] if base_ref else [])
    patch = _git(repo, *args).stdout
    _git(repo, "reset", "--hard")
    return patch


def _stage(repo: Path, patch: str, **extra):
    params = {"repo_root": str(repo), "base": "main", "patch_text": patch,
              "branch": "integration/t1", "run_id": "t1"}
    params.update(extra)
    return integrator.stage(params)


def test_stage_creates_branch_and_commit(tmp_path):
    repo = make_repo(tmp_path)
    patch = make_patch(repo, {"game.py": "def score():\n    return 2\n", "new.py": "x = 1\n"})
    res = _stage(repo, patch)

    assert res["ok"] is True, res
    assert res["branch"] == "integration/t1"
    assert res["base_commit"] != res["head"]
    assert set(res["files"]) == {"game.py", "new.py"}
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "integration/t1"
    assert not _git(repo, "status", "--porcelain").stdout.strip()  # 已提交，工作树干净


def test_stage_rejects_dirty_tree(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "game.py").write_text("def score():\n    return 7\n", encoding="utf-8")
    res = _stage(repo, "diff --git a/x b/x\n")
    assert res["ok"] is False
    assert "脏树" in res["error"]


def test_stage_conflict_rolls_back(tmp_path):
    repo = make_repo(tmp_path)
    patch = make_patch(repo, {"game.py": "def score():\n    return 2\n"})
    # 先把 main 推到补丁上下文对不上的位置
    (repo / "game.py").write_text("def score():\n    return 99\n\n# moved on\n", encoding="utf-8")
    _git(repo, "commit", "-am", "main moved")

    res = _stage(repo, patch)
    assert res["ok"] is False
    assert "game.py" in res["conflicts"], res
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "main"
    assert "integration/t1" not in _git(repo, "branch", "--list").stdout
    assert not _git(repo, "status", "--porcelain").stdout.strip()


def test_verify_records_results(tmp_path):
    repo = make_repo(tmp_path)
    patch = make_patch(repo, {"game.py": "def score():\n    return 2\n"})
    _stage(repo, patch)

    good = integrator.verify({"repo_root": str(repo), "branch": "integration/t1",
                              "commands": ["python -c 'print(1)'"]})
    assert good["ok"] is True and good["skipped"] is False and good["results"][0]["code"] == 0

    bad = integrator.verify({"repo_root": str(repo), "branch": "integration/t1",
                             "commands": ["python -c 'import sys; sys.exit(3)'"]})
    assert bad["ok"] is False and bad["results"][0]["code"] == 3

    empty = integrator.verify({"repo_root": str(repo), "branch": "integration/t1"})
    assert empty["ok"] is True and empty["skipped"] is True


def test_merge_requires_human_approval(tmp_path):
    repo = make_repo(tmp_path)
    _stage(repo, make_patch(repo, {"game.py": "def score():\n    return 2\n"}))
    verify = integrator.verify({"repo_root": str(repo), "branch": "integration/t1",
                                "commands": ["python -c 'print(1)'"]})

    res = integrator.merge({"repo_root": str(repo), "base": "main",
                            "branch": "integration/t1", "verify": verify})
    assert res["ok"] is False
    assert any("人工批准" in r for r in res["reasons"])
    # 未合并：main 还是原值
    assert "return 1" in _git(repo, "show", "main:game.py").stdout


def test_merge_rejects_unrun_verify(tmp_path):
    repo = make_repo(tmp_path)
    _stage(repo, make_patch(repo, {"game.py": "def score():\n    return 2\n"}))
    verify = integrator.verify({"repo_root": str(repo), "branch": "integration/t1"})  # skipped

    res = integrator.merge({"repo_root": str(repo), "base": "main", "branch": "integration/t1",
                            "approve": True, "approved_by": "owner", "verify": verify})
    assert res["ok"] is False
    assert any("未真正执行" in r for r in res["reasons"])


def test_merge_squashes_and_writes_ledger(tmp_path):
    repo = make_repo(tmp_path)
    patch = make_patch(repo, {"game.py": "def score():\n    return 2\n", "new.py": "x = 1\n"})
    staged = _stage(repo, patch)
    verify = integrator.verify({"repo_root": str(repo), "branch": "integration/t1",
                                "commands": ["python -c 'print(1)'"]})

    res = integrator.merge({"repo_root": str(repo), "base": "main", "branch": "integration/t1",
                            "approve": True, "approved_by": "owner",
                            "verify": verify, "gate": {"verdict": "pass"},
                            "patch_sha256": staged["patch_sha256"]})
    assert res["ok"] is True, res
    assert res["rebased"] is False
    # squash 后 main 只有一个新提交，内容已合入
    assert "return 2" in _git(repo, "show", "main:game.py").stdout
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "main"
    count = int(_git(repo, "rev-list", "--count", f"{res['base_before']}..main").stdout.strip())
    assert count == 1

    ledger = Path(res["ledger"])
    assert ledger.is_file()
    entry = json.loads(ledger.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert entry["action"] == "merge" and entry["approved_by"] == "owner"
    assert entry["base_before"] == res["base_before"] and entry["merge_commit"] == res["merge_commit"]


def test_merge_blocks_on_gate_block(tmp_path):
    repo = make_repo(tmp_path)
    _stage(repo, make_patch(repo, {"game.py": "def score():\n    return 2\n"}))
    verify = integrator.verify({"repo_root": str(repo), "branch": "integration/t1",
                                "commands": ["python -c 'print(1)'"]})
    res = integrator.merge({"repo_root": str(repo), "base": "main", "branch": "integration/t1",
                            "approve": True, "approved_by": "owner", "verify": verify,
                            "gate": {"verdict": "block"}})
    assert res["ok"] is False
    assert any("block" in r for r in res["reasons"])


def test_merge_rebases_when_base_moved(tmp_path):
    repo = make_repo(tmp_path)
    _stage(repo, make_patch(repo, {"game.py": "def score():\n    return 2\n"}))
    # base 前进一个提交
    _git(repo, "checkout", "main")
    (repo / "other.py").write_text("y = 2\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "main moved")

    verify = integrator.verify({"repo_root": str(repo), "branch": "integration/t1",
                                "commands": ["python -c 'print(1)'"]})
    res = integrator.merge({"repo_root": str(repo), "base": "main", "branch": "integration/t1",
                            "approve": True, "approved_by": "owner", "verify": verify})
    assert res["ok"] is True, res
    assert res["rebased"] is True
    assert "return 2" in _git(repo, "show", "main:game.py").stdout
    assert "y = 2" in _git(repo, "show", "main:other.py").stdout


def test_revert_restores_previous_state(tmp_path):
    repo = make_repo(tmp_path)
    _stage(repo, make_patch(repo, {"game.py": "def score():\n    return 2\n"}))
    verify = integrator.verify({"repo_root": str(repo), "branch": "integration/t1",
                                "commands": ["python -c 'print(1)'"]})
    merged = integrator.merge({"repo_root": str(repo), "base": "main", "branch": "integration/t1",
                               "approve": True, "approved_by": "owner", "verify": verify})
    assert merged["ok"] is True

    back = integrator.revert({"repo_root": str(repo), "commit": merged["merge_commit"]})
    assert back["ok"] is True, back
    assert "return 1" in (repo / "game.py").read_text(encoding="utf-8")


def test_open_pr_dry_run_touches_nothing(tmp_path):
    repo = make_repo(tmp_path)
    _stage(repo, make_patch(repo, {"game.py": "def score():\n    return 2\n"}))
    res = integrator.open_pr({"repo_root": str(repo), "branch": "integration/t1",
                              "base": "main", "title": "t"})
    assert res["ok"] is True and res["dry_run"] is True
    assert any("git push" in c for c in res["commands"])
    assert not _git(repo, "remote").stdout.strip()  # 没有远端，也没尝试推送


def test_non_git_repo_rejected(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    res = integrator.stage({"repo_root": str(plain), "patch_text": "x"})
    assert res["ok"] is False
    assert "不是 git 仓库" in res["error"]


def test_register_exposes_all_actions():
    class FakeRuntime:
        def __init__(self):
            self.handlers = []

        def add_handler(self, capability, action, handler):
            self.handlers.append((capability, action, handler))

    runtime = FakeRuntime()
    integrator.register(runtime)
    caps = [(c, a) for c, a, _ in runtime.handlers]
    assert caps == [("integrator", a) for a in integrator.ACTIONS]
    out = runtime.handlers[0][2]({"repo_root": "/nonexistent"}, {})
    assert out["ok"] is False