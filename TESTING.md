# 验证记录

日期：2026-11-02。Windows / Python 3.12.6。

    python -E -s -W error::ResourceWarning -m unittest discover -s tests -v

结果：78 项，54 项通过，24 项平台跳过，无失败。
Linux/POSIX 的 nofollow、真实 dir_fd 权限等测试本机无法执行；不能将跳过当作已验证。
本整合快照尚未在 Linux 上重新运行全套测试，也未验证 GitHub 托管 CI。

测试包括：四工具协议、64 KiB 单读、tail、50 项批读/512 KiB 上限、逐项错误、UTF-8 字节保真、
20 次滚动限流与重置、HTTP 鉴权/来源/体积限制、写入排他、防路径逃逸、进程退出清理及脱敏诊断。
仅运行受控本地 Python 子进程、回环 HTTP 和临时文件，不进行真实 SSH/HPC 连接。

代码公开前按白名单收集，未读取或上传真实私有配置、密钥、凭据、研究输入/输出和个人文档。
运行代码与当前部署的七个文件逐一 SHA256 一致；本次没有替换实际运行代码。
