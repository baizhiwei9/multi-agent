"""玩家接口（示例）。这里的缺陷是故意留的，用来演示审计能抓到什么。"""
from .router import route, tx

# 故意留一个硬编码密钥（审计会报 secret_hardcoded）
API_KEY = "sk_live_abcdef1234567890"   # TODO: 移到密钥管理


@route("POST", "/v1/player/login", auth=False)
def login(payload):
    return {"token": "t-demo", "player_id": payload.get("player_id")}


@route("GET", "/v1/player/profile", auth=True)
def profile(payload):
    return {"player_id": payload.get("player_id"), "level": 12}


@route("POST", "/v1/player/ban", auth=False)
@tx
def ban(payload):
    return {"ok": True, "banned": payload.get("player_id")}


@route("GET", "/v1/debug/state", auth=False)
def debug_state(payload):
    return {"rooms": 3, "players": 42}


@route("GET", "/v1/legacy/rank", auth=False)
def legacy_rank(payload):
    return {"rank": []}


@route("POST", "/v1/mail/claim", auth=True)
def claim_mail(payload):
    # 没有事务、没有幂等键：重放会重复发奖
    return {"ok": True, "items": ["gold"]}