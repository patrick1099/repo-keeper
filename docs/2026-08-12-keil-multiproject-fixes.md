# 一次多工程 Keil 仓库落地暴露的六个缺陷（2026-08-12）

在一个含**两个 `.uvprojx`**、走 ARMCC v5（AC5）的 Keil 仓库上跑 `Keeper.py init`，
暴露了下面六条。全部已修，每条记录「当时什么样、为什么没被发现、修法」。
具体仓库的复盘留在别处 —— 这里只留工具自己的部分。

## 1. AC5 工程无条件 `-D__CC_ARM`

凡是走 pack 里 CMSIS 头的 AC5 工程（`core_cm*.h → cmsis_compiler.h → cmsis_armcc.h`），
clang 解析 `cmsis_armcc.h` 报 **14 errors**，器件头整棵 TU 的索引跟着死。
去掉 `__CC_ARM` 后 **0 error**，只剩一条 `#warning Not supported compiler type`。

**为什么没被发现**：`__CC_ARM` 的消费者不在仓库里，在 include path 上的 pack 头里。
grep 项目源码永远看不到它，于是「这个宏没人用」这个结论每次都成立、每次都是错的。

**修法**：`Keil2Clangd.compat_macros()` 成为唯一决定点，AC5 默认不发，`--cc-arm` 显式开
（vendored CMSIS 头或自己代码分支在这个宏上的工程才需要）。

## 2. verify 从不调用编译器（假安心）

`verify_output()` 查 `-I` 存在、源文件存在、`directory` 绝对、两文件 `-D` 集合一致 ——
全是**生成物跟自己比**。上面那条 14 errors 的配置，verify 报的是 `0 error`。
一个从不运行编译器的检查，在构造上就看不见 parse error。

**修法**：`k2c_common.probe_syntax()` 拿生成的 flags 真跑 `clang -fsyntax-only` 解析
两条 entry。`--no-syntax-probe` 可关；PATH 上没有 clang 时明说是 **SKIPPED 而非通过**。

## 3. `-o` 默认进程 CWD，与 `-p` 完全解耦

`-o/--output` 默认 `'.'`，跟 `-p/--project` 没有任何关系：从别处调用就把数据库落在
调用者当时站的目录；一个仓库两个工程时，第二次运行直接盖掉第一次的。

**修法**：Keil 与 IAR 两个后端的 `-o` 默认改成**工程文件自己的目录** —— 唯一一个是
「你要什么」的函数的默认值。

## 4. 多工程只配第一个就放弃

`Keeper.step_clangd` 只调一次 `Proj2Clangd`。遇到多个工程文件，后端按设计拒绝猜，
init 提示你指定 `--project` 然后就结束了 —— 「两个工程都配」根本无法表达。
提示文案本身还写错了 flag。

**修法**：`_clangd_projects()` 把每个 `.uvprojx` / `.ewp` 展开成一条，逐个配置；
卡住的工程逐条报告，且报告里的 flag 按后端给对（keil `-t` / iar `-c`）。
CMake 不在此列：它那些嵌套 `CMakeLists.txt` 描述的是一棵构建树，不是多个工程。

## 5. 生成块的 banner 认死当前工具名

`_strip_generated()` 按 `GENERATED_BANNER` 精确匹配定位旧生成块。工具改过一次名，
于是旧名字写的块不再「像生成的」，被当用户内容保留，新块追加在它后面 ——
同一个 `info/exclude` 里两个生成块、约 100 行重复，而 banner 自称「重新生成会整体覆盖」。
没有任何东西失败，也没有任何东西说话。

**修法**：`GENERATED_BANNER_RE` 匹配**句式**而非字面量，工具名是唯一允许变的部分；
`_strip_generated` 从第一个 banner 起整体裁掉，顺带把上面那条框线也带走。
回归测试：`test_a_block_from_the_old_tool_name_is_replaced_not_kept`。

## 6. re-anchor exe 不会自动落到仓库根

`dist/` 是 gitignore 的 PyInstaller 产物，也就是说**每一次新克隆都没有 exe**。
`deploy_reanchor_exe()` 遇到这种情况只打一行 `not built -- skipped` 就继续，
整轮 init 照样报成功。结果是仓库有 clangd 配置，却没有那个「换机器/换路径后能修好它」
的 exe —— 而一行 skip 夹在一次成功运行的中间，没人会读到。

**修法**：缺失或过期时**自动构建**（`build_reanchor_exe()`，与 `build_exe.bat` 同参数）。
不自动做的是 `pip install pyinstaller` —— 那要联网、会改这台机器，是另一种性质的决定。
另外 `Keeper` 现在把「exe 在不在仓库根」写进 summary，而不是留在 scrollback 里。
回归测试：`TestReanchorExeIsBuiltWhenMissing`。
