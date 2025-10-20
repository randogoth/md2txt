from __future__ import annotations

from typing import Any

from conversion_core import ParserFactory, RendererFactory
from plugin_registry import PluginRegistry


parser_plugins = PluginRegistry[ParserFactory]()
renderer_plugins = PluginRegistry[RendererFactory[Any]]()


def register_parser(name: str, factory: ParserFactory) -> None:
    parser_plugins.register(name, factory)


def register_renderer(name: str, factory: RendererFactory[Any]) -> None:
    renderer_plugins.register(name, factory)


def get_parser_factory(name: str) -> ParserFactory:
    return parser_plugins.get(name)


def get_renderer_factory(name: str) -> RendererFactory[Any]:
    return renderer_plugins.get(name)


def available_parsers() -> list[str]:
    return parser_plugins.names()


def available_renderers() -> list[str]:
    return renderer_plugins.names()
