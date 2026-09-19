# ch08 本地端到端执行结果

受控 provider、真实 SDK + 内存 HTTP、Agent/文件/权限引擎/Textual App.run_test；无真实 API 请求。

- PASS：轻量存盘后工具续调
- PASS：工具执行后自动压缩并继续
- PASS：实际超限后紧急压缩，工具不重复执行
- PASS：再次超限停止，不递归摘要
- PASS：Textual 手动压缩，模式和近期原文保留
- PASS：Textual 静默流取消后再次输入
- PASS：Textual 三次失败熔断后新操作恢复
- PASS：Textual 缺窗口阻止请求，补充配置后恢复
- PASS：anthropic SDK：多轮工具→自动摘要→继续
- PASS：anthropic SDK + Textual：手动→取消/失败→再输入
- PASS：openai SDK：多轮工具→自动摘要→继续
- PASS：openai SDK + Textual：手动→取消/失败→再输入

结果：12/12 通过。
