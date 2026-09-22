"""中文版 argparse 帮助输出（Hermes 汉化 · --help 批次）。

argparse 的 ``usage:`` / ``positional arguments:`` / ``options:`` /
``-h, --help  show this help message and exit`` 全部来自 Python 标准库，
不是本仓文案，所以只能用子类覆写。

**为什么只需要改顶层 parser**：``add_subparsers()`` 默认把 ``parser_class`` 设为
``type(self)``，而 ``add_parser()`` 内部是 ``self._parser_class(**kwargs)``。
因此只要顶层用 :class:`ZhArgumentParser`，子命令/孙命令的 parser 会自动继承
同一个类与 formatter —— 整棵 ``--help`` 树一次覆盖（已用三层 parser 实测）。

**注意**：本仓有 4 处 ``add_parser(..., formatter_class=argparse.RawDescriptionHelpFormatter)``
显式传参，会覆盖默认值。:class:`ZhArgumentParser` 因此会把已知的标准 formatter
**自动升级**成对应的中文版本，而不是只在缺省时兜底。
"""

from __future__ import annotations

import argparse

# argparse 内置节标题 → 中文
ZH_SECTIONS = {
    "positional arguments": "位置参数",
    "options": "选项",
    "optional arguments": "选项",       # Python < 3.10 的旧标题
    "required arguments": "必需参数",
    "subcommands": "子命令",
}

_HELP_FLAG_TEXT = "show this help message and exit"
_HELP_FLAG_ZH = "显示此帮助信息并退出"


class _ZhMixin(argparse.HelpFormatter):
    """中文覆盖逻辑；与具体 HelpFormatter 基类组合使用（菱形继承，MRO 合法）。"""

    class _Section(argparse.HelpFormatter._Section):
        """节标题的半角冒号是 **硬编码在 _Section.format_help 里** 的
        （``heading_text = _('%(heading)s:')``），不是 formatter 的方法，
        所以只能覆写 _Section。父类返回的字符串里标题只出现一次，安全替换。"""

        def format_help(self):
            out = super().format_help()
            heading = self.heading
            if isinstance(heading, str) and heading and not out.startswith("\n" + heading + "："):
                out = out.replace(heading + ":", heading + "：", 1)
            return out

    def _format_usage(self, usage, actions, groups, prefix):
        # 只在 argparse 传 None 时替换（它代表默认的 "usage: "）。
        # 注意：add_subparsers() 会用 prefix='' 调用本方法去推算子命令的 prog，
        # 若把 '' 也当成 None（`prefix or ...`）会把中文前缀污染进 prog，
        # 结果就是 `用法：用法：hermes config ...`。
        if prefix is None:
            prefix = "用法："
        return super()._format_usage(usage, actions, groups, prefix)

    def start_section(self, heading):
        key = str(heading) if heading is not None else ""
        super().start_section(ZH_SECTIONS.get(key, heading))

    def add_argument(self, action):
        # 只拦 ``-h/--help`` 那条自动生成的说明，不碰业务 help=
        try:
            if getattr(action, "help", None) == _HELP_FLAG_TEXT:
                action.help = _HELP_FLAG_ZH
        except Exception:
            pass
        return super().add_argument(action)


class ZhHelpFormatter(_ZhMixin, argparse.RawDescriptionHelpFormatter):
    """默认中文 formatter（保留 RawDescription 的段落原样输出行为）。"""


class ZhRawTextHelpFormatter(_ZhMixin, argparse.RawTextHelpFormatter):
    """对应 ``argparse.RawTextHelpFormatter`` 的中文版。"""


class ZhMetavarTypeHelpFormatter(_ZhMixin, argparse.MetavarTypeHelpFormatter):
    """对应 ``argparse.MetavarTypeHelpFormatter`` 的中文版。"""


#: 标准 formatter → 中文版（显式传参时自动升级）
_FORMATTER_UPGRADE = {
    argparse.HelpFormatter: ZhHelpFormatter,
    argparse.RawDescriptionHelpFormatter: ZhHelpFormatter,
    argparse.RawTextHelpFormatter: ZhRawTextHelpFormatter,
    argparse.MetavarTypeHelpFormatter: ZhMetavarTypeHelpFormatter,
}


class ZhArgumentParser(argparse.ArgumentParser):
    """默认使用中文帮助输出的 ArgumentParser。

    - 未指定 ``formatter_class`` 时用 :class:`ZhHelpFormatter`；
    - 显式指定了上面映射表中的标准 formatter 时，自动升级为中文版本
      （本仓有多处 ``formatter_class=argparse.RawDescriptionHelpFormatter``）。
    - ``add_subparsers`` 显式对齐 ``parser_class``，保证子孙 parser 继续继承。
    """

    def __init__(self, *args, **kwargs):
        fc = kwargs.get("formatter_class")
        if fc is None:
            kwargs["formatter_class"] = ZhHelpFormatter
        elif fc in _FORMATTER_UPGRADE:
            kwargs["formatter_class"] = _FORMATTER_UPGRADE[fc]
        super().__init__(*args, **kwargs)

    def add_subparsers(self, **kwargs):
        kwargs.setdefault("parser_class", type(self))
        return super().add_subparsers(**kwargs)
