"""接入适配器：把具体业务仓库的动作挂到 Hub 上。"""

from . import balanceai, stock

__all__ = ["balanceai", "stock"]