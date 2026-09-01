# -*- coding: utf-8 -*-
"""
tests 包标记文件。

为什么必须有这个文件：
--------------------------------------------------------------------------------
本机 Python 的 site-packages 下存在第三方同名包 `tests`（带 __init__.py），
同名包 `tests`（带 __init__.py）。Python 的模块查找规则是「常规包优先于命名空间包」，
即使我们把项目根插到了 sys.path[0]，无 __init__.py 的 tests/ 目录仍会被那个
第三方包遮蔽，导致 `import tests.fixtures.mock_results` 报 ModuleNotFoundError。

因此这里与 tests/fixtures/__init__.py 都必须存在，保证导入的是本项目的测试代码。
--------------------------------------------------------------------------------
"""
