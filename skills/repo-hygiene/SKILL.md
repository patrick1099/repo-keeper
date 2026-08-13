---
name: repo-hygiene
description: Make `git status` in a firmware repo show code changes only, WITHOUT touching any file the team shares — ignore rules go to .git/info/exclude, already-tracked IDE/user-state files get skip-worktree, and files reported modified whose bytes are identical get renormalized. Use when a Keil/IAR repo's pending-changes list is full of build output, *.uvoptx, *.uvguix, RTE_Components.h, .clangd / compile_commands.json or ~$ Office lock files; when a file keeps showing as modified but `git diff` is empty; when a hand-maintained .gitignore has grown into dozens of literal dead paths; or when the user asks to stop tracking changes to a file WITHOUT deleting it from the repo.
---

# Repo hygiene — 让待提交列表只剩代码

四类噪音，**三种不同的修法**。选错了不是效果差，是根本不起作用，或者把同事的文件删了。

| 噪音 | 例子 | 正确修法 | 用错会怎样 |
|---|---|---|---|
| 未跟踪的生成物 | `Objects/`、`*.map`、`.clangd`、`~$说明书.docx` | ignore 规则 | — |
| **已跟踪**、只有本机在改 | `*.uvoptx`、`RTE_Components.h`、IAR `*.pbd` | `git update-index --skip-worktree` | 写 ignore 规则**完全无效**;`git rm --cached` 会在同事下次 pull 时**删掉他们的文件** |
| 内容没变却显示"已修改" | `*.uvprojx` | `git add --renormalize` + `-text` 属性 | 前两种都碰不到它 |
| 只有仓库主人能判断 | `Release/Useful/*.bin` | 报告,不动 | 猜错就是丢发布固件 |

**第二行是这个 skill 存在的主要理由。** 用户说"停止跟踪"时,九成指的是 skip-worktree(文件留在仓库里,只是本机改动不上报),不是 `git rm --cached`(从仓库删除)。**动手前先确认是哪一个。**

## 默认只写本机 —— 这是承重设计,不是保守

`.gitignore` 和 `.gitattributes` 是**从工作分支拉下来的共享文件**。改它们,就把一个人的
整理动作变成了所有同事都要 review、要解冲突、要一起承担的提交。所以每种修法都有一个
**每份 clone 各自生效**的形式,默认走的就是它:

| 共享写法 | 本地写法(默认) |
|---|---|
| `.gitignore` | `.git/info/exclude` |
| `.gitattributes` | `.git/info/attributes` |
| skip-worktree | 本来就是本地的(标记存在 `.git/index`) |

**代价要如实说,不能藏**:这三样都不会被 clone、不会被 push,重新检出一份就没了;
而且**仓库自己的 `.gitignore` 优先级高于 `.git/info/exclude`** —— 被仓库规则挡住的文件,
本地放不出来,`!xxx` 例外在这里打不过(实测)。撞上这种情况工具会**如实报告**,
而不是写一条静默失效的规则。

真要整理共享文件时加 `--shared`,并且**先跟同事打招呼**。

## 用法

```powershell
# 只扫描,什么都不写 —— 默认行为,先看报告
py -3 "${CLAUDE_PLUGIN_ROOT}/scripts/RepoHygiene.py" -p <repo>

# 扫描 <父目录> 下的每一个仓库
py -3 "${CLAUDE_PLUGIN_ROOT}/scripts/RepoHygiene.py" -p <父目录> --each

# 预演真实写入(走同一条写路径,只是不落盘)
py -3 "${CLAUDE_PLUGIN_ROOT}/scripts/RepoHygiene.py" -p <repo> --apply --dry-run

# 执行(全本地),也可以只挑一样做
py -3 "${CLAUDE_PLUGIN_ROOT}/scripts/RepoHygiene.py" -p <repo> --apply
py -3 "${CLAUDE_PLUGIN_ROOT}/scripts/RepoHygiene.py" -p <repo> --write-ignore
py -3 "${CLAUDE_PLUGIN_ROOT}/scripts/RepoHygiene.py" -p <repo> --freeze
py -3 "${CLAUDE_PLUGIN_ROOT}/scripts/RepoHygiene.py" -p <repo> --renormalize

# 改共享文件(会产生同事要 review 的提交)
py -3 "${CLAUDE_PLUGIN_ROOT}/scripts/RepoHygiene.py" -p <repo> --apply --shared

# 解冻(不给路径 = 全部解冻)
py -3 "${CLAUDE_PLUGIN_ROOT}/scripts/RepoHygiene.py" -p <repo> --unfreeze [<路径>...]
```

`--write-gitignore` 是 `--write-ignore --shared` 的旧写法,保留兼容。

报告分五段,`[1]`~`[3]` 是三种修法各自负责的部分,`[4]` 是工具拒绝替人决定的,`[5]` 是现有
`.gitignore` 的体检(本地模式下**只读**)。**先把报告给用户看,再问要不要执行。**

## 报告里必须转述给用户的三件事

1. **`[4] 只有你能判断的`** —— 逐条念给用户听,不要替他决定。典型的是
   `Release/Useful/*.bin`:可能是故意提交的发布固件,也可能是编译残留。
2. **`[5]` 里的危险规则** —— `*.py`、`*.0`、`CMakeLists.txt` 这类。本地模式下工具**不动它们**,
   只是让用户知道它们在那儿;要真删得走 `--shared`,而**剔除一条别人当初特意加的规则,
   必须让用户点头**。
3. **`[2]` 里"此刻就有未提交改动"的那些** —— 冻结后这些改动会从 `git status` 消失。
   盘上内容一个字节不变,但用户要知道自己看不见它们了。

## skip-worktree:三件必须一起说的事

1. **标记存在 `.git/index` 里,是每个 clone 各自的。** 不会推给同事,也不会被别的
   clone 或 worktree 继承 —— 换一份检出就要重跑一次。同一个项目有几份检出,就跑几次。
2. **同事真改了被冻结的文件时,`git pull` 会报**
   `Your local changes to the following files would be overwritten`。
   解法:`--unfreeze <文件>` → 拉 → 重新 `--freeze`。
3. **想主动提交某个冻结文件的改动**:`--unfreeze` 它 → `git add`/`commit` → 再 `--freeze`。

## 写出来的规则块长什么样

`.git/info/exclude` 和 `.gitignore` 用同一套渲染。三段固定顺序,重复运行不会长胖:

```
<生成块>            按用途分段,每段/每组上方一行注释说明"为什么"
# ==== 手工增补 ====   工具永不改动;旧文件里无规则覆盖的行原样搬进这里
# ==== 例外 ====       !repo-keeper-reanchor.exe,必须在最后
```

- **理由必须独占一行。`.gitignore` 不支持行尾注释** —— `Objects/  # 输出目录` 会被当成
  一个含空格和 `#` 的模式,而 git **从不报告"这条规则没匹配到任何文件"**。这个 bug 曾让
  74 条规则全部失效而一切看起来正常。所以写完必须用真 git 复验:
  `git check-ignore -z --stdin`(**`-z` 不能省** —— 非 ASCII 路径会被 C-quoting 成
  `"Doc/~$\350\257\264..."`,对不上就把好规则误报成失效,而这些仓库里到处是中文路径)。
- **例外必须在最后**:git 按**最后一条**匹配的规则决定。`!foo.exe` 写在手工区的
  `*.exe` 上面会被它悄悄抵消。
- **在文件末尾追加的行会被救回手工区** —— 追加在 EOF 是最自然的动作,而 EOF 在例外块
  下面;直接丢掉就是静默数据丢失。
- `--shared` 时旧 `.gitignore` 写到 `.gitignore.bak`。

## 幻影修改([3])的两道安全闸

“内容逐字节相同却报已修改”看着无害,处理错了却会造出**一个碰遍所有工程文件的垃圾提交**:

1. **只对"裸字节 == index blob"的文件加 `-text`。** 对"盘上 CRLF、仓库 LF"的文件
   (`core.autocrlf` 下的正常状态)加 `-text`,不是暴露幻影,是**凭空造出一个真实差异**,
   接着 `--renormalize` 会把 CRLF 提交进去。这道闸在真实仓库上立刻拦下了 4 个。
   判定必须用 `git hash-object --no-filters` —— **不加 `--no-filters` 会套 filter**,
   CRLF 工作区 + LF blob 算出来的 hash 相同。
2. **`git add --renormalize` 若真 stage 了内容,说明判定错了** → 立刻 `git reset` 撤回,
   不能只打印警告。

顺序也是承重的:**先写属性,再 renormalize,且 renormalize 时重新探测幻影列表** ——
写 `-text` 本身会改变归一化方式,可能把干净文件变成新的幻影。

## `[0]` 暂存区:最后一个规则还有用的时刻

报告第一段看的是**已经 `git add`、下一个 commit 就落进仓库**的文件。它排在最前面，因为
这是时间窗最紧的一类：`git add` 已经**覆盖**了 ignore 规则，而一旦提交，规则对那条路径
**永远**无效，能用的只剩 `skip-worktree` 或者把它从所有人的检出里删掉。

判据跟别处一致：本该被某条规则挡住的、`FREEZE_RULES` 里的本机状态文件、`REVIEW_RULES`
里的、扩展名属于 `AUTO_IGNORE_SUFFIXES` 的。**只报告，绝不替你 unstage** —— 别人一次
刻意的 `git add -f` 是有理由的，而工具看不见那个理由。

撤下来是 `git restore --staged <文件>`（不是 `git rm --cached`，那是给已提交文件的，
会在同事下次 pull 时删掉他们的）。

## 规则不够用时:让 AI 直接补

看完 `[0]` 和 `[1]` 的剩余项，确认某一类永远不该进仓库，就地加规则：

```bash
py -3 RepoHygiene.py -p <仓库> --add-rule "<模式>" --why "<理由>" [--rule-layer global]
py -3 RepoHygiene.py -p <仓库> --write-ignore      # 加完要重写才生效
```

规则存在自己的文件里，`--add-rule` 会**整体重写**它，所以别在里面手写别的东西：

```
~/.repo-keeper/extra-ignore.toml          跨项目复用
<主检出>/.repo-keeper.extra-ignore.toml   本仓库(已被 ignore 挡住,不进仓库)
```

跟两层 TOML 配置的区别：那边数组**整体替换**，这里两层**拼接** —— 项目加一条，并没有
对全局那些说过任何话。所以它们不共用一个文件。

两道闸，对人和对 AI 一视同仁：**理由必填**（说不出理由的行就是这个文件失控的起点）；
**`DANGEROUS_LINES` 里的模式一律拒绝**。AI 替你加一条 `*.py` 跟人手写它是同一个陷阱，
只是少了一双眼睛盯着 —— 那条规则加的当天看着无害，三个月后静默吞掉你写的脚本。

## 两条铁律

- **ignore 规则对已跟踪的文件毫无作用。** 报告 `[5]` 末尾的"规则被已跟踪文件架空"
  就是在说这个:规则写了也不生效,得靠 `[2]` 的冻结。别在这上面浪费一轮。
- **不要自己拼 `git rm --cached`。** 它会在同事下次 pull 时删掉他们本地的文件。用户
  明确要求"从仓库里删掉"时才用,而且要先说清后果。

## 改规则表

规则都在 `scripts/RepoHygiene.py` 顶部的表里,每条带一句理由:

| 表 | 管什么 |
|---|---|
| `IGNORE_SECTIONS` | 写进 ignore 文件的规则,`(段落标题, [(为什么, [模式...])])` |
| `NEGATIONS` | 必须跟着仓库走的例外,渲染在文件最后 |
| `FREEZE_RULES` | 匹配到的**已跟踪**文件建议 skip-worktree |
| `REVIEW_RULES` | 匹配到就交给用户判断,工具不动 |
| `DANGEROUS_LINES` | 旧 `.gitignore` 里遇到就报警的行，同时也是 `--add-rule` 的黑名单 |
| `AUTO_IGNORE_SUFFIXES` | 内置规则没覆盖时，允许**现场生成**规则的扩展名 |
| `GITATTRIBUTES_BINARYISH` | 标 `-text`,防幻影修改复发 |

`AUTO_IGNORE_SUFFIXES` 是封闭表，**每一项都必须是不可能是源码的扩展名**——这就是允许
工具自己造规则的全部安全论证。`*.log` 不可能吞掉你几个月后写的代码，`*.py` 可以。往里
加条目前先问这一句。另外：仓库如果**跟踪着**同扩展名的文件，就不生成（说明在这个仓库
它是刻意保留的），生成的是通配符而不是字面路径（字面清单正是这工具要治的病）。

加规则时**顺手加一条测试**:`scripts/tests/test_repo_hygiene.py`。
`test_no_rule_carries_an_inline_comment` 挡住行尾注释,
`test_git_itself_confirms_the_rules_bite` 用真 git 验证规则真的生效。

`*.uvprojx` **永远不要**进 `FREEZE_RULES` —— 它是真正的工程定义(源文件清单、编译宏、
优化等级),改了必须提交。它显示"已修改"却没有 diff,是换行问题,归 `[3]` 管。

worktree 场景注意:`info/exclude` 是从 **`--git-common-dir`** 读的,
写到 worktree 私有的 git dir 里完全无效。
