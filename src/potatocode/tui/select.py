"""provider 选择列表：配置多于一个时，启动后以方向键列表选择活动 provider。"""

from __future__ import annotations

from textual.widgets import OptionList
from textual.widgets.option_list import Option

from ..config import ProviderConfig


class ProviderSelect(OptionList):
    """按配置顺序列出各 provider 的名称与模型，供方向键选择。"""

    def __init__(self, providers: list[ProviderConfig], *, id: str | None = None) -> None:
        self._providers = providers
        options = [
            Option(f"{p.name}  ({p.protocol} · {p.model})", id=str(i))
            for i, p in enumerate(providers)
        ]
        super().__init__(*options, id=id)

    def selected_provider(self, index: int) -> ProviderConfig:
        """按列表索引取回对应的 ProviderConfig。"""
        return self._providers[index]
