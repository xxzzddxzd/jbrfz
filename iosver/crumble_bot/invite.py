"""Friend invite binding."""
from __future__ import annotations

import logging

from .grpc_client import GrpcClient
from .headers import Session, build_metadata
from .messages import register_friend_inviter_request

log = logging.getLogger(__name__)
REGISTER_PATH = "/cc.public.game.EventService/RegisterFriendInviter"


def register_friend_inviter(client: GrpcClient, session: Session, inviter_mid: str):
    meta = build_metadata(session)
    body = register_friend_inviter_request(inviter_mid)
    resp = client.unary(REGISTER_PATH, body, metadata=meta)
    if session.adopt_resource_key(resp.headers):
        log.debug("resource_key <- %s", session.resource_key)
    return resp
