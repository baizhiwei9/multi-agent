"""服务器循环（示例）：故意留一个忙等，审计会报 busy_wait。"""


def spin():
    while True:
        pass