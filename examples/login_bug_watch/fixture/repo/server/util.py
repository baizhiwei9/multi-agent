"""口令校验与令牌（fixture）。"""
from __future__ import annotations

import hashlib


def check_password(user, password):
    digest = hashlib.sha256(str(password).encode("utf-8")).hexdigest()[:16]
    return bool(user) and user.get("hash") == digest


def make_token(user_id):
    return f"t-{user_id}"