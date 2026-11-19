# 外置 MCP 目录

外部 MCP 服务器统一在 `extensions/mcp.json` 中按业界标准 `mcpServers` 结构登记（可从 `extensions/mcp.example.json` 复制），本地自定义 MCP 脚本可存放在当前 `extensions/mcp/` 目录下。

示例（联网搜索与网页正文提取）：

```json
{
  "mcpServers": {
    "deepseek_web": {
      "command": "uvx",
      "args": ["deepseek-search-fetch-mcp"],
      "env": {
        "DEEPSEEK_API_KEY": "sk-..."
      }
    }
  }
}
```

登记后打开 `/worker <id>` 配置窗口，在 MCP 多选框中勾选对应名称即可装配给该 Worker。
