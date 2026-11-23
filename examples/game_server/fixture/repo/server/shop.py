"""商城接口（示例）。"""

from .router import route, tx


@route("POST", "/v1/shop/buy", auth=True)
@tx
def buy_item(payload):
    return {"ok": True, "item": payload.get("item_id")}