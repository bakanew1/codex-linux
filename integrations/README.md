# 本地科研分析接入

代理负责取回文件，不自动解析全部 VASP 格式，也不运行服务器 VASPKIT。
Windows 本地分析 Skill 可以消费 MCP 返回的完整字节。不要把连接配置或密码转交给绘图工具。

当前配套的 vaspkit-results Skill 可做部分文本数值和 scalar cube/XSF 分析；它是单独组件，不与本代理打包安装。
原始 DOSCAR/LOCPOT 能读取不等于任意版本都已解析。曾验证一个标准自旋 DOSCAR 的总 DOS 适配，不代表通用 PDOS 支持。
优化任务的 DOSCAR 不自动成为最终静态电子结构证据。

默认绘图曾切换至 EditaPlot / Origin。EditaPlot 现有路线要求受支持的 Origin 版本；Origin 9.0 兼容层尚未实现。
本仓库不内嵌 EditaPlot、nature-figure、Origin 或势库；不发布计算输入/结果、POTCAR、用户报告或个人路径。
