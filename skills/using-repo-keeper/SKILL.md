---
name: using-repo-keeper
description: The one entry point for repo-keeper — use this whenever setting up, tidying or reconciling an embedded firmware repo, and it will route to the right stage itself. Triggers - opening a repo for the first time or on a new machine, "git status is full of build junk", "stop tracking this file without deleting it", clangd cannot jump between files, a file shows as modified with an empty diff, or two long-lived branches have drifted and must not be merged. Also use when the user names repo-keeper without saying which part.
---

# repo-keeper —— 仓库管家

**这是唯一需要记住的名字。** 另外三个 skill 是它的四个环节里的三个，由这里分派，
不需要点名调用。

主线只有一句：**保住一条干净分支，只让代码提交进去。**

| 环节 | 做什么 | 归谁 |
|---|---|---|
| ① 落脚 | 不在共享分支上干活 → 开 worktree | 本 skill |
| ② 除噪 | 让 `git status` 只剩代码改动 | `repo-hygiene` |
| ③ 索引 | 生成并自检 `.clangd` + `compile_commands.json` | `clangd-config` |
| ④ 对账 | 只把**代码**提交搬进干净分支 | `clean-branch` |

①②③ 是机械的，**由一个脚本按正确顺序连起来跑**，不靠这份文档嘱咐你去调三个 skill
（嘱咐是可以被跳过的，而且跳过了没有任何痕迹）。④ 要人现场判断，所以只做路由。

顺序本身是承重的：ignore 规则要在 clangd 之前落地，否则新生成的 `.clangd` 立刻变成
一条未跟踪改动；而这两件都得在**离开共享分支之后**做，不然是在不该动的检出里动手。

## 第一步永远是这条

```bash
py -3 "${CLAUDE_PLUGIN_ROOT}/scripts/Keeper.py" init -p <仓库路径>
```

它会：判断当前分支 → 生成两层配置模板 → 跑 repo-hygiene → 跑 clangd-config →
报告 clean-branch 的状态。

退出码：**0 = 就绪，1 = 有需要用户决定的事，2 = 出错。**
拿到 1 不要自作主张往下推 —— 把它列出来的那几条念给用户听。

其他开关：

```bash
py -3 "${CLAUDE_PLUGIN_ROOT}/scripts/Keeper.py" init -p <仓库> --dry-run     # 走真实路径不落盘
py -3 "${CLAUDE_PLUGIN_ROOT}/scripts/Keeper.py" init -p <仓库> --no-apply    # 只扫描不写
py -3 "${CLAUDE_PLUGIN_ROOT}/scripts/Keeper.py" init -p <仓库> --worktree "<路径>" --branch <新分支名>
py -3 "${CLAUDE_PLUGIN_ROOT}/scripts/Keeper.py" explain -p <仓库>            # 每个配置值来自哪一层
```

## init 会自己做的 / 一定不做的

**会做**（每一条都是**每份 clone 各自生效、且可撤销**的）：

- 生成两层配置模板（新文件，git 看不见）
- ignore 规则 → `.git/info/exclude`
- IDE 状态文件 → `skip-worktree` 冻结（撤销：`RepoHygiene.py --unfreeze`）
- clangd 配置（生成物，已被上面的规则挡住）

**一定不做**：

- **不碰 `.gitignore` / `.gitattributes`** —— 那是从工作分支拉下来的共享文件，改它们
  会把一个人的整理动作变成所有同事都要 review 的提交。init 从不传 `--shared`。
- **不猜 worktree 放哪** —— 在受保护分支上时它停下来问，因为放错地方会在别人磁盘上
  留垃圾。
- **不替用户判断 `[4]`** —— 一个被提交进仓库的 `.bin` 可能是故意发布的固件，也可能是
  编译残留，只有仓库主人知道。
- **不动分支** —— cherry-pick、迁移、解冲突全部留给 ④。

## 什么时候转给哪个 skill

`init` 跑完之后，按用户接下来要干什么分派：

- **还在为噪音烦** —— `git status` 里仍有该消失的东西，或者要「停止跟踪某个文件但
  不能删掉它」→ `repo-hygiene`。（这两件事修法完全不同：前者是 ignore 规则，后者是
  `skip-worktree`，用 `git rm --cached` 会在同事下次 pull 时删掉他们的文件。）
- **跳转/补全不对** —— 换了机器、项目搬了家、要挑 target、IAR 的宏不对
  → `clangd-config`。
- **两条分支漂了** —— 有改动只落在一边、文档跑到了代码分支上、要把提交搬过去而**不能
  merge** → `clean-branch`。

## 配置是两层的

```
~/.repo-keeper/defaults.toml        跨项目复用:身份、文档 glob、受保护分支、剥离脚本
<仓库根>/.repo-keeper.local.toml    项目特有:分支 ref、代码路径、白名单、刻意漂移
```

项目层覆盖全局层。**表逐键深合并、数组整体替换、标量项目优先。**
分层配置的错误是不可见的（值来自错的那个文件，看起来跟来自对的文件一模一样），
所以任何时候拿不准都跑 `explain`，它逐键打印来源。

项目层放在**主检出**的根上，一份文件服务所有 linked worktree —— 每个 worktree 各存
一份就是它们开始漂移的起点。它不进仓库，靠 `.git/info/exclude` 挡住（不是 `.gitignore`）。

**必填键缺了就非零退出，不猜默认值。** 猜错的分支 ref 会让整轮跑在错的分支上，而且
一切看起来正常。

## 汇报给用户时

- 先给 `init` 的结论和「需要你决定」那几条，**不要**把整段 repo-hygiene 报告原样倒出来。
- `[4] 只有你能判断的` 逐条念，别代答。
- 冻结了此刻有未提交改动的文件时要说一句：盘上内容一个字节没变，但那些改动从
  `git status` 里消失了。
- 本地模式的代价要说清：`.git/info/` 和 `.git/index` 都**不会被 clone、不会被 push**，
  换一份检出要重跑。
