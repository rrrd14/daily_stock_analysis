# -*- coding: utf-8 -*-
"""Shared test helper to keep litellm imports lightweight in unit tests."""

import sys
import types


def ensure_litellm_stub() -> None:
    """Install a minimal litellm stub unless a test already provided one."""
    if "litellm" in sys.modules:
        return

    litellm_stub = types.ModuleType("litellm")

    class _DummyRouter:  # pragma: no cover
        """Minimal stand-in for ``litellm.Router``.

        必须接受与真实 Router 相同的构造参数：否则当本 stub 先于真实 litellm
        被安装时，``Router(model_list=...)`` 会抛 TypeError，导致同一批测试在
        全量运行（先导入真实 litellm）与子集运行（先安装 stub）下结果不一致。
        """

        def __init__(self, *args, **kwargs):
            self.model_list = kwargs.get("model_list", list(args))
            self.model_group_alias = kwargs.get("model_group_alias", {})

        def completion(self, *args, **kwargs):
            """stub 不发起真实请求。"""
            return None

        async def acompletion(self, *args, **kwargs):
            return None

        def completion_cost(self, *args, **kwargs):
            return 0.0

    class _DummyRateLimitError(Exception):
        pass

    class _DummyContextWindowExceededError(Exception):
        pass

    litellm_stub.Router = _DummyRouter
    litellm_stub.RateLimitError = _DummyRateLimitError
    litellm_stub.ContextWindowExceededError = _DummyContextWindowExceededError
    litellm_stub.completion = lambda **kwargs: None
    sys.modules["litellm"] = litellm_stub
