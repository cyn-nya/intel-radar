"""数据源适配器集合。新增一条管道 = 新增一个模块。"""
from . import github_trending  # noqa: F401

__all__ = ["github_trending"]
