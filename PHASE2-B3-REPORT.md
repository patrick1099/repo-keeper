# 第二阶段第 3 批交付报告（B3-prepare）

日期：2026-09-04
任务书：`task-secret-gate-phase2-batch3.md`（v2，558 行）
工作目录：`shared/plugins/repo-keeper`

> **本批的交付形态是 prepare，不是 activate。**
> `~/.githooks/` 和 `~/.repo-keeper/` 一个字节都没有被写过（§7 第 14 条有 mtime 对照）。
> `install-hooks --activate` 的代码已经写出来，但本批一次都没有执行过。

---

## 1. 交付了什么

| 文件 | 变化 | 说明 |
|---|---|---|
| `scripts/GateCheck.py` | 33436 -> 61303 字节 | 新增 `install-hooks` 与 `sync-exempt` 两个子命令 |
| `hooks/commit-msg` | 2048 -> 3021 字节 | 接力改成无条件；三段署名检查一字未动 |
| `hooks/_passthru` | 430 -> 1233 字节 | 同一套修法，按 `basename $0` 认槽位 |
| `hooks/commit-msg.bak-2026-09-04` | 新建 2048 字节 | 改前原字节备份（§4.3 第 1 步） |
| `scripts/tests/test_install.py` | 新建 | 29 条用例 |
| `hooks/pre-push` | **未改动** | 7857 字节，评审后已定稿（§4.0） |

`GateCheck.py` 新增的函数：`_norm_path` / `_sha256_bytes` / `_find_source_root` /
`_current_hooks_path` / `_plan_one` / `_install_plan` / `_BatchInstallError` /
`_BatchWriter` / `_source_commit` / `_build_manifest` / `_install_execute` /
`_resolve_target_dir` / `_report_check` / `_cmd_install_hooks` / `_gh_command` /
`_run_gh` / `_cmd_sync_exempt`。

`--check` / `--stage` / `--activate` 共用同一个 planner（`_install_plan`）。
**writer（`_BatchWriter`）实际只被 `--stage` 和 `--activate` 共用**——`--check`
在调到 `_install_execute` 之前就返回了，并没有走一个「被置成只算不写」的 writer。
任务书 §0.1 的字面要求是三模式共用同一个 writer，这里是一处偏离（见 §6.1）。
偏离的实质影响有限：`--check` 不做任何模拟，它报的就是 planner 算出来的计划，
所以「预演与真装漂移」这个要害没有发生；但断言不能照任务书的原话写。

---

## 2. 验收（§7 十五条）

### 2.1 测试

| # | 命令 | 结果 |
|---|---|---|
| 1 | `pytest scripts/tests/test_install.py -q` | `29 passed in 18.47s` |
| 2 | `pytest scripts/tests/test_gatecheck.py -q` | `51 passed in 38.78s` —— 存量 51 条一条没坏 |
| 3 | `pytest scripts/tests/test_hooks.py -q` | `30 passed in 57.76s` —— 存量 30 条一条没坏 |
| 4 | `pytest scripts/tests/ -q` 全量（无 `--ignore`） | `654 passed, 2 subtests passed in 733.39s (0:12:13)` |
| 5 | `pytest scripts/tests/test_no_secrets.py -q` | `13 passed in 7.03s` |

`test_install.py` 的 29 条按四组分布：

- `TestInstallHooksCheck`（4 条）：空目录报 `missing` 且不建任何东西、装好后报 `ok`
  且目标 mtime 不变、`--check` 与 `--stage` 的计划逐项相同、`core.hooksPath` 指向别处
  时报 `installed-but-inactive`。
- `TestInstallHooksStage`（10 条）：闭包与钩子一起装、目标不同则建备份、缺可执行位报
  `mode-wrong`、目标含 `\r` 报 `crlf`、compare-and-swap 在目标被改动时中止、
  写后校验失败则回滚、第二个文件失败要把第一个也回滚、临时文件保 `.py` 后缀、
  装进去的 `.py` 仍是密文、三模式一个都不给时退非零。
- `TestSyncExempt`（7 条）：只写 `PRIVATE`、`gh` 失败时文件一个字节不动、非 JSON 输出
  判失败、返回条数等于 `--limit` 当截断处理、`GH_HOST` 被钉死、写出的键能被 `arm` 认出、
  `--dry-run` 不写。
- `TestCommitMsgRelay`(5 条) 与 `TestPassthruRelay`(3 条)：署名拦截、干净放行、
  哨兵拿到完整参数且退出码透传、自我接力守卫、不在仓库里时 fail-closed。

### 2.2 第 6 条：`install-hooks --check` 对真实环境

`py -3 scripts/GateCheck.py install-hooks --check --json`（**不写任何字节**）：

```json
{
  "ok": true,
  "data": {
    "active_hooks_dir": "~/.githooks",
    "files": [
      { "name": "GateCheck.py",  "status": "missing", "target": "~/.repo-keeper/GateCheck.py" },
      { "name": "cli_common.py", "status": "missing", "target": "~/.repo-keeper/cli_common.py" },
      { "name": "secretscan.py", "status": "missing", "target": "~/.repo-keeper/secretscan.py" },
      { "name": "toolname.py",   "status": "missing", "target": "~/.repo-keeper/toolname.py" },
      { "name": "pre-push",      "status": "differs", "target": "~/.githooks/pre-push" },
      { "name": "commit-msg",    "status": "differs", "target": "~/.githooks/commit-msg" },
      { "name": "_passthru",     "status": "differs", "target": "~/.githooks/_passthru" }
    ],
    "missing_slots": [
      "pre-applypatch", "post-applypatch", "pre-receive", "update", "proc-receive",
      "post-receive", "post-update", "push-to-checkout", "pre-auto-gc",
      "sendemail-validate", "fsmonitor-watchman", "p4-pre-submit", "post-index-change"
    ]
  },
  "error": null,
  "meta": {}
}
```

（`target` 在实际输出里是展开后的绝对路径，此处为可读性缩写成 `~`；其余逐字。）

**读法**：4 个 `.py` 全部 `missing` —— `~/.repo-keeper/` 现在一个 `.py` 都没有，
所以**闸即使装了钩子也跑不起来**，这正是 §2.1 说的「4 个文件必须一起装」。
3 个钩子全部 `differs` —— `~/.githooks/` 里在岗的是 2026-08-13 那批旧版本。
缺 13 个槽位是 git 全部钩子名减去已有的 12 个。

### 2.3 第 7 条：`install-hooks --stage <临时目录>`

目标目录 `<系统临时目录>/b3stage-xxxxxxxx`，
`installed: 7`，`mode: "stage"`。落地结果：

| 文件 | 落地字节 | 与源逐字节相同 | manifest 一致 | mode | `\r` |
|---|---|---|---|---|---|
| `home/GateCheck.py` | 61303 | ✅ | ✅ `50e3d7293cb8` | 100644 | — |
| `home/cli_common.py` | 17551 | ✅ | ✅ `b14e6f26f85f` | 100644 | — |
| `home/secretscan.py` | 16149 | ✅ | ✅ `b964924a4281` | 100644 | — |
| `home/toolname.py` | 5819 | ✅ | ✅ `f5a80b097816` | 100644 | — |
| `hooks/pre-push` | 7857 | ✅ | ✅ `79159d888788` | 100755 | 0 |
| `hooks/commit-msg` | 3021 | ✅ | ✅ `2f1999812ddf` | 100755 | 0 |
| `hooks/_passthru` | 1233 | ✅ | ✅ `4adae336ba10` | 100755 | 0 |

（sha256 只贴前 12 位，避免触发发布闸的 `FULL_HASH` 规则。「manifest 一致」的含义是：
manifest 里记的 sha256 == 源文件的 sha256 == 落地文件的 sha256，三方相等。）

另外落地 `home/installed-manifest.json`（1784 字节，明文，`.json` 本就不加密）。

**`--check` 的幂等复检**：对同一个 stage 目录再跑一次 `--check`，4 个 `.py` 全部报
`ok`，3 个钩子报 `installed-but-inactive`。后者不是"没装好"——字节是对的，
只是那个目录不是当前的 `core.hooksPath`，这正是 §2.3 四维状态分类要区分的第四维。
任务书第 7 条写的是"断言这次报的是 `ok`"，实际得到的是更精确的两类，**不是缺陷**。

**但这一条只证明了 `--check` 幂等，没有证明「安装」幂等。**
评审后补跑：对同一个 stage 目录连跑两次 `--stage`，**第二次必定失败**
（`E_GATE_UNAVAILABLE`，已回滚）。详见 §6.2 —— 这是本批最重的一条缺陷，
而上面这条"幂等复检"恰好把它盖住了。

### 2.4 第 8 条：§6.4 canary 复跑

把 python 入口换成必退 97 且会落标记文件的 canary。**标记文件不存在 = python 从未被
启动**，比看进程树可靠（进程一闪即逝抓不住）。

| 场景 | exit | python 起过吗 | 本地钩子被接力 | stdin 逐字节原样 |
|---|---|---|---|---|
| 公司仓形状，无本地钩子 | 0 | **False** | —— | —— |
| 公司仓形状，有本地哨兵（哨兵退 41） | **41** | **False** | ✅ | ✅ |
| 对照组：github 目标 | 1 | **True** | —— | —— |

第一行证明公司路径零影响；第二行证明**接力确实发生了**（这是评审后新增的要求：
不能只证明"没起 python"，还得证明"本地钩子跑到了"），且退出码原样传播；
第三行是对照组，证明 canary 确实会被起到——否则前两行的 `False` 可能只是 canary 装错了。
对照组 stderr：`敏感词闸：武装判定失败（退出码 97），无法确认该不该扫描，拒绝本次 push。`

### 2.5 第 9 条：§4.3 前后对照

协议五步全部走到：改前备份到仓内 `hooks/commit-msg.bak-2026-09-04`；改前两条实跑；
只动末尾接力；改后原样重跑；再加接力验证。

改前 / 改后两份结果文件**逐字节相同**（各 398 字节，sha256 前 12 位同为
`e753f9e1efbf`）。内容：

```
=== 带署名的 message ===
exit=1
stdout(0):

stderr(290):

  提交被拒绝：commit 里出现 AI 署名。
  命中 Co-Authored-By 行： Co-Authored-By: <AI名> <bob@example.com>

  这是全局硬性规则（~/.githooks/commit-msg）：产出归用户，AI 不署名。
  去掉该行后重新提交。确需放行用 git commit --no-verify。

=== 干净 message ===
exit=0
stdout(0):

stderr(0):
```

**一处刻意的不逐字**：夹具里那个 AI 名字在报告里替换成了 `<AI名>`。
本仓硬约束是「任何 AI 的名字不许出现在任何地方，报告也不许」，
而这份输出恰好是在演示闸怎么拦它。逐字节相同这件事由两份文件的 sha256 相等来担保，
不依赖把那个词抄进报告。

接力验证（`commit-msg`）：哨兵拿到完整参数、退出码 7 原样传播、stdout 干净；
自我接力守卫命中时退 0 且 stderr 为空；不在仓库里时退 1（fail-closed）。
`_passthru` 同样三条，退出码 9 原样传播、参数 `alpha beta` 完整送达。

### 2.6 第 10 条：§4.4 激活影响清单

扫描范围：公司项目根目录下全部公司仓与 worktree、`treasury-vault`、
`shared/plugins/*`、`ai-room`、家目录下直接可见的 git 仓。每个仓用
`git rev-parse --path-format=absolute --git-common-dir` 定位真实钩子目录
（linked worktree 的 `.git` 是文件，必须解到 common dir），列出其中所有非 `.sample` 文件。

**结论：所有被扫到的仓，`.git/hooks/` 下都没有非 `.sample` 的钩子**，
全部是 `git init` 自带的 `*.sample`。所以 `--activate` 之后**不会有任何仓库本地钩子
从"被静默屏蔽"变成"开始运行"**——因为本来就一个都没有。

判据说明：Windows/NTFS 上 exec 位没有意义（`st_mode` 恒为 `0o666`，`os.chmod` 也带不上
`0111`），而 git-for-Windows 对 `hooks/` 下任何非 `.sample` 文件都会尝试执行，
所以清单以「非 `.sample` 文件」为口径，exec 位仅作参考。

**但有一条必须写进决策材料**：`~/.githooks/` 里除 `commit-msg` 外还有 10 个
`_passthru` 槽位副本（`applypatch-msg` / `post-checkout` / `post-commit` /
`post-merge` / `post-rewrite` / `pre-commit` / `pre-merge-commit` /
`prepare-commit-msg` / `pre-push` / `pre-rebase`），它们全是 430 字节的旧版，
**带着 §1.1 那个 `--git-path` 缺陷**。`--activate` 只替换 `pre-push` /
`commit-msg` / `_passthru` 三个文件，**不会**修好那 10 个槽位。
补槽位是行为变更，归 §2.8 / §8(c)，本批只记账。

### 2.7 第 11 条：Esafenet 复核

用 PowerShell（非白名单进程，看到的是盘上真实字节）读头 8 字节：

| 文件 | 头 8 字节 | 判定 |
|---|---|---|
| `scripts/GateCheck.py` | `e0a891e7d8f205ac` | 密文 ✅ |
| `scripts/tests/test_install.py`（**新建**） | `e0a891e7d8f205ac` | 密文 ✅ |
| `scripts/tests/test_hooks.py` | `e0a891e7d8f205ac` | 密文 ✅ |
| `scripts/tests/test_gatecheck.py` | `e0a891e7d8f205ac` | 密文 ✅ |
| `scripts/secretscan.py` / `cli_common.py` / `toolname.py` | `e0a891e7d8f205ac` | 密文 ✅ |
| `hooks/pre-push` / `commit-msg` / `_passthru` / `commit-msg.bak-*` | `23212f62696e2f73` | 明文（= `#!/bin/s`，与基线一致） |

`--stage` 装进临时目录的 4 个 `.py` **也逐个验过，全部 `e0a891e7d8f205ac`** ——
安装过程没有把密文写成明文。新建的 `test_install.py` 单独验过（只验"改动过的文件"
会漏掉新建的）。

**一个容易误判的现象，写在这里免得下一个人踩**：同一个 `.py`，PowerShell 看到
61303 字节，python 看到 57207 字节，差正好 **4096** —— 那是 Esafenet 的加密头。
两种视角看到两个大小是**正常的**，不是文件被截断。
`~/.repo-keeper/audit-words.txt` 同理（基线记 1188，PowerShell 看 5284）。

### 2.8 第 12 条：换行

用 `py -3` 读字节数 `\r`：`hooks/pre-push` 179 行 0 个 `\r`，
`hooks/commit-msg` 68 行 0 个，`hooks/_passthru` 24 行 0 个。三份钩子全是纯 LF。

`--stage` 落地的三份钩子同样 0 个 `\r`（见 2.3 表格）。

### 2.9 第 13 条：git 状态

`repo-keeper`：

```
 M hooks/_passthru
 M hooks/commit-msg
 M scripts/GateCheck.py
 M scripts/tests/test_gatecheck.py
?? hooks/commit-msg.bak-2026-09-04
?? hooks/pre-push
?? scripts/tests/test_hooks.py
?? scripts/tests/test_install.py
```

`treasury-vault`（含金库自身的无关改动）：

```
 M shared/plugins/repo-keeper/hooks/_passthru
 M shared/plugins/repo-keeper/hooks/commit-msg
 M shared/plugins/repo-keeper/scripts/GateCheck.py
 M shared/plugins/repo-keeper/scripts/tests/test_gatecheck.py
 M shared/skills/script-manager/INDEX.md
 M shared/skills/script-manager/scripts/contract-test/contract_test.py
?? .phase2-tmp/
?? shared/plugins/repo-keeper/PHASE2-B2-REPORT.md
?? shared/plugins/repo-keeper/hooks/commit-msg.bak-2026-09-04
?? shared/plugins/repo-keeper/hooks/pre-push
?? shared/plugins/repo-keeper/scripts/tests/test_hooks.py
?? shared/plugins/repo-keeper/scripts/tests/test_install.py
?? shared/skills/... （金库自身的无关改动，非本批产出）
?? task-secret-gate-phase2-batch3.md
```

两边 `git diff --cached --stat` 都是空的 —— **没有任何暂存、提交或 ref 改动**。
（`script-manager` 与 `skills` 下那几项是金库自身的无关改动，不是本批碰的。）

### 2.10 第 14 条：两个在岗目录一个字节没动

开工前（08:54）记的基线与收工后逐行对照，**完全一致**：

`~/.githooks/`（12 个文件）：`commit-msg` 2048 字节 `2026-08-13 20:09:11`，
其余 11 个（`_passthru` + 10 个槽位副本）各 430 字节 `2026-08-13 20:09:38`。

`~/.repo-keeper/`（3 个文件）：`audit-words.txt` `2026-08-12 14:24:23`、
`audit-words.txt.bak` `2026-08-10 17:08:39`、`defaults.toml` `2026-08-13 19:20:28`。

全部 mtime 停在 2026-08-10 ~ 08-13，**今天没有一个被写过**。
词表只记了"有多少字节"，**一条词都没抄进本报告**。

---

## 3. 第 15 条：人工复核要点

### 3.1 最容易出错的地方

1. **`install-hooks` 的 4 个 `.py` 必须一起装。** 只装 `GateCheck.py` 不装
   `secretscan.py`，表现是每次 push 都报 `E_GATE_UNAVAILABLE` 被拒 —— fail-closed
   兜住了不漏，但等于把机器锁死。`_BatchWriter` 的整批回滚就是防这个。
2. **原子写的临时文件必须保 `.py` 后缀。** 命名成 `GateCheck.py.tmp-xxxx` 会让临时
   文件落成明文，`os.replace` 再把明文搬到目标上，而 sha256 校验照样通过 ——
   校验的是解密后的内容，看不见加密态。用例
   `test_temp_file_keeps_py_suffix` 与 `test_staged_py_keeps_esafenet_encryption` 钉这条。
3. **接力必须是无条件的。** 第 2 批就是在这里栽的（见 `PHASE2-B2-REPORT.md` §17）：
   接力只挂在"检查通过"那条路径上，另外三条放行路径静默吞掉本地钩子。
   本批的 `commit-msg` 和 `_passthru` 按同一口径改，放行路径只有一条，统一走接力。

### 3.2 我做了判断而任务书没明说的地方

1. **`--stage` 的幂等复检报的是两类状态而不是统一的 `ok`**（见 2.3）。
   我判定这是正确行为而非缺陷，因为 `installed-but-inactive` 恰好是 §2.3 要求的
   第四维。如果你认为 `--check` 应当在非活动目录上也报 `ok`，那是另一个设计决定。
2. **§4.3 的"逐字贴出"做了一处遮蔽**（见 2.5），理由与担保方式已写在那一节。
3. **激活影响清单的判据用「非 `.sample` 文件」而不是 exec 位**（见 2.6），
   因为 Windows 上 exec 位不成立。这个口径比按 exec 位更保守（会多列不会少列）。

---

## 4. 开着的问题（本批没动，留给你拍板）

### 4.1 `--stage DIR` 不自建子目录

`--stage DIR` 要求 `DIR/hooks` 和 `DIR/home` **事先存在**，否则退
`E_GATE_UNAVAILABLE`。对一个新鲜的 `tempfile.mkdtemp()` 直接跑会失败，必须先手工
`mkdir` 两个子目录。

对 `--activate` 来说，「目标目录不存在就拒绝」是正确的 fail-closed；
但 `--stage` 的目标本来就是一次性临时目录，要求它预先存在纯属摩擦。
**现状如实记录，本批没有改。**

### 4.2 manifest 的 `source_commit` 是误导性的

`installed-manifest.json` 里记的是 `"source_commit": "3d1a4be"`，但工作区有大量未提交
改动，实际装进去的字节根本不是那个 commit 的内容。也就是说这个字段现在**不能用来回答
"装的是哪一版"**，而那正是它存在的理由。

可选的修法（本批一个都没做）：记工作区脏不脏、或改记每个文件的 sha256（manifest 里
已经有了）、或干脆去掉这个字段。**留给你定。**

### 4.3 那 10 个 `_passthru` 槽位副本

见 2.6 末段。`--activate` 不会修好它们，它们会继续带着 `--git-path` 缺陷在岗。
补不补属于 §8(c) 的一部分。

---

## 5. 决策点状态

- **(a) `_passthru` 与 `commit-msg` 的接力 —— 已定，本批已执行**：源码修好、测通，
  `~/.githooks/` 里在岗的那两份一个字节没动。
- **(b) 豁免 TTL 过期 —— 已定，本批遵守**：过期即视为未豁免照常扫描，
  `sync-exempt` 做完也没有把联网加回钩子里。
- **(c) 真正装到 `~/.githooks/` —— 仍待你拍板，本批按"不装"做**。
  `--activate` 一次都没执行过。决策材料见 2.2（真实环境现状）、2.6（激活影响清单）、
  4.3（那 10 个槽位）。

---

## 6. 评审后补正（2026-09-04）：四条部署阻断点

报告写完后把它连同方案、任务书、三个钩子源码和 `GateCheck.py` 一起送去做了一轮
独立评审。评审结论是 **不要执行 `--activate`**，并点出四条阻断点。
四条我都逐条实测复核过，**全部属实**，其中两条是本报告前面写错了（已就地改掉）。

### 6.1 `--check` 并没有走 writer（本报告原先的断言是错的）

`_cmd_install_hooks` 里 `if args.check: return _report_check(...)`，在调到
`_install_execute` 之前就返回了。所以任务书 §0.1 要求的「三模式共用同一个 writer，
差别只是 writer 被置成只算不写」**没有照字面实现**：共用的是 planner，writer 只被
`--stage` / `--activate` 共用。

实质影响有限（`--check` 不做任何模拟，报的就是 planner 的计划，不存在预演与真装漂移），
但本报告 §1 原先照任务书原话写成了"共用同一个 writer"，那是错的，已改。

### 6.2 装第二次必定失败 —— 安装器不是幂等的

`_install_execute` 写 manifest 时 `prev_sha` 硬编码成 `None`：

```python
writer.write(home_target / "installed-manifest.json", ..., None)
```

而 `_BatchWriter.write` 里，目标已存在且 `prev_sha is None` 就判定「目标在 plan
之后才出现」并中止整批。于是只要 `installed-manifest.json` 已经在，
下一次 `--stage` / `--activate` **必然在最后一步失败并整批回滚**。

实测（同一个 stage 目录连跑两次）：

```
--- 第一次 ---  ok=True  installed=7
--- 第二次 ---  ok=false
  "code": "E_GATE_UNAVAILABLE",
  "message": "安装失败, 已回滚: 目标在 plan 之后才出现, 拒绝覆盖: <stage>\\home\\installed-manifest.json",
  "details": {"stage": "rolled_back"}
```

**这意味着 `--activate` 一辈子只能成功执行一次**，之后任何升级、重装、修复都装不进去。
本报告 §2.3 原先那条"幂等复检"只测了 `--check`，恰好把这条盖住了，已就地补正。

顺带：`installed-manifest.json` 在整个 `GateCheck.py` 里**只被写、从未被读**
（全文只有第 879 行一处）。方案 §D5 要它承担版本状态与闭包完整性校验，目前还没接上。

### 6.3 回滚不覆盖真实 I/O 异常

`_install_execute` 只 `except _BatchInstallError`。而写入路径上的
`shutil.copy2` / `open` / `fsync` / `os.replace` / `os.chmod` / `read_bytes`
抛出的 `OSError`（磁盘满、权限、文件被占用、路径过长）**会越过回滚直接冒出去**，
留下装了一半的现场。

对 `--stage` 无所谓（临时目录），对 `--activate` 是真风险：半套闭包 = 每次 push 报
`E_GATE_UNAVAILABLE` 被拒；半套钩子 = 更难说清的混合态。

### 6.4 装的槽位数不够 —— 激活后仍有 22 个槽位被静默屏蔽

`INSTALL_HOOK_FILES = ["pre-push", "commit-msg", "_passthru"]`，只装 3 个文件。
而 `GIT_HOOK_NAMES` 有 **24** 个槽位。

本报告 §2.6 原先说"`--activate` 不会修好那 10 个槽位"，**这个数字不准**：
那 10 个副本里有一个是 `pre-push`，它会被专用闸替换掉。准确的账是：

| | 数量 | 激活后的状态 |
|---|---|---|
| 专用闸替换 | 2（`pre-push` / `commit-msg`） | ✅ 修好 |
| 旧缺陷副本继续在岗 | **9** | ❌ 带 `--git-path` 缺陷 |
| 槽位仍然缺失 | **13** | ❌ 全局钩子不存在，本地钩子照常跑（这一类反而没被屏蔽） |
| 合计仍不正确的槽位 | **22** | |

还有一条容易漏的：**修好的 `_passthru` 模板本身不叫任何一个 git 钩子名，git 永远不会
调用它**。它只是被复制成各槽位名之后才起作用。所以"装了修好的 `_passthru`"这件事，
在不补槽位的前提下对运行时行为**零影响**。

评审的判断是：这 22 个槽位应当纳入同一次真实激活，而不是先激活残缺版、以后再补。
理由是「眼下没有受害者」恰恰说明现在补代价最低——不会唤醒任何现有本地钩子，
行为冲击最小；而推迟的代价是，将来某个仓一旦新增本地钩子，它会从未运行过，
git 操作照常成功、没有任何痕迹，正是本方案要消灭的那种失效模式。

### 6.5 `--activate` 可以「装成功但没生效」

当前 `_cmd_install_hooks` 不校验 `core.hooksPath` 是否等于安装目标。
在没配 `core.hooksPath`、或配到别处的机器上，`--activate` 仍会返回成功——
字节全对、manifest 全对，而 git 根本不看那个目录。
状态分类里已经有 `installed-but-inactive` 这一维，但它只用于报告，没有用作 activate 的前置条件。

---

## 7. 结论：现在不具备激活条件

本批作为 **prepare** 是合格的：扫描核心、两个新子命令、两个钩子的接力修复、
29 条新用例、全量 654 passed、两个在岗目录一个字节没动。

但 §6 的四条在装到全机之前必须先处理。它们的共同点是：
**失败时的表现要么是全机 push 被锁死，要么是命令和测试都显示成功而闸实际没在跑。**
后者正是这个方案从头到尾要消灭的那一类。

激活次序（等上述问题解决后）：先把完整分发闭包提交成一个干净的本地 commit；
再用真实在岗目录的镜像做一次 shadow stage，覆盖「4 个 `.py` 缺失 + 旧钩子替换 +
补齐槽位 + manifest 已存在」这个真实混合状态；然后单次事务激活，
**`commit-msg` 放在最后切换**（让旧的署名闸尽可能久地保持在岗）；
切换后立刻跑实际安装路径上的四条 canary，而不是留观察期——静默失效等不出来。

---

## 8. 修复记录（2026-09-07）：§6 五条逐条改掉

§6 的五条全部实现了修复。下面逐条说改了什么、以及**改错了会怎么表现**——
这个闸的失效是静默的，"错了会怎么表现"比"为什么对"更值得写下来。

### 8.1 幂等：manifest 也走 compare-and-swap

`_install_execute` 开头先算出 manifest 目标当前的真实 sha（不存在则 `None`），
写的时候把它当 `prev_sha` 传进去，不再硬编码 `None`。

- 改错的表现：装第二次报 `E_GATE_UNAVAILABLE` 整批回滚——**响亮**，不静默。
- 用例：`test_stage_twice_both_succeed` / `test_stage_three_times_still_succeeds`；
  另加 `test_tampered_manifest_aborts_batch` 钉死 CAS 本身没被顺手削掉。

### 8.2 回滚覆盖真实 I/O 异常

`except _BatchInstallError` 放宽成 `except Exception`。`copy2` / `open` / `fsync` /
`os.replace` / `os.chmod` 抛的 `OSError` 现在一律触发整批回滚，再包成
`E_GATE_UNAVAILABLE`；回滚本身失败仍单独报 `rollback_failed`。

- 改错的表现：留下装了一半的闭包，每次 push 被拒——**响亮**。
- 用例：`test_oserror_mid_batch_rolls_back`（第 2 次 `os.replace` 抛 ENOSPC）、
  `test_oserror_on_backup_rolls_back`（备份阶段抛 EACCES），均断言两个目标目录
  事后为空 / 原样。

### 8.3 补槽位：铺 19 个，另外 3 个**故意留空**

`_install_plan` 现在把 `hooks/_passthru` 逐字节铺进 `PASSTHRU_SLOTS`。
条目数从 7 涨到 26（4 个 `.py` + 3 个分发钩子 + 19 个槽位副本）。

§6.4 说的是"22 个槽位"。**实际铺 19 个**，剩下 3 个列进 `PASSTHRU_SKIP_SLOTS`
不铺，这是本次唯一一处偏离评审建议的地方，理由是它们的退出码语义不一样：

| 槽位 | "退 0" 在这里的含义 |
|---|---|
| `push-to-checkout` | git 认定**工作区已由钩子更新完毕**，不再自己更新 |
| `proc-receive` | 要说 packet 协议；只退 0 不说话等于给出空答复 |
| `fsmonitor-watchman` | 退 0 且无输出 = **"没有文件变化"** |

铺一个只会退 0 的壳，在这三处制造的正是本方案要消灭的那类静默错误（工作区静默
不同步、索引静默陈旧）。而这三处"槽位缺失"本身是 fail-loud 的：git 直接报错，
或退回默认的拒绝行为。所以留空才是安全态，写死在常量旁边的注释里。

- 改错的表现：这三处若照铺，失效是**静默**的（工作区/索引悄悄不对）；
  留空则是响亮的。方向选的是响亮那边。
- 顺带：本报告 §6.4 那条"修好的 `_passthru` 模板自己不叫任何 git 钩子名，
  git 永远不会调它"现在有了行为用例——
  `test_slot_copy_relays_under_its_own_slot_name` 把装好的 `pre-commit` 槽位副本
  真跑一遍，断言本地哨兵钩子的退出码 7 原样透传。
- 用例另有：全部 19 个槽位逐字节等于源、3 个 skip 槽位事后不存在、
  两个专用闸不是 `_passthru` 的副本；并先钉死列表长度，防列表为空时循环空转通过。

### 8.4 `source_commit`：改判分发闭包，而不是整仓

新增 `_source_closure_state(source_root)`：只对分发闭包那 7 个路径跑
`git status --porcelain`，回答"这 7 个文件是否都已跟踪、且工作区与暂存区都等于
HEAD"。**只看闭包不看整仓**——堵死整仓会把日常迭代堵死，而 `source_commit`
要回答的本来也只是"装进去的字节是哪一版"。

- manifest 新增 `source_dirty` / `source_dirty_files` / `source_dirty_reason`。
- `--stage` 允许脏，只如实记 `source_dirty: true`；`--activate` 闭包脏就拒绝。
- `git status` 跑不起来 -> `reason` 非空 -> 按脏处理（fail-closed）。
- **贴着写入再验一次**：`_install_execute(require_clean=True)` 在真正落盘前重跑
  一次闭包判定，堵住"plan 读字节"与"真正落盘"之间源仓被改动的窗口。
- 改错的表现：`source_commit` 继续答非所问——**静默**。所以这条做了两层
  （前置检查 + 写入时复检），变异检验里把两层同时拿掉才会有第三条用例转红。
- 用例：`TestSourceClosureState` 6 条（干净 / 改过 / 未跟踪 / **闭包之外的脏被忽略**
  / 非仓库判定不了 / manifest 带上状态）+ `TestActivatePreconditions` 里的
  拒绝路径与写入时复检。

### 8.5 `--activate` 先校验 `core.hooksPath`

`--activate` 现在要求 `core.hooksPath` 规范化后**等于安装目标**，否则拒绝并把
两个值都打出来。没配、或配到别处，都装不进去。

- 改错的表现：字节全对、manifest 全对，而 git 根本不看那个目录——**最静默的一种**。
- 用例：未设置 / 指向别处两条，均断言退非 0 且目标目录事后为空。

### 8.6 `--stage` 自建它自己的两个子目录（甲）

`--stage DIR` 现在会建 `DIR/hooks` 与 `DIR/home`，但 **`DIR` 本身仍要求先存在**；
`--check` 与 `--activate` 分支里一个 `mkdir` 都没有。

- 改错的表现：`--activate` 打错路径时造出一套"字节全对但 git 根本不看"的目录还报
  成功——**静默**。所以 mkdir 只写在 stage 这一个分支里，并用两条用例分别钉死
  `--check` 与 `--activate` 事后不留下任何目录。

### 8.7 清单不再只写不读（§D5）

`--check` 现在读回 `installed-manifest.json`，在文本与 JSON 两路都报
`installed_at` / `source_commit` / `source_dirty` / 文件数；读不动就报
`parse_error` 而不是崩。同时报当前分发闭包的干净度。

### 8.8 验证

- **全量**：`683 passed, 2 subtests passed in 977.21s`，退 0（原 654，新增 29 条，
  全在 `scripts/tests/test_install.py`：29 -> 58）。发布闸在同一次全量里。
- **变异检验**：把五条修复逐一改回缺陷态，确认对应用例真的转红——

  | 改回缺陷态 | 结果 |
  |---|---|
  | manifest `prev_sha` 硬编码 `None` | 2 failed |
  | 只接 `_BatchInstallError` | 2 failed |
  | `_install_plan` 不铺槽位 | 2 failed |
  | 去掉 `--stage` 的 mkdir | 1 failed |
  | 去掉写入时的闭包复检 | 1 failed |
  | 前置检查与写入复检**同时**去掉 | 3 failed |

  头一轮我自己写坏过两条变异（`[] or [...]` 被真值短路吃掉、闭包前置检查被写入时
  复检兜住），都是变异本身无效而非用例失效；修正后如上。这一轮还顺手抓出
  `test_all_slots_installed_byte_identical` 在列表为空时会空转通过，已加长度守卫。
- **真实环境 `--check`**：26 个条目（14 missing / 12 differs），`missing_slots` 13，
  闭包报脏并逐条列出 4 个文件，`installed_manifest` 为 `null`。一个字节没写。
- **两个在岗目录仍未被动**：`~/.githooks/` 12 个文件、`~/.repo-keeper/` 3 个文件，
  大小与 mtime 全部停在 2026-08-10 ~ 08-13。
- **Esafenet 密文态**：改过的两个 `.py` 头字节仍是 `e0 a8 91 e7 d8 f2 05 ac`。

### 8.9 仍然没做的事

- **`--activate` 这个子命令一次都没执行过**，连临时目录都没有。`require_clean=True`
  那条写入路径是直接驱动 `_install_execute` 覆盖的（`test_require_clean_passes_when_closure_clean`
  往临时目录真写了一遍），**但"`--activate` 端到端跑通"这件事仍未验证过**。
- 因此 §7 那句结论只往前挪了一格：五条阻断点没有了，**分发闭包仍然是脏的**
  （这 7 个文件还没提交），所以 `--activate` 此刻仍会被自己的前置检查拒绝。
  下一步是把闭包提交成一个干净的本地 commit，然后按 §7 的次序走 shadow stage。
