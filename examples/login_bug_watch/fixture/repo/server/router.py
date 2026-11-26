"""路由装饰器（fixture）：只为了让审计能扫出 method/path/file/line。"""
from __future__ import annotations


def route(method, path, auth=True):
    def deco(fn):
        fn.__route__ = (method, path, auth)
        return fn

    return deco


def tx(fn):
    return fn