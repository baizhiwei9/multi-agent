"""用户存储（fixture，内存实现）：登录失败计数与锁定都在这里。"""
from __future__ import annotations

_USERS = {
    "alice": {"id": 1, "hash": "5e884898da28047d"},
    "bob": {"id": 2, "hash": "6f2a9a1c33d0c9f4"},
}
_FAILS = {}


def find_user(username):
    return _USERS.get(username)


def bump_fail(username):
    """记一次失败：超过 5 次就锁定。"""
    _FAILS[username] = _FAILS.get(username, 0) + 1
    return _FAILS[username]


def locked(username):
    return _FAILS.get(username, 0) >= 5