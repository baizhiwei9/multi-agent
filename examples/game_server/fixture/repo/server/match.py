"""匹配接口（示例）。"""

from .router import route, tx


@route("POST", "/v1/match/join", auth=True)
@tx
def join_match(payload):
    return {"room_id": "r-1", "players": [payload.get("player_id")]}