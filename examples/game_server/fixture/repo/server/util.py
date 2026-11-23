"""工具函数（示例）：故意留一个裸 except，审计会报 broad_except。"""


def safe_call(fn):
    try:
        return fn()
    except Exception:
        pass