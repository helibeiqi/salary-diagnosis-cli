# -*- coding: utf-8 -*-
"""comp-agent-harness 源码包。

显式声明为常规包（而非隐式命名空间包），让
`python -c "import src.tools.registry"` 与 dsh 插件 spawn 出的子进程
在任意工作目录下都有确定一致的导入行为。
"""
