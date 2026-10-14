# -*- coding: utf-8 -*-
"""mcpclient 层：最小 MCP 客户端 + 管理器（接上任意 MCP server 即有工具）。"""
from .client import MCPClient, MCPError
from .manager import MCPManager

__all__ = ["MCPClient", "MCPError", "MCPManager"]
