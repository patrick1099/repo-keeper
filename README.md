# repo-keeper —— 仓库管家

嵌入式固件仓库的管家。主线只有一句：**保住一条干净分支，只让代码提交进去。**

Claude Code 插件，也可以当独立脚本用（纯 stdlib，Python 3.11+）。

```bash
py -3 scripts/Keeper.py init -p <仓库路径>
```

一条命令：判断当前分支 → 生成两层配置模板 → 治理 git 噪音 → 生成 clangd 配置 →
报告分支对账状态。退出码 **0 = 就绪，1 = 有需要你决定的事，2 = 出错**。

## 四个环节

| 环节 | 做什么 | skill |
|---|---|---|
| ① 落脚 | 不在共享分支上干活 → 开 worktree | `using-repo-keeper` |
| ② 除噪 | 让 `git status` 只剩代码改动 | `repo-hygiene` |
| ③ 索引 | 生成并自检 `.clangd` + `compile_commands.json` | `clangd-config` |
| ④ 对账 | 只把**代码**提交搬进干净分支 | `clean-branch` |

①②③ 是机械的，由 `Keeper.py` 按正确顺序连起来跑。顺序本身是承重的：ignore 规则要在
clangd 之前落地，否则新生成的 `.clangd` 立刻变成一条未跟踪改动；而这两件都得在离开
共享分支之后做，不然是在不该动的检出里动手。④ 要人现场判断（冲突、迁移方向、白名单
例外），所以只做路由。

用 Claude Code 的话，**只需要记住 `using-repo-keeper` 一个名字**，它自己分派。

## 两条承重原则

### 一、默认一个字节都不进仓库

`.gitignore` 和 `.gitattributes` 是**从工作分支拉下来的共享文件**。改它们，就把一个人
的整理动作变成了所有同事都要 review、要解冲突的提交。所以每种修法都有一个每份 clone
各自生效的形式，**默认走的就是它**：

| 共享写法 | 本地写法（默认） |
|---|---|
| `.gitignore` | `.git/info/exclude` |
| `.gitattributes` | `.git/info/attributes` |
| skip-worktree | 本来就是本地的（标记在 `.git/index`） |

代价如实说、不藏：这三样都不会被 clone、不会被 push，重新检出要重跑；而且**仓库自己的
`.gitignore` 优先级高于 `.git/info/exclude`**（实测），被仓库规则挡住的文件本地放不
出来，`!xxx` 在这里打不过 —— 撞上就如实报告，不写一条静默失效的规则。真要动共享文件
加 `--shared`。

### 二、有歧义一律非零退出，不猜

工程/target/configuration 多选而没指定、必填配置键缺失、worktree 放哪 —— 全都停下来问。
理由是一致的：**猜错了会安静地跑在错的东西上，并且看起来一切正常。** 猜错的分支 ref
让整轮对账跑在错的分支上；猜错的 target 生成一份宏全不对的 `.clangd`；猜错的 worktree
位置在别人磁盘上留垃圾。

## 一、repo-hygiene —— 让 `git status` 只剩代码

Keil/IAR 仓库的待提交列表长期被编译残留、IDE 状态和索引配置淹没。这些噪音看着是一类，
实际需要**三种互不通用**的修法，选错了不是效果差，是根本不生效、或者把同事的文件删了：

| 噪音 | 例子 | 修法 | 用错会怎样 |
|---|---|---|---|
| 未跟踪的生成物 | `Objects/`、`*.map`、`.clangd`、`~$说明书.docx` | ignore 规则 | — |
| **已跟踪**、只有本机在改 | `*.uvoptx`、`RTE_Components.h`、IAR `*.pbd` | `git update-index --skip-worktree` | ignore 规则对已跟踪文件**完全无效**；`git rm --cached` 会在同事下次 pull 时**删掉他们的文件** |
| 内容没变却显示"已修改" | `*.uvprojx` | `git add --renormalize` + `-text` 属性 | 前两种都碰不到它 |
| 只有仓库主人能判断 | `Release/*.bin` | 报告，不动 | 猜错就是丢发布固件 |

```bash
py -3 scripts/RepoHygiene.py -p <repo>                   # 只扫描,默认什么都不写
py -3 scripts/RepoHygiene.py -p <父目录> --each           # 扫描其下每个仓库
py -3 scripts/RepoHygiene.py -p <repo> --apply            # 全本地,仓库零改动
py -3 scripts/RepoHygiene.py -p <repo> --apply --shared   # 改 .gitignore/.gitattributes
py -3 scripts/RepoHygiene.py -p <repo> --unfreeze [<路径>...]
```

**写完用真 git 自检。** `.gitignore` 和 `.gitattributes` 都**不支持行尾注释** ——
`Objects/  # 输出目录` 会被 git 当成一个含空格和 `#` 的模式，匹配不到任何东西，而 git
从不报告"这条规则没匹配上任何文件"。所以自检问的不是"文件写出去了吗"，是"git 现在
真的忽略这些文件了吗"（`git check-ignore -z --stdin` / `git check-attr`）。`-z` 不能省：
非 ASCII 路径会被 C-quoting，对不上就把好规则误报成失效。

幻影修改那一类有两道闸：**只对"裸字节 == index blob"的文件加 `-text`**（判定必须用
`git hash-object --no-filters` —— 不加会套 filter，CRLF 工作区 + LF blob 算出相同
hash），否则加 `-text` 不是暴露幻影而是凭空造出真实差异，`--renormalize` 会把 CRLF
提交进去；以及 **`--renormalize` 若真 stage 了内容就立刻 `git reset` 撤回**。顺序上
先写属性再 renormalize，并重新探测幻影列表。

`*.uvprojx` **永不冻结** —— 它是真正的工程定义（源文件清单、编译宏、优化等级），改了
必须提交。

## 二、clangd-config —— 让编辑器看懂工程

从 Keil `.uvprojx`、IAR `.ewp`（ICCARM/ICCRL78/ICCRX/ICC430）或 CMake 工程生成
`.clangd` + `compile_commands.json`。

```bash
py -3 scripts/Proj2Clangd.py -p <dir> --detect-only   # 先看看认出了什么
py -3 scripts/Proj2Clangd.py -p <dir>                 # 自动分发到对应后端
```

**IAR 后端不猜内建宏，直接问编译器**：跑 `icc<arch>.exe --predef_macros` 取全部内建宏
写进生成头用 `-imacros` 挂上；char 符号性从 `__CHAR_MIN__` 反推；`--core` 靠试编译谈判
（设备头里有 `#error "... only"`，逐个候选编译，谁过用谁）。`.ewp` 里只存 IDE 下拉框
索引，静态映射必错。

三个后端都会检查**配置可发现性**：clangd 只搜源文件自身目录和祖先目录、**从不搜兄弟
目录**，而输出常落在 `Proj`/`build` 而源码在兄弟目录 —— 症状是同文件跳转正常、跨文件
跳转静默失效。`--fix-placement` 在源码的最近公共祖先写一个指针 `.clangd`。

生成后默认自检：每个 `-I` 是否存在、每个源文件是否存在、`directory` 是否为绝对路径、
`.clangd` 与 `compile_commands.json` 的 `-D` 是否一致（后两者不一致即 exit 3）。

项目搬家 / 换电脑 / 换 worktree 时走 re-anchor：外科手术式修正失效路径，不动注释与
手工增补，文件清单对不上时**拒绝改坏**。`scripts/build_exe.bat` 打出随仓库走的
`repo-keeper-reanchor.exe`。

## 三、clean-branch —— 只让代码进干净分支

两条长期分支有角色分工（一条纯代码对外，一条是带文档和工具的超集），历史又各自被
`filter-branch` 改写过。于是：**不能 merge**（hash 全不同，merge 会把整段历史当新东西
再引一遍），**也不能用 patch-id 求差**（历史改写不均匀时全是假结果）。只能按 subject
匹配 + 代码路径的真实 tree diff 来对账。

```bash
py -3 scripts/CleanBranch.py detect     # 同步点 + 待搬提交(附现成命令) + 漂移 + 越界文档
py -3 scripts/CleanBranch.py verify     # 四项不变量 → PASS/FAIL
py -3 scripts/PickToClean.py <commit>...  # 工作分支→干净分支
```

**`PickToClean.py` 的价值全在动手之前那两道闸**，它们各自对应一种「成功退出但结果是
错的」：**文档守卫** —— 干净分支的不变量是每条提交只含代码，而一条混了文档的提交照样
能 pick 成功，文档就静静进了对外分支的记录；所有 commit 一起预检，任一命中就整体拒绝。
**身份核对** —— `--reset-author` 拿的是那个 worktree 的 git config（这是对的，git config
才是真相源），但一份新检出可能根本没配过，于是提交悄悄签上个人身份落进对外分支而一切
看起来都成功了。配了 `identity.clean` 时脚本先核对，对不上就停下。是核对，不是覆盖。

刻意的永久分歧要填**两张表**：`expected_drift` 让 `verify` 不报 FAIL，`never_pick` 让
`detect` 不把肇事提交列成待办。只填前一张，`verify` 会变绿而 `detect` 仍在劝你把分歧
撤销掉。

## 配置

两层，因为其中一半（身份、文档 glob、受保护分支）在你所有仓库里是同一套，重复填既啰嗦
又会漂移：

| 层 | 路径 | 装什么 |
|---|---|---|
| 全局 | `~/.repo-keeper/defaults.toml` | 身份、文档 glob、受保护分支、扫描深度 |
| 项目 | `<仓库根>/.repo-keeper.local.toml` | 分支 ref、代码路径、白名单、刻意漂移、拉黑 hash |

合并语义：**表逐键深合并**（项目加一条 `expected_drift` 不会清空全局其余条目）、
**数组整体替换**（数组是一个值不是一个集合；拼接会让"我明明删了一项怎么还在"变成常见
困惑，而且继承来的列表将永远缩不了）、**标量项目优先**。

分层配置的错误是不可见的 —— 值来自错的那个文件，看起来跟来自对的文件一模一样。所以
每个脚本都有 `--explain`，逐键打印来源：

```bash
py -3 scripts/Keeper.py explain -p <仓库>
```

项目层放在**主检出**的根上，一份文件服务所有 linked worktree（每个 worktree 各存一份
就是它们开始漂移的起点），并由 ignore 规则挡在仓库之外。

`init` 会生成两份带注释的模板，但**必填键留空** —— 模板替你填一个分支 ref，就是替你
猜。

## 结构

```
.claude-plugin/plugin.json           插件清单
.claude-plugin/marketplace.json      可直接作为 marketplace 安装
skills/using-repo-keeper/SKILL.md    总入口:分派到下面三个
skills/repo-hygiene/SKILL.md         git 噪音治理
skills/clangd-config/SKILL.md        clangd 配置生成 + 校验
skills/clean-branch/SKILL.md         干净分支对账
scripts/Keeper.py                    总入口脚本:按正确顺序把 ①②③ 连起来
scripts/toolname.py                  工具名的唯一定义处(改名成本控制在一处)
scripts/local_config.py              两层 TOML 加载 + 合并 + 来源追踪 + 友好报错
scripts/RepoHygiene.py               ignore 规则 / skip-worktree 冻结 / 换行归一
scripts/CleanBranch.py               分支对账:detect / verify
scripts/PickToClean.py               工作分支→干净分支搬运(文档守卫 + 身份核对)
scripts/Proj2Clangd.py               识别工程类型并分发
scripts/k2c_common.py                共用:路径格式化 / .clangd 渲染 / 位置校验
scripts/Keil2Clangd.py               Keil 后端
scripts/Iar2Clangd.py                IAR 后端
scripts/Cmake2Clangd.py              CMake 后端
scripts/ReAnchor.py                  项目搬家后修正失效路径(仅 Keil)
scripts/tests/                       全套单测
```

## 测试

```bash
cd scripts && py -3 -m pytest tests/ -q
```

其中 `tests/test_no_secrets.py` 是一道**发布闸**：它扫全仓，拒绝让私人字符串跟着发出去。
分两半 —— 结构规则（家目录里的真实账号名、完整 git hash、`[[私人笔记链接]]`、真实邮箱）
写在测试里，因为模式本身不泄漏任何东西；而分支名、雇主、代号这类**只有你知道它敏感**
的词，读自 `~/.repo-keeper/audit-words.txt`，**故意不放进这个仓库** —— 把那张表写进
检查它的文件，就等于把要防的字符串发出去，闸门自己变成了泄漏点。这个文件不存在时那一半
跳过并说明原因。

## 来源与 License

Apache-2.0。clangd 那部分后端源自 [huiyi-li/keil2clangd](https://github.com/huiyi-li/keil2clangd)，
沿用其许可证；`repo-hygiene` 与 `clean-branch` 是新写的。本仓合并并取代了此前那个只做
clangd 配置的 `keil2clangd` 插件仓。
