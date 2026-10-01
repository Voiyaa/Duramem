"""跨模块共享的异常类型。

`DuramemError` 原来定义在 service.py。导入/归档域拆出后（A.15），领域模块
也要抛它，而领域模块不能反过来 import service（会成环）——所以异常类型
下沉到这个叶子模块，service.py 原地再导出：`from duramem.service import
DuramemError` 的既有写法全部照旧。
"""

from __future__ import annotations


class DuramemError(RuntimeError):
    pass


__all__ = ["DuramemError"]
