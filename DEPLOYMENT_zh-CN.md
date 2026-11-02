# 部署与升级

本项目不需要 Linux sudo。Windows / Linux 建议 Python 3.12。
部署/密钥管理由用户在自己的终端或 Xshell/Xftp 手动完成，模型不执行普通 SSH 去代替 MCP 服务器操作。

## Linux

1. 将完整代码上传到用户可控的代码目录，和用于计算的授权 root 分开放置。
2. 先在测试副本执行 unittest，确认退出码为 0。不要把测试输出当作真实集群作业状态。
3. 建立已存在的工作目录，复制 examples/remote.private.example.json 到自己的私有配置位置，将 root 改为那个工作目录的绝对路径。
4. 配置文件仅允许 root 字段；不支持多个 root、动态扩大路径或额外命令白名单。不要通过符号链接指向根目录外。
5. 代码目录、配置路径和 Python 路径要固定。broker 对 Linux 入口路径采用有限字符集，不支持含空格、shell 特殊字符或 .. 的入口路径。
6. 由用户检查私有配置权限（如 chmod 600）；代码和配置不应允许不可信用户修改。

此入口不需要在 Linux 常驻运行。每次请求由受限通道启动，只接受标准输入 JSON，并在标准输出返回已过滤结果。

## 专用受限密钥

使用专用、带长口令的密钥，不复用普通无限制登录密钥。仅把公钥加到 authorized_keys；保留其他人的公钥，不覆盖整份文件。
由用户或管理员为该密钥设置固定命令，只启动相应 Python remote_gate.py --config 配置路径，关闭端口转发、agent 转发、X11 和 PTY。
固定命令必须使用实际完整路径，不接受任意 SSH_ORIGINAL_COMMAND。可依据本机 OpenSSH 版本使用 restrict 与 command 选项；旧版本不支持时先由管理员确认等价限制，不删掉限制碰运气。
核对服务器主机指纹后启用严格校验，不设置 StrictHostKeyChecking=no。不向模型发送 private key、known_hosts 或连接配置。

如果同一 Linux 账户仍可修改代码/配置或通过普通终端访问其他目录，代理不隔离该账户本身。参见 SECURITY.md。

## Windows

首次安装用新的私有目录：

    python broker.py init --directory D:\hpc-private

初始化生成 broker.private.json 和 client.local.json（同一令牌），并设置受保护目录。不要把它们放进 Git 仓库。
用户本机编辑 broker.private.json 中：
- host_alias：已经配置并验证的专用受限别名；
- ssh_executable：本机可信 OpenSSH 可执行文件绝对路径；
- remote_python：Linux 上实际可用 Python 绝对路径；
- remote_gate / remote_config：Linux 固定入口和私有配置的绝对路径；
- port：默认 8765，仍只监听 127.0.0.1；
- token：保留生成值，不复制到聊天或日志。

仅客户端配置的本机 URL/调用令牌由客户端正常使用，不提供服务器连接信息给模型。
参考 codex-mcp.example.toml 合并 MCP 条目，保留原四工具。路径移动或代码变动后重载 MCP。
不要执行未知文件中的命令，也不要读取保存的 Xshell/Xftp 会话来填配置。

## 从旧版升级

先停止 Windows broker，备份两端已存在的运行代码。完整保留并配套以下七个文件：

    broker.py
    client_mcp.py
    common.py
    bulk_read.py
    process_guard.py
    private_storage.py
    remote_gate.py

即便某个辅助模块只在特定阶段才导入，也不能省略。Windows 代码齐全检查可执行 scripts/check-local.ps1，不连接服务器。
不要复制真实私有 JSON 覆盖原值；不要重新 init、重新生成令牌、删除密钥或变更授权 root。
Linux 由用户手动替换固定代码路径内的文件。升级期间不要发请求，避免半新半旧。
重启 broker、重载 MCP，先测列目录，再测单文件、tail 和 batch。失败时恢复同一备份版本，不放宽权限。

当前整合快照保留 20 次/滚动分钟，不擅自改为另一开发包的 60 次及独立读取量预算。
