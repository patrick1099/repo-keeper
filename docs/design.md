# 设计记录

README 只讲结论。这里放「为什么必须这样」的部分，都是踩出来的。

## 环节顺序

`Keeper.py init` 把落脚 → 除噪 → 索引连起来跑，顺序不能换：

- ignore 规则要在 clangd 之前落地，否则新生成的 `.clangd` 立刻变成一条未跟踪改动，
  刚清干净的 `git status` 又脏了；
- 这两件都得在离开共享分支之后做，不然是在不该动的检出里动手。

新建 worktree 是一条更短的索引路径：Git 检出完成后，先复制源工作区里除 `.git`、其他
linked worktree 和主检出专属配置之外的内容，因此 gitignore 挡住的构建与 clangd 产物也能
继承。目标与源必须指向同一提交，源工作区也不能有可见的 tracked 改动；否则不允许整树覆盖。
复制到位后调用 ReAnchor 的同一套实现修路径，只有配置不完整、归属检查失败或不是 Keil 工程
时才回到完整生成器。

对账（`clean-branch`）不进这条自动链，因为它要人现场判断冲突、迁移方向和白名单例外。

## 自检

写完不能信「文件写出去了」，要用真 git 问「现在到底忽不忽略」。三个必须踩对的细节：

**`.gitignore` 和 `.gitattributes` 都不支持行尾注释。** `Objects/  # 输出目录` 会被 git 当成
一个含空格和 `#` 的模式，匹配不到任何东西，而 git 从不报告「这条规则没匹配上任何文件」。
所以自检走 `git check-ignore -z --stdin` 和 `git check-attr`。

**`-z` 不能省。** 非 ASCII 路径会被 git 做 C-quoting，对不上就把好规则误报成失效。中文路径
在这个仓库群里是常态。

**判定幻影修改必须用 `git hash-object --no-filters`。** 不加 `--no-filters` 会套 filter，
CRLF 工作区 + LF blob 会算出相同 hash，于是把一个真有差异的文件误判成幻影。

幻影修改那一类因此有两道闸：

1. 只对「裸字节 == index blob」的文件加 `-text`。否则加 `-text` 不是暴露幻影，而是凭空造出
   真实差异，`--renormalize` 会把 CRLF 提交进去。
2. `--renormalize` 若真 stage 了内容，立刻 `git reset` 撤回。

顺序上先写属性再 renormalize，并重新探测幻影列表。

## 分支对账

适用场景：两条长期分支有角色分工（一条纯代码对外，一条是带文档和工具的超集），历史又各自
被 `filter-branch` 改写过。于是两条常规路都断了：

- **不能 merge** —— hash 全不同，merge 会把整段历史当新东西再引一遍；
- **不能用 patch-id 求差** —— 历史改写不均匀时全是假结果。

只能按 commit subject 匹配 + 代码路径的真实 tree diff 来对账。

刻意的永久分歧要填两张表：`expected_drift` 让 `verify` 不报 FAIL，`never_pick` 让 `detect`
不把肇事提交列成待办。只填前一张，`verify` 会变绿而 `detect` 仍在劝你把分歧撤销掉。

## clangd 后端

**IAR 后端不猜内建宏。** 跑 `icc<arch>.exe --predef_macros` 取全部内建宏写进生成头，用
`-imacros` 挂上；char 符号性从 `__CHAR_MIN__` 反推；`--core` 靠试编译谈判（设备头里有
`#error "... only"`，逐个候选编译，谁过用谁）。`.ewp` 里只存 IDE 下拉框索引，静态映射必错。

**配置可发现性。** clangd 只搜源文件自身目录和祖先目录，从不搜兄弟目录。而 Keil/IAR 的输出
常落在 `Proj` 或 `build`，源码在兄弟目录，症状是同文件跳转正常、跨文件跳转静默失效，且没有
任何报错。`--fix-placement` 在源码的最近公共祖先写一个指针 `.clangd`。

**生成后默认自检：** 每个 `-I` 是否存在、每个源文件是否存在、`directory` 是否为绝对路径、
`.clangd` 与 `compile_commands.json` 的 `-D` 是否一致。失败即 exit 1，JSON 信封错误码
`E_VERIFICATION_FAILED`，details 带 errors/warnings 摘要。

## 配置合并语义

数组整体替换而不是拼接，是刻意的：数组是一个值，不是一个集合。拼接会让「我明明删了一项
怎么还在」变成常见困惑，而且继承来的列表将永远缩不了。

项目层配置放在主检出的根上，一份文件服务所有 linked worktree —— 每个 worktree 各存一份就是
它们开始漂移的起点。
