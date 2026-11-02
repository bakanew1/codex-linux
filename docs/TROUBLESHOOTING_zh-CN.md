# 故障排查

## 先定位层次

- 无法启动：看启动时的错误，检查代码是否配套以及本机私有配置；不要重建凭据。
- 工具可发现但调用失败：不等于 Linux 出错。区分本机 HTTP、进程启动、后端认证与文件入口。
- 本地默认端口为 127.0.0.1:8765，端口连通只说明 TCP 可达，不证明整个请求成功。
- 无认证 GET 返回 501 只说明该 HTTP 方法不支持，不等于代理没运行。
- 当前客户端对真正本机传输异常可附带异常类型和数字 errno/winerror；不输出完整异常文本、URL、头部或令牌。

## 脱敏诊断

broker 窗口可能出现：

    HPC_DIAG stage=BACKEND_PROCESS code=AUTHENTICATION
    HPC_DIAG stage=PROCESS_START errno=...

诊断只提供固定类别或数字错误，不暴露原始标准错误。不要把原始凭据或完整配置粘贴到 issue。

| 现象 | 处理 |
|---|---|
| AUTHENTICATION | 用户检查 ssh-agent 是否 Running，并用专用密钥重新 ssh-add；不要让模型读取 agent 身份 |
| NAME_RESOLUTION | 用户本机核对既有别名配置，不把配置发给模型 |
| HOST_KEY | 用户通过可信渠道核对指纹；不关闭严格校验 |
| REMOTE_MODULE_MISSING / REMOTE_MODULE_IMPORT | 用户检查两端是否来自同一完整包，尤其 bulk_read.py 和辅助模块 |
| EXECUTABLE_OR_ENTRY_MISSING | 用户核对固定 Python/入口路径；该类别可能来自多个文件，不自动猜路径 |
| CONNECTION_FAILED | 既可能本机 HTTP，也可能后端子进程/IO失败；结合诊断，不盲目重启 |
| UNAVAILABLE | 检查缺模块、混合版本、平台/文件权限等 |
| RATE_LIMIT / BUSY | 停止相应读取，说明原因，稍后重试；不切换其他通道绕过 |
| FILE_CHANGED | 等输出写完，重新核对完整性 |
| NOT_FOUND | 核对授权根内的相对路径，不自动扩大 root |
| UNSAFE_PATH | 路径/文件类型被拒绝，不跟随链接或改权限规避 |
| ALREADY_EXISTS | 不覆盖、不删旧文件，也不为规避限制悄悄改名 |

程序运行窗口没有请求日志是正常的。生成目录错误、端口占用、配置损坏并不能用重置所有密钥来通用解决。
