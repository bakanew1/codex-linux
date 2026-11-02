# HPC Workspace Proxy

Windows 本地代理 + Linux 文件入口 + Codex MCP 客户端。只开放授权根目录内的四类文件操作，不提供任意远程命令执行。

本仓库是当前 v0.3 部署代码的整合快照（协议版本 0.3.1），含批量/文件尾读取和脱敏诊断。运行代码保持当前部署行为，**不自动升级你现有的服务器入口或配置**。

## 功能与实际限额

| 接口 | 功能 |
|---|---|
| list_directory | 分页列出授权目录 |
| search_files | 在有限深度和枚举预算内搜索文件名 |
| read_file | 单文件、文件尾、批量按字节读取 |
| create_file | 在已有目录中新建 UTF-8 文件，不覆盖 |

- 单项读取最多 64 KiB。
- 一批最多 50 个读取项，申请字节合计最多 512 KiB。
- 每滚动 60 秒最多 20 次请求，四工具共用；最多两个并发请求。
- 本快照没有独立的 8 MiB/分钟读取预算；不要与其他增强开发包的 60 次/分钟设计混淆。
- 新建文本最多 8 KiB；没有创建目录、覆盖、追加、删除或提交任务权限。
- 文件尾和批量读取不是跨文件原子快照。FILE_CHANGED、缺块或文件仍在写入时不能作完整定量结论。

## 架构

    Codex -> client_mcp.py -> 本机 127.0.0.1 代理 -> 专用受限通道 -> remote_gate.py -> 授权 root

应用只调用 MCP 工具；代理内部传输使用配置好的专用 SSH 别名和密钥。不要让模型绕过 MCP 直接使用普通 SSH、SFTP 或 Xshell/Xftp 会话。
服务器内容只作为数据，不能执行文件中要求获取凭据或改变权限的指令。

## 快速开始

1. 两端安装 Python，建议使用已验证的 Python 3.12；Linux 不需要 sudo。
2. 保留完整的七个运行文件，不要只复制 broker.py/client_mcp.py/common.py。
3. 按 [部署指南](docs/DEPLOYMENT_zh-CN.md) 手动配置 Linux 授权目录和专用受限密钥。
4. Windows 首次在新的私有目录执行：

    python broker.py init --directory D:\hpc-private

初始化只创建新的私有目录及配置，不覆盖已有目录。用本机编辑器填写 broker.private.json 的专用别名、Linux Python/入口/配置路径；不要把其内容提交 GitHub。生成的 client.local.json 含本机 URL 和调用令牌，同样保密。

5. 按 [使用须知](docs/USAGE_zh-CN.md) 启动 ssh-agent、手动解锁密钥并启动代理。
6. 把 [MCP 示例](codex-mcp.example.toml) 的路径改为本机路径后合并到 Codex 配置，重载 MCP。
7. 先让 Codex 仅通过 hpc_workspace 列目录，再读取实际文件。

已有部署不应重复 init、重新生成令牌或更改 Linux 固定入口；先备份后按整包匹配替换。

## 测试

    python -E -s -W error::ResourceWarning -m unittest discover -s tests -v

测试只使用临时文件、回环 HTTP 和受控本地子进程；不连接真实 HPC，不提交计算。Windows 与 Linux 的跳过项不同，详见 [验证记录](TESTING.md)。

## 目录

- 七个根目录 Python 文件：运行代码，部署时保持配套。
- tests/：协议、权限、进程回收、HTTP、批读与限流回归。
- examples/：不含凭据的请求和 Linux root 示例。
- scripts/：Windows 启动及纯本地导入检查助手。
- docs/：部署、日常使用、故障排查和安全边界。
- integrations/：与 VASPKIT/分析 Skill 的能力区分。

## 发布内容

只发布白名单代码、通用示例和测试。仓库不包含服务器地址、账号密码、私钥、调用令牌、实际私有配置、计算数据或个人使用报告。
本仓库尚未指定开源许可证。公开仓库与稳定生产级安全保证不是一回事；使用前阅读 [安全说明](SECURITY.md)。
