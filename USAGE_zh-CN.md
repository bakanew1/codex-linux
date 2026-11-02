# 使用须知

## 下次要打开哪些程序

日常读取只需要 PowerShell（代理）和 Codex。Xshell/Xftp 仅由用户手动做 Linux 计算、上传或维护；模型不查看、不操作这些窗口或保存的会话。
不需要预先在 Xshell 登录，代理使用自己的专用通道。Skill 和 Python 分析在 Windows 运行。

## 开机后准备

普通 PowerShell 检查：

    Get-Service -Name ssh-agent

如果服务停止，由用户在管理员 PowerShell 执行：

    Start-Service -Name ssh-agent

若服务被禁用，先设为 Manual，再启动：

    Set-Service -Name ssh-agent -StartupType Manual
    Start-Service -Name ssh-agent

这只是 Windows 本机服务，不是 Linux sudo。不要在 ssh-agent 后面输入中文顿号。
回到普通 PowerShell，用自己的专用私钥路径执行：

    ssh-add "$env:USERPROFILE\.ssh\YOUR_RESTRICTED_KEY"

用户亲自输入私钥长口令；输入时没有字符显示是正常现象。显示 Identity added 后继续。不要将口令或私钥发给模型。
Xshell 保存的登录密码不会自动替 OpenSSH 解锁密钥。

## 启动与结束

代码目录示例：

    python broker.py serve --config D:\hpc-private\broker.private.json

或者：

    powershell -File scripts\start-broker.ps1 -ConfigPath D:\hpc-private\broker.private.json

启动后 PowerShell 保持占用、通常没有请求日志是正常现象。不要并开第二份代理。
结束时按 Ctrl+C。读取完成后，本地完整缓存可继续分析；Windows 重启或密钥状态丢失后可能需要重新启动 agent/加载密钥。

## 给 Codex 的请求

先测试：

> 仅通过 hpc_workspace，列出授权根目录前 10 项，不使用 SSH 或桌面控制。

读取结果：

> 仅通过 hpc_workspace，检查 calculation/OUTCAR 的最后 4096 字节，不修改文件。

使用相对路径，不需要把服务器地址或密码发到聊天里。授权根目录由代理配置决定，不由一句自然语言临时扩大。
分析任务默认只读；只有明确要求新建文件时才使用 create_file，且必须位于已存在目录，最多 8192 个 UTF-8 字节。

## read_file 请求示例

    {"path":"calculation/OUTCAR","offset":0,"length":65536}
    {"path":"calculation/OUTCAR","tail":4096}
    {"files":[{"path":"calculation/INCAR","offset":0,"length":1024},
              {"path":"calculation/OUTCAR","tail":4096}]}

tail 单位是字节，不是行。50 项可以是不同文件，也可以是同一文件不同位置；没有全程只许读 50 个文件的限制。
每批申请量合计最多 512 KiB。预算按参数申请量而不是实际较短返回值判断。
批量内合法路径缺文件等按项返回错误；非法路径或非法参数可能整批拒绝。

## 完整性

只有 offset=0 到 size 全部字节连续覆盖，才可称为完整。尾部 EOF=true 不代表取得整文件。
保留原始 base64/字节，跨块重组完成后再解码 UTF-8。批量 UTF-8 文本可只返回 text_utf8、data_base64 为 null。
文件变化时停止相关定量分析；普通多块读取不能检测所有同大小原地改动，不提供跨块原子快照。
没有从输出稳定自动推断集群任务结束的功能；没有常驻监控或自动提交任务功能。
