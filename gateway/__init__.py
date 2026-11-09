# -*- coding: utf-8 -*-
"""
8090 专属通道网页打开、鉴权登录与会话管理独立模块 (gateway)
与下游题录采集逻辑 100% 解耦：仅负责打开网页、穿透登录/验证码、检测流量配额并交付就绪 Page。
"""
from gateway.channel_8090 import (
    GatewayError,
    GatewayAuthExpiredError,
    GatewayQuotaExhaustedError,
    GatewayCaptchaBlockedError,
    GatewayMirrorUnreachableError,
    Channel8090Gateway,
)

__all__ = [
    "GatewayError",
    "GatewayAuthExpiredError",
    "GatewayQuotaExhaustedError",
    "GatewayCaptchaBlockedError",
    "GatewayMirrorUnreachableError",
    "Channel8090Gateway",
]
