# repo-keeper

给 Keil / IAR 固件仓库用的管家。装它只为一件事：让 `git status` 里只剩你自己写的代码。

嵌入式工程天生往仓库里吐东西，编译中间件、IDE 的窗口布局、clangd 的索引配置，待提交列表
长期是几十行噪音里埋着两行真改动。repo-keeper 把这些噪音按类清掉，顺手把 clangd 配好，
并且默认一个字节都不写进仓库，同事那边毫无感知。

Apache-2.0 · Python 3.11+ 纯标准库 · Claude Code 插件，也可以当独立脚本用

## 先看它干什么

```console
$ py -3 scripts/RepoHygiene.py -p <你的仓库>

模式: 本地 —— 只写 .git/info/ 与 .git/index,一个字节都不进仓库,同事无感

[1] ignore 规则能解决的 —— 未跟踪的生成物
  7 个文件/目录将被新规则挡住:
    - Code/App/.clangd
    - Code/App/Proj/compile_commands.json
    - Code/App/Proj/MeterApp.uvguix.dev
    ...

[2] ignore 规则解决不了的 —— 已跟踪、但只有本机在改
  10 个文件建议 skip-worktree 冻结:
    - Code/App/Proj/MeterApp.uvoptx
        Keil 用户选项:当前选中哪个 target、断点、窗口位置
    - Code/App/Proj/RTE/_Debug/RTE_Components.h
        Keil 每次打开工程都会重写,内容只跟工程名/target 名走
    ...
  文件继续留在仓库里,同事 pull 不受任何影响;只是本机改动不再上报。
  其中 3 个此刻就有未提交改动 —— 冻结后这些改动会从 git status 消失(盘上内容不变)

[3] 两者都解决不了的 —— 内容没变却显示「已修改」
  3 个文件的工作区内容与仓库内容逐字节相同,是 core.autocrlf 的换行折腾
  修法: git add --renormalize(不会 stage 任何内容) + 标 -text 防复发

[5] 仓库自带 .gitignore 的体检(只读 —— 本地模式不碰它)
  有效规则行: 183   已被本工具覆盖的冗余行: 97   指向已不存在路径的死行: 65
  !! 危险规则: 3 —— 本地模式不动它们,但你该知道它们在那儿
    - CMakeLists.txt    <- 构建脚本是源码,不该被忽略
    - *.0    <- 含义模糊,极易误伤(任何以 .0 结尾的文件)
    - *.py    <- 会静默忽略你以后写的每一个 Python 脚本

以上只是扫描,什么都没写。要执行(默认全走本机,不碰仓库文件):
  --apply             三样都做      (加 --dry-run 先预演)
  --shared            改成写仓库里的 .gitignore / .gitattributes
```

（真实运行结果，只把工程名换成了通用名。那三条危险规则是那个仓库里真有的。）

那五类对应**五种互不通用的修法**。选错了不是效果差一点，而是根本不生效，或者把同事的
文件删了：

| 噪音 | 修法 | 用错会怎样 |
|---|---|---|
| [1] 未跟踪的生成物 | ignore 规则 | — |
| [2] 已跟踪、只有本机改 | `git update-index --skip-worktree` | ignore 规则对已跟踪文件完全无效；`git rm --cached` 会在同事下次 pull 时删掉他们的文件 |
| [3] 内容没变却显示已修改 | `git add --renormalize` + `-text` 属性 | 前两种都碰不到它 |
| [4] 只有仓库主人能判断 | 报告，不动 | 猜错就是丢发布固件 |

`*.uvprojx` 永不冻结：它是真正的工程定义（源文件清单、编译宏、优化等级），改了必须提交。

## 安装

Claude Code 插件：

```
/plugin marketplace add patrick1099/repo-keeper
/plugin install repo-keeper@repo-keeper
```

装完只需记住 `using-repo-keeper` 一个名字，它自己分派到下面三个 skill。

当独立脚本用：

```bash
git clone https://github.com/patrick1099/repo-keeper
py -3 repo-keeper/scripts/Keeper.py init -p <你的仓库>
```

`init` 一条命令走完：判断当前分支 → 生成两层配置模板 → 治理 git 噪音 → 生成 clangd 配置
→ 报告分支对账状态。退出码 `0` 就绪、`1` 有需要你决定的事、`2` 出错。

## 四个环节

| 环节 | 做什么 | skill |
|---|---|---|
| ① 落脚 | 不在共享分支上干活，开 worktree | `using-repo-keeper` |
| ② 除噪 | 让 `git status` 只剩代码改动 | `repo-hygiene` |
| ③ 索引 | 生成并自检 `.clangd` + `compile_commands.json` | `clangd-config` |
| ④ 对账 | 只把代码提交搬进干净分支 | `clean-branch` |

①②③ 是机械的，`Keeper.py` 按固定顺序连起来跑，顺序本身承重（[为什么](docs/design.md#环节顺序)）。
④ 要人现场判断冲突和迁移方向，所以只做路由。

## 凭什么用它

**默认一个字节都不进仓库。** 改 `.gitignore` 会把一个人的整理动作变成所有同事都要 review
的提交，所以每种修法都有一份只在本份 clone 生效的形式，而且默认走的就是它：

| 共享写法 | 本地写法（默认） |
|---|---|
| `.gitignore` | `.git/info/exclude` |
| `.gitattributes` | `.git/info/attributes` |
| skip-worktree | 本来就是本地的（标记在 `.git/index`） |

代价如实说：这三样都不会被 clone、不会被 push，重新检出要重跑；而且仓库自己的 `.gitignore`
优先级高于 `.git/info/exclude`，被仓库规则挡住的文件本地放不出来，`!xxx` 在这里打不过。
撞上就如实报告，不写一条静默失效的规则。真要动共享文件加 `--shared`。

**写完用真 git 自检，不信自己写出去了。** `.gitignore` 不支持行尾注释，`Objects/  # 输出目录`
会被 git 当成一个含空格和 `#` 的模式，匹配不到任何文件，而 git 从不报告「这条规则没匹配上
任何东西」。所以自检问的是 `git check-ignore` / `git check-attr` 现在到底忽不忽略，不是文件
写出去了没有（[几个必须踩对的细节](docs/design.md#自检)）。

**有歧义一律非零退出。** 工程/target 多选而没指定、必填配置键缺失、worktree 放哪，全都停下
来问。因为猜错了会安静地跑在错的东西上，并且看起来一切正常：猜错的 target 生成一份宏全不对
的 `.clangd`，猜错的分支 ref 让整轮对账跑在错的分支上。

## clangd 配置

从 Keil `.uvprojx`、IAR `.ewp`（ICCARM / ICCRL78 / ICCRX / ICC430）或 CMake 工程生成
`.clangd` + `compile_commands.json`。

```bash
py -3 scripts/Proj2Clangd.py -p <dir> --detect-only   # 先看看认出了什么
py -3 scripts/Proj2Clangd.py -p <dir>                 # 自动分发到对应后端
```

IAR 后端不猜内建宏，直接跑 `icc<arch>.exe --predef_macros` 问编译器；`--core` 靠试编译谈判。
三个后端都会检查配置可发现性，因为 clangd 只搜源文件自身目录和祖先目录、从不搜兄弟目录，
而输出常落在 `Proj`/`build` 而源码在兄弟目录，症状是同文件跳转正常、跨文件跳转静默失效。
`--fix-placement` 在源码的最近公共祖先写一个指针 `.clangd`。

项目搬家 / 换电脑 / 换 worktree 时走 re-anchor，外科手术式修正失效路径，不动注释与手工增补，
文件清单对不上时拒绝改坏。`scripts/build_exe.bat` 打出随仓库走的 `repo-keeper-reanchor.exe`。

细节见 [clangd 后端](docs/design.md#clangd-后端)。

## 干净分支对账

适用于两条长期分支有角色分工（一条纯代码对外，一条是带文档和工具的超集）、历史又各自被
`filter-branch` 改写过的仓库。这种情况不能 merge，也不能用 patch-id 求差，只能按 subject
匹配 + 代码路径的真实 tree diff 来对账（[为什么](docs/design.md#分支对账)）。

```bash
py -3 scripts/CleanBranch.py detect       # 同步点 + 待搬提交(附现成命令) + 漂移 + 越界文档
py -3 scripts/CleanBranch.py verify       # 四项不变量 → PASS/FAIL
py -3 scripts/PickToClean.py <commit>...  # 工作分支 → 干净分支
```

`PickToClean.py` 的价值全在动手之前那两道闸，它们各自对应一种「成功退出但结果是错的」：
**文档守卫**（一条混了文档的提交照样能 pick 成功，文档就静静进了对外分支的记录）和
**身份核对**（`--reset-author` 拿的是当前 worktree 的 git config，而一份新检出可能根本没配过，
于是提交悄悄签上个人身份落进对外分支）。两者都是所有 commit 一起预检，任一命中整体拒绝。

## 配置

两层，因为其中一半（身份、文档 glob、受保护分支）在你所有仓库里是同一套：

| 层 | 路径 | 装什么 |
|---|---|---|
| 全局 | `~/.repo-keeper/defaults.toml` | 身份、文档 glob、受保护分支、扫描深度 |
| 项目 | `<仓库根>/.repo-keeper.local.toml` | 分支 ref、代码路径、白名单、刻意漂移、拉黑 hash |

合并语义：表逐键深合并、数组整体替换、标量项目优先。分层配置的错误是不可见的，值来自错的
那个文件看起来跟来自对的文件一模一样，所以每个脚本都有 `--explain` 逐键打印来源：

```bash
py -3 scripts/Keeper.py explain -p <仓库>
```

项目层放在主检出的根上，一份文件服务所有 linked worktree。`init` 生成的模板必填键留空，
因为模板替你填一个分支 ref 就是替你猜。

## 结构

```
skills/          using-repo-keeper(总入口) / repo-hygiene / clangd-config / clean-branch
scripts/         Keeper.py(总入口) + RepoHygiene / CleanBranch / PickToClean
                 Proj2Clangd(分发) + Keil2Clangd / Iar2Clangd / Cmake2Clangd / ReAnchor
                 local_config.py(两层 TOML 加载 + 来源追踪)
scripts/tests/   全套单测
```

```bash
cd scripts && py -3 -m pytest tests/ -q
```

其中 `tests/test_no_secrets.py` 是一道发布闸，扫全仓拒绝让私人字符串跟着发出去。词表分两半：
结构规则（真实账号名、完整 git hash、真实邮箱）写在测试里，因为模式本身不泄漏任何东西；
分支名、雇主、代号这类只有你知道它敏感的词读自 `<private-audit-wordlist>`，故意不放进
这个仓库，否则闸门自己就是泄漏点。该文件不存在时那一半跳过并说明原因。

## 来源与 License

Apache-2.0。clangd 后端源自 [huiyi-li/keil2clangd](https://github.com/huiyi-li/keil2clangd)，
沿用其许可证；`repo-hygiene` 与 `clean-branch` 是新写的。本仓合并并取代了此前那个只做 clangd
配置的 `keil2clangd` 插件仓。
