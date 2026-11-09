# 安装与环境适配

首次安装、换机器或迁移工程时，先准备本机工具和配置。仓库操作从 [using-repo-keeper](skills/using-repo-keeper/SKILL.md) 进入。

## 基础工具与路径

基础脚本需要 Python 3.11+ 和 Git。用选定解释器执行 `scripts/Keeper.py --ai-help`，从本文件目录解析脚本路径。宿主没有 `CLAUDE_PLUGIN_ROOT` 时直接传解析后的插件根；解释器路径只存本机配置。

clangd 功能再检测 clangd 和对应工具链：Keil、IAR 或 CMake 按实际工程选择。IAR 后端需要真实编译器提取预定义信息，不能用另一台机器的编译器路径或猜测结果替代。

全局配置为用户主目录下的 `.repo-keeper/defaults.toml`，项目配置为主检出根的 `.repo-keeper.local.toml`。沿用现有配置与 explain 输出核对来源；身份、分支和代码范围使用该项目已确认的值。工程换位置先检查 re-anchor 能力，不覆盖手工维护的配置。

## 可选全局命令

Windows 可用 [安装脚本](scripts/install_hygiene_cli.bat) 构建并安装 repo-hygiene；这条路径额外需要 PyInstaller。只调用 Python 脚本时无需为了安装插件而打包 exe。全局命令目录由本机用户配置和系统目录接口确定，安装后从新进程验证 PATH；插件升级不会自动更新旧 exe。

## 可选 Git hooks

本仓的发布检查属于 Git hooks，和 Claude、Codex 的会话 hooks 分开。实现入口为 [GateCheck.py](scripts/GateCheck.py)，先读其 `--ai-help`。以下参数接在选定 Python 与脚本路径后：

```text
install-hooks --check --json
install-hooks --stage <已存在的临时目录> --json
install-hooks --activate --json
```

先检查现有 `core.hooksPath`、用户钩子与接力关系，再检查计划或临时落盘结果；只有本次需求包括启用发布检查时才 activate。保留原有 commit-msg/pre-push 的有效行为，不能为安装覆盖掉用户的规则。sync-exempt 会联网读取私有仓信息，按其独立用途使用，不作为默认安装步骤。

新系统须检查 Git 启动钩子的 shell、Python 发现与退出码传播。AI 宿主不同不要求重写 Git 钩子；Git 所在系统不同才可能需要启动壳适配。

## 验收与维护

基础入口以帮助和代表性只读扫描验收；clangd 用真实工程验证跨文件定义跳转。Git hooks 在临时仓验证允许、拒绝及修正后通过，不能拿安装脚本成功当作已拦截成功。

升级后核对 Python 脚本、可选 exe 和已安装钩子的版本。卸载只处理本工具拥有的入口，恢复确认过的原钩子配置；不要为卸载删除工程配置或用户其他 hooks。
