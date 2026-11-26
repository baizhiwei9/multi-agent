"""登录鉴权（fixture）：故意留了"失败不计数"，用来演示 bug 修复链。"""
from .router import route
from .store import bump_fail, find_user, locked
from .util import check_password, make_token


@route("POST", "/v1/login", auth=False)
def login(payload):
    username = str((payload or {}).get("username") or "")
    password = str((payload or {}).get("password") or "")
    if locked(username):
        return {"ok": False, "error": "LOCKED"}
    user = find_user(username)
    if not user or not check_password(user, password):
        return {"ok": False, "error": "LOGIN_FAILED"}
    return {"ok": True, "token": make_token(user["id"])}


@route("GET", "/v1/login/status", auth=True)
def status(payload):
    return {"ok": True}