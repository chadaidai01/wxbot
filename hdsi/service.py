# -*- coding: utf-8 -*-
"""InterludeService 组合层，对应上游 src/service.ts 的 InterludeService 类。

上游是单文件 8524 行；为可维护性与并行移植，按职责拆成若干 mixin（见 SERVICE_PORTING_SPEC.md），
本文件负责把它们组合成与上游同名、同语义的服务类。
"""

from __future__ import annotations

from .service_base import ServiceBaseMixin
from .service_decision import ServiceDecisionMixin
from .service_desktop import ServiceDesktopMixin
from .service_inbound import ServiceInboundMixin
from .service_media import ServiceMediaMixin
from .service_memory import ServiceMemoryMixin
from .service_narrative import ServiceNarrativeMixin
from .service_schedule import ServiceScheduleMixin
from .service_story import ServiceStoryMixin

__all__ = ['InterludeService']


class InterludeService(
    ServiceBaseMixin,
    ServiceDesktopMixin,
    ServiceStoryMixin,
    ServiceInboundMixin,
    ServiceMediaMixin,
    ServiceNarrativeMixin,
    ServiceDecisionMixin,
    ServiceScheduleMixin,
    ServiceMemoryMixin,
):
    """HDS-Interlude 主服务；方法由各 mixin 提供，语义与上游逐字一致。"""
