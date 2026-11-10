"""VC-R-01 本地网页界面（单人使用：无登录、无多人协作）。

模块
----
view.py    展示层纯函数：把库里取出来的行整理成"界面能直接画"的结构。
server.py  本地 HTTP 服务：只做 IO 与转发，规则一律经 CLI 调 src/main.py。

设计前提见 server.py 顶部注释与 界面说明.md。
"""

__all__ = ["view", "server"]
