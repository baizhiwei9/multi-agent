"""极简路由注册器（示例用）。

真实项目可以直接用 FastAPI / Flask / Express / Go mux；审计扫描器认的是
`@route("POST", "/path")`、`@app.post("/path")`、`register_route("GET", "/path")`
这几类常见写法。这里保留一个最小实现，方便离线跑通整条链路。
"""
from __future__ import annotations

ROUTES = []


def route(method, path, auth=False):
    def deco(fn):
        ROUTES.append({"method": method.upper(), "path": path,
                       "auth": bool(auth), "handler": fn.__name__})
        return fn
    return deco


def tx(fn):
    """标记这个 handler 走事务（真实项目里是 with tx(): / @Transactional）。"""
    fn._tx = True
    return fn