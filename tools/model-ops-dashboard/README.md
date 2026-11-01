# Qwen Operations Console

这是一个只在本机 `127.0.0.1:8787` 监听的只读运维面板。后端使用本机已有的 `ssh t640` 配置获取远程 GPU、Docker、SGLang 和模型状态；网页不会保存私钥、sudo 密码，也不提供修改服务器状态的接口。

在项目根目录双击 `启动模型监控面板.cmd`，浏览器会自动打开：

```text
http://127.0.0.1:8787
```

也可以在 PowerShell 中运行：

```powershell
cd "<pilot 项目目录>"
py -3 tools/model-ops-dashboard/server.py
```

关闭终端或按 `Ctrl+C` 即可停止网站。页面默认每 5 秒刷新，展示：

- 三张 GPU 的利用率、显存、功率、温度、时钟和 persistence mode；
- 模型、SGLang、原生工具 parser、MTP、TP、上下文和并发设置；
- 容器镜像、启动命令、端口、状态和 GPU 进程；
- historical-reproduction 与 recommended-primary 采样配置；
- 七层配置清单 SHA256、源码提交和实际配置漂移检查；
- 完整原始状态 JSON。

如果页面显示 SSH 连接失败，先确认 Windows `ssh-agent` 正在运行，并验证：

```powershell
ssh t640 "hostname"
```
