#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_clean_branch.py — CleanBranch.py / PickToClean.py 的 stdlib unittest 测试。

运行:
    py -3 -m unittest discover -s <this dir> -v

=== 设计要点 ===

1. 无 pytest，只用 stdlib `unittest`。所有 Python 调用走 `py -3`，绝不用裸 `python`
   （Windows 商店别名会挂起；且 `py -3` = 本机 3.14.6）。

2. **不依赖任何真实的剥离脚本**。真脚本因人而异，某些机器上还可能被透明加密而读不成
   文本。测试自带一份等价的**假剥离脚本源码**(STRIP_SRC)，fixture 把它写进临时目录，
   git 的 clean filter 指向它。契约：stdin bytes → stdout bytes，exit 0，**只操作字节永不解码**
   （固件源码常是 GB2312，任何 decode 都会破坏字节），幂等。

3. `_make_repo()` 造一个镜像真实布局的临时 git 仓库：`extensions.worktreeConfig=true`、
   common dir 的 `info/attributes` 挂 `*.c/*.h filter=privclean`、共享 config
   `filter.privclean.clean = py -3 "<假脚本>"`、两个 linked worktree (clean/work)、
   仅 work worktree 用 `--worktree filter.privclean.clean=cat` 翻成直通、两 worktree 用不同
   提交身份。返回 `FakeRepo` 对象（含各路径 + `commit()`/`git()` 助手），供后续 Task 复用。

4. **让 CleanBranch.py 指向临时仓库的办法（monkeypatch）**：
   分支引用是模块级名字 `CLEAN_REF / WORK_REF / MAIN_REF`（正常由 `configure()` 从两层
   配置填入），worktree 解析结果缓存在模块级 `_WT_CACHE`（惰性、import 无副作用）。
   `BaseCleanBranchTest.setUp` 里：
     - 直接覆盖三个 REF 为临时仓库的分支名，**不走 configure()**——测试要的是逻辑，
       不是配置加载；配置加载自己有 test_local_config.py 管；
     - 直接把 `cb._WT_CACHE` 塞成 `(clean_wt, work_wt)`，绕过 `_resolve_worktrees()`
       （它依赖 `os.getcwd()` 在真仓库内，测试里不成立）。
   `tearDown` 恢复原值并把 `_WT_CACHE` 复位为 None，避免测试间状态泄漏。

5. **CRLF**：fixture 里 `core.autocrlf=false`，否则 git 的换行转换会破坏字节级 blob 断言。

6. **Windows 清理**：git 对象文件常为只读，`shutil.rmtree` 直删会 PermissionError；
   `_rm_readonly` 处理器先 chmod 再重试。
"""
import io
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import CleanBranch as cb  # noqa: E402  (需先插 sys.path)
import PickToClean  # noqa: E402  (同目录，复用上面的 sys.path)
import local_config  # noqa: E402


# ---------------------------------------------------------------------------
# 假剥离脚本源码：写进临时目录，由 git clean filter 以 `py -3 <此文件>` 调用。
# 纯字节扫描，永不 decode。marker = '?'(0x3F) 或全角'？'(0xA3 0xBF) 紧跟空白
# (空格 0x20 / Tab 0x09 / 全角空格 0xA1 0xA1)。只在注释**开头**紧跟 marker 时才算私人注释：
#   - `//? ...` 行注释：整行仅此注释则删整行，否则只删行尾注释；
#   - `/*? ... */` 块注释（可跨行）：整块删除。
# 因为只在 `//`/`/*` 之后紧邻处判定 marker，代码里的三元 `a ? b : c` 天然不受影响。
# GB2312 双字节尾字节范围 0xA1-0xFE，永不为 0x3F，故字节级判定对汉字安全。
#
# ⚠ 与真脚本的已知差异（2026-07-09 实测）：真脚本是**上下文无关**的——只要出现
# `//`/`/*` + marker 就剥，不管在哪。故真脚本会破坏：
#     char *s = "//? x";      -> char *s = "        (字符串被截断!)
#     /* see //? x */         -> /* see             (块注释未闭合!)
#     // http://? no          -> // http:
# 本假脚本认得字符串字面量与已在注释内的位置，比真脚本更保守。
# 当前固件里这三种形态均为 0 次命中，故无实际损坏；差异对本测试套件无影响
# （CleanBranch 只要求 strip 确定性，两侧用同一个 strip 比较）。
# ---------------------------------------------------------------------------
STRIP_SRC = r'''
import sys, os
try:
    import msvcrt
    msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
    msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
except Exception:
    pass

WS1 = (0x20, 0x09)  # 空格, Tab


def _is_ws(data, i):
    n = len(data)
    if i >= n:
        return False
    if data[i] in WS1:
        return True
    if data[i] == 0xA1 and i + 1 < n and data[i + 1] == 0xA1:  # 全角空格
        return True
    return False


def _is_marker(data, i):
    n = len(data)
    if i >= n:
        return False
    if data[i] == 0x3F:  # '?'
        return _is_ws(data, i + 1)
    if data[i] == 0xA3 and i + 1 < n and data[i + 1] == 0xBF:  # 全角 '？'
        return _is_ws(data, i + 2)
    return False


def strip_private(data):
    out = bytearray()
    i = 0
    n = len(data)
    line_start = 0   # out 中当前行起点
    only_ws = True   # 当前行到目前为止只吐了空白(代码字节)
    while i < n:
        b = data[i]
        # 字符串 / 字符字面量：整体照抄，跳过其中的 // /* 与 ?
        if b == 0x22 or b == 0x27:
            q = b
            out.append(b); i += 1; only_ws = False
            while i < n:
                c = data[i]; out.append(c); i += 1
                if c == 0x5C and i < n:      # 反斜杠转义
                    out.append(data[i]); i += 1
                elif c == q:
                    break
            continue
        # 行注释 //
        if b == 0x2F and i + 1 < n and data[i + 1] == 0x2F:
            if _is_marker(data, i + 2):
                eol = data.find(b"\n", i)
                if eol == -1:
                    eol = n
                if only_ws:                  # 整行仅此私人注释 -> 删整行(含换行)
                    del out[line_start:]
                    i = eol + 1 if eol < n else n
                else:                        # 行尾私人注释 -> 连同前导空白删掉
                    while len(out) > line_start and out[-1] in WS1:
                        out.pop()
                    if eol > i and data[eol - 1] == 0x0D:   # CRLF: \r 在注释区内，补回
                        out.append(0x0D)
                    i = eol
                continue
            else:                            # 普通行注释：照抄到行尾
                eol = data.find(b"\n", i)
                if eol == -1:
                    eol = n
                out += data[i:eol]; i = eol; only_ws = False
                continue
        # 块注释 /* */
        if b == 0x2F and i + 1 < n and data[i + 1] == 0x2A:
            end = data.find(b"*/", i + 2)
            cend = n if end == -1 else end + 2
            if _is_marker(data, i + 2):      # 私人块注释：整块删除
                i = cend
            else:
                out += data[i:cend]; i = cend; only_ws = False
            continue
        # 换行
        if b == 0x0A:
            out.append(b); i += 1; line_start = len(out); only_ws = True
            continue
        # 普通字节
        out.append(b); i += 1
        if b not in (0x20, 0x09, 0x0D):
            only_ws = False
    return bytes(out)


def main():
    data = sys.stdin.buffer.read()
    sys.stdout.buffer.write(strip_private(data))
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


# ---------------------------------------------------------------------------
# 底层助手
# ---------------------------------------------------------------------------
class _Result:
    """轻量结果对象：.returncode / .stdout / .stderr。"""
    __slots__ = ("returncode", "stdout", "stderr")

    def __init__(self, rc, out, err):
        self.returncode, self.stdout, self.stderr = rc, out, err


def _git(cwd, *args, check=True, binary=False, stdin=None):
    """在 cwd 跑 git。始终以字节模式执行（stdin 可为 bytes）；
    binary=False 时把 stdout/stderr 解码为 str，binary=True 时保留 bytes（用于 blob 断言）。"""
    r = subprocess.run(
        ["git", "-C", cwd, "-c", "core.quotepath=false", *args],
        input=stdin,
        capture_output=True,
    )
    if binary:
        out, err = r.stdout, r.stderr
        err_text = err.decode("utf-8", "replace")
    else:
        out = r.stdout.decode("utf-8", "replace")
        err = err_text = r.stderr.decode("utf-8", "replace")
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed ({r.returncode}):\n{err_text}")
    return _Result(r.returncode, out, err)


def _rm_readonly(func, path, exc):
    """rmtree 处理器：git 对象只读 -> chmod 可写后重试。兼容 onexc/onerror 三参签名。"""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except Exception:
        pass


def _rmtree(path):
    if not os.path.isdir(path):
        return
    try:
        shutil.rmtree(path, onexc=_rm_readonly)      # Python 3.12+
    except TypeError:
        shutil.rmtree(path, onerror=_rm_readonly)    # 旧签名兜底
    except Exception:
        pass


def _write_strip_script(dirpath):
    """把假剥离脚本写进 dirpath，返回绝对路径。"""
    p = os.path.join(dirpath, "strip_private_comments.py")
    with open(p, "wb") as f:
        f.write(STRIP_SRC.encode("utf-8"))
    return os.path.abspath(p)


def _run_strip(script_path, data):
    """按 git clean filter 的方式跑假脚本：stdin bytes -> stdout bytes。"""
    r = subprocess.run(["py", "-3", script_path], input=data, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError("strip script failed:\n" + r.stderr.decode("utf-8", "replace"))
    return r.stdout


# ---------------------------------------------------------------------------
# fixture：临时 git 仓库 + 两个 worktree
# ---------------------------------------------------------------------------
class FakeRepo:
    """
    镜像真实「干净/工作」双分支布局的临时仓库。属性：
      root          — 主 worktree（默认分支）目录
      clean_wt      — 干净分支 linked worktree 目录（继承共享 filter = 剥离）
      work_wt         — 工作分支 linked worktree 目录（--worktree filter = cat 直通）
      strip_script  — 假剥离脚本绝对路径
      main_branch / clean_branch / work_branch — 分支名
    方法：
      git(wt, *args, ...)               — 在某 worktree 跑 git
      commit(which, files, message)     — 向干净/工作分支提交 {relpath: bytes}，返回新 HEAD hash
      cleanup()                         — 递归删除临时目录（处理只读）
    """
    MAIN = "main"
    CLEAN = "clean"
    WORK = "work"

    # 身份：干净分支用共享 config，工作分支用 worktree-local override，后续作者测试可区分
    SHARED_NAME, SHARED_EMAIL = "clean-bot", "clean@example.com"
    WORK_NAME, WORK_EMAIL = "work-bot", "work@example.com"

    def __init__(self, base):
        self._base = base
        self.root = os.path.join(base, "repo")
        self.clean_wt = os.path.join(base, "wt-clean")
        self.work_wt = os.path.join(base, "wt-work")
        self.main_branch = self.MAIN
        self.clean_branch = self.CLEAN
        self.work_branch = self.WORK
        self.strip_script = None
        self._build()

    # -- 构建 --
    def _build(self):
        os.makedirs(self.root, exist_ok=True)
        self.strip_script = _write_strip_script(self._base)
        strip_cmd = 'py -3 "%s"' % self.strip_script.replace("\\", "/")

        # 1. init（显式默认分支名，避免 master/main 歧义）
        _git(self.root, "init", "-b", self.main_branch)

        # 2. 共享 config
        _git(self.root, "config", "extensions.worktreeConfig", "true")
        _git(self.root, "config", "core.autocrlf", "false")
        _git(self.root, "config", "user.name", self.SHARED_NAME)
        _git(self.root, "config", "user.email", self.SHARED_EMAIL)
        _git(self.root, "config", "filter.privclean.clean", strip_cmd)

        # 3. common dir 的 info/attributes（三 worktree 共享，无法按 worktree 区分）
        common = _git(self.root, "rev-parse", "--path-format=absolute",
                      "--git-common-dir").stdout.strip()
        info = os.path.join(common, "info")
        os.makedirs(info, exist_ok=True)
        with open(os.path.join(info, "attributes"), "wb") as f:
            f.write(b"*.c filter=privclean\n*.h filter=privclean\n")

        # 4. 初始提交（无 marker，剥离不影响）+ 两分支
        with open(os.path.join(self.root, "base.c"), "wb") as f:
            f.write(b"int base;\n")
        _git(self.root, "add", "base.c")
        _git(self.root, "commit", "-m", "base")
        _git(self.root, "branch", self.clean_branch)
        _git(self.root, "branch", self.work_branch)

        # 5. 两个 linked worktree
        _git(self.root, "worktree", "add", self.clean_wt, self.clean_branch)
        _git(self.root, "worktree", "add", self.work_wt, self.work_branch)

        # 6. 仅 work worktree：过滤器直通 + 独立身份
        _git(self.work_wt, "config", "--worktree", "filter.privclean.clean", "cat")
        _git(self.work_wt, "config", "--worktree", "user.name", self.WORK_NAME)
        _git(self.work_wt, "config", "--worktree", "user.email", self.WORK_EMAIL)

    # -- 助手 --
    def _wt(self, which):
        return {self.CLEAN: self.clean_wt, self.WORK: self.work_wt,
                self.MAIN: self.root}.get(which, which)

    def git(self, which, *args, **kw):
        return _git(self._wt(which), *args, **kw)

    def commit(self, which, files, message):
        """向干净/工作分支(或 worktree 路径)提交 {relpath: bytes}。经过该 worktree 的 filter。"""
        wt = self._wt(which)
        paths = []
        for rel, data in files.items():
            p = os.path.join(wt, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(p), exist_ok=True) if os.path.dirname(p) else None
            with open(p, "wb") as f:
                f.write(data)
            paths.append(rel)
        _git(wt, "add", "--", *paths)
        _git(wt, "commit", "-m", message)
        return _git(wt, "rev-parse", "HEAD").stdout.strip()

    def cleanup(self):
        _rmtree(self._base)


def _make_repo():
    """造临时 FakeRepo。调用方负责 repo.cleanup()（或 addCleanup）。"""
    base = tempfile.mkdtemp(prefix="cleanbranch-test-")
    try:
        return FakeRepo(base)
    except Exception:
        _rmtree(base)
        raise


# ---------------------------------------------------------------------------
# 基类：建 fixture + 把 CleanBranch.py 指向临时仓库（monkeypatch）
# ---------------------------------------------------------------------------
#: 正常由 configure() 从两层配置填入；测试直接赋值，跑的是逻辑不是配置加载。
#: 注意 CODE_PATHS 必须非空且与用例里的文件路径对得上——为空时 drift 的 diff
#: 恒为空，一整组用例会**假通过**。
FIXTURE_SETTINGS = {
    "CODE_PATHS": ["App", "Boot", "drv", "bsp", ".clangd"],
    "DOC_GLOBS": ["*.md", "*.csv", "*.txt", "*.docx", "docs/"],
    "ALLOW": [],
    "EXPECTED_DRIFT": {},
    "NEVER_PICK": {},
    "SCAN": 60,
}

#: PickToClean.main 要一个 cfg 才能核对提交身份。空配置 = 没声明身份 = 不核对，
#: 这正是大多数用例想要的；身份闸自己另有用例。
EMPTY_CFG = local_config.Config(
    {}, {}, {local_config.GLOBAL: None, local_config.PROJECT: None})


class BaseCleanBranchTest(unittest.TestCase):
    _PATCHED = ("CLEAN_REF", "WORK_REF", "MAIN_REF", "_WT_CACHE",
                *FIXTURE_SETTINGS)

    def setUp(self):
        self.repo = _make_repo()
        self.addCleanup(self.repo.cleanup)
        # 保存并覆盖 CleanBranch 模块状态
        self._orig = {name: getattr(cb, name) for name in self._PATCHED}
        cb.CLEAN_REF = self.repo.clean_branch
        cb.WORK_REF = self.repo.work_branch
        cb.MAIN_REF = self.repo.main_branch
        cb._WT_CACHE = (self.repo.clean_wt, self.repo.work_wt)
        for name, value in FIXTURE_SETTINGS.items():
            setattr(cb, name, value)

    def tearDown(self):
        for name, value in self._orig.items():
            setattr(cb, name, value)
        cb._WT_CACHE = None  # 复位缓存，防止测试间泄漏


# ---------------------------------------------------------------------------
# 用例组 1：fixture 冒烟 + filter 接线验证
# ---------------------------------------------------------------------------
class FixtureTests(BaseCleanBranchTest):
    def test_fixture_builds(self):
        # 两个 worktree 存在
        self.assertTrue(os.path.isdir(self.repo.clean_wt))
        self.assertTrue(os.path.isdir(self.repo.work_wt))
        # work worktree filter = cat
        w = self.repo.git(FakeRepo.WORK, "config", "--worktree", "--get",
                          "filter.privclean.clean", check=False)
        self.assertEqual(w.returncode, 0)
        self.assertEqual(w.stdout.strip(), "cat")
        # clean worktree 无 worktree-local 覆盖（--worktree --get 找不到 -> 非 0 且空）
        c = self.repo.git(FakeRepo.CLEAN, "config", "--worktree", "--get",
                          "filter.privclean.clean", check=False)
        self.assertNotEqual(c.returncode, 0)
        self.assertEqual(c.stdout.strip(), "")

    def test_filter_wiring_work_keeps_clean_strips(self):
        """本方案立身之本：同一内容经 hash-object，工作分支保留注释、干净分支剥离。"""
        content = b"int a; //? note\n"

        qh = self.repo.git(FakeRepo.WORK, "hash-object", "-w",
                           "--path=x.c", "--stdin", stdin=content).stdout.strip()
        qblob = self.repo.git(FakeRepo.WORK, "cat-file", "-p", qh, binary=True).stdout
        self.assertIn(b"//? note", qblob)
        self.assertEqual(qblob, content)

        xh = self.repo.git(FakeRepo.CLEAN, "hash-object", "-w",
                           "--path=x.c", "--stdin", stdin=content).stdout.strip()
        xblob = self.repo.git(FakeRepo.CLEAN, "cat-file", "-p", xh, binary=True).stdout
        self.assertNotIn(b"//? note", xblob)
        self.assertIn(b"int a;", xblob)
        self.assertNotEqual(qh, xh)  # 两 blob hash 必不同


# ---------------------------------------------------------------------------
# 用例组 2：假剥离脚本行为（直接跑脚本，与 git 调用方式一致）
# ---------------------------------------------------------------------------
class FakeStripScriptTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="strip-test-")
        self.addCleanup(_rmtree, self._dir)
        self.script = _write_strip_script(self._dir)

    def strip(self, data):
        return _run_strip(self.script, data)

    def test_strips_line_comment_whole_line(self):
        self.assertEqual(self.strip(b"//? note\n"), b"")

    def test_strips_line_comment_trailing(self):
        self.assertEqual(self.strip(b"int a; //? note\n"), b"int a;\n")

    def test_preserves_crlf_trailing(self):
        # 真剥离脚本实测保留 CRLF（\r 落在被删的注释区内，必须补回）；假脚本须一致
        self.assertEqual(self.strip(b"int a; //? note\r\nint b;\r\n"),
                         b"int a;\r\nint b;\r\n")

    def test_preserves_crlf_whole_line(self):
        self.assertEqual(self.strip(b"//? note\r\nint b;\r\n"), b"int b;\r\n")

    def test_strips_block_comment_multiline(self):
        src = b"int a; /*? secret\nstill secret */\nint b;\n"
        out = self.strip(src)
        self.assertNotIn(b"secret", out)
        self.assertIn(b"int a;", out)
        self.assertIn(b"int b;", out)

    def test_preserves_normal_line_comment(self):
        src = b"int a; // keep me\n"
        self.assertEqual(self.strip(src), src)

    def test_preserves_normal_block_comment(self):
        src = b"int a; /* keep me */\n"
        self.assertEqual(self.strip(src), src)

    def test_preserves_degraded_question_marks(self):
        # //????,0:?? —— 其 '?' 后面从不跟空白，绝不能被当 marker
        src = b"int x = 0; //????,0:??\n"
        self.assertEqual(self.strip(src), src)

    def test_preserves_ternary(self):
        # 代码里的三元 a ? b : c 含 "? "，但不在注释开头，必须原样保留
        src = b"int r = a ? b : c;\n"
        self.assertEqual(self.strip(src), src)

    def test_fullwidth_marker_trailing(self):
        # //？<全角空格>secret  =>  // + A3BF + A1A1 + ...
        src = b"int a; //\xa3\xbf\xa1\xa1secret\n"
        out = self.strip(src)
        self.assertNotIn(b"secret", out)
        self.assertEqual(out, b"int a;\n")

    def test_fullwidth_marker_whole_line(self):
        src = b"//\xa3\xbf\xa1\xa1secret\n"
        self.assertEqual(self.strip(src), b"")

    def test_idempotent(self):
        src = (b"int a; //? note\n"
               b"int b; /*? blk\nmore */\n"
               b"int r = a ? b : c; //????,0:??\n"
               b"// keep\n"
               b"int a; //\xa3\xbf\xa1\xa1x\n")
        once = self.strip(src)
        twice = self.strip(once)
        self.assertEqual(once, twice)


# ---------------------------------------------------------------------------
# 回归护栏常量：固件里真实的「退化问号」字节片段（bsp/lib_public.h:64）。
# 全是字面 0x3F，且每个 '?' 后从不跟空白 -> 绝不能被 marker 规则误吃。
# 若未来改 marker 规则让它命中，本常量参与的用例会立刻失败。
# ---------------------------------------------------------------------------
REAL_DEGRADED = b"    uint8_t ADCModeConversionMode;   //????,0:??,1:??\n"


# ---------------------------------------------------------------------------
# 用例组 3/4：real_drift() + clean_marker_leaks()
#   通过 CleanBranch.STRIP_ENV 指向的环境变量注入假剥离脚本；测试文件必须落在 CODE_PATHS 下
#   （App/Boot/drv/bsp/.clangd），否则 real_drift 的 diff 为空、用例会假通过。
# ---------------------------------------------------------------------------
class _StripInjectBase(BaseCleanBranchTest):
    def setUp(self):
        super().setUp()
        self._orig_env = os.environ.get(cb.STRIP_ENV)
        os.environ[cb.STRIP_ENV] = self.repo.strip_script

    def tearDown(self):
        if self._orig_env is None:
            os.environ.pop(cb.STRIP_ENV, None)
        else:
            os.environ[cb.STRIP_ENV] = self._orig_env
        super().tearDown()

    # 用 cat 绕过剥离往干净分支塞一个「泄漏」blob（模拟裸 cherry-pick 事故）
    def _plant_on_clean(self, rel, data, message="plant"):
        p = os.path.join(self.repo.clean_wt, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
        self.repo.git(FakeRepo.CLEAN, "-c", "filter.privclean.clean=cat",
                      "add", "--", rel)
        self.repo.git(FakeRepo.CLEAN, "commit", "-m", message)


class RealDriftTests(_StripInjectBase):
    def test_strip_bytes_uses_injected_script(self):
        # 前提校验：strip_bytes 确实走了注入的假脚本
        self.assertEqual(cb.strip_bytes(b"int a; //? x\n"), b"int a;\n")

    def test_real_drift_ignores_comment_only(self):
        # 同一内容提交两边：干净分支剥离、工作分支 (cat) 保留 -> 仅注释差异
        content = b"int a; //? note\n"
        self.repo.commit(FakeRepo.CLEAN, {"App/Code/x.c": content}, "x on clean")
        self.repo.commit(FakeRepo.WORK, {"App/Code/x.c": content}, "x on work")
        real, stat = cb.real_drift()
        self.assertEqual(real, [])
        self.assertEqual(stat, "")

    def test_real_drift_reports_code_change(self):
        self.repo.commit(FakeRepo.CLEAN, {"App/Code/x.c": b"int a;\n"}, "a")
        self.repo.commit(FakeRepo.WORK, {"App/Code/x.c": b"int b;\n"}, "b")
        real, stat = cb.real_drift()
        self.assertIn("App/Code/x.c", real)
        self.assertNotEqual(stat, "")

    def test_real_drift_reports_both(self):
        # 既有代码差异又有注释差异 -> 仍报出（strip 后代码仍不同）
        self.repo.commit(FakeRepo.CLEAN, {"App/Code/x.c": b"int a;\n"}, "a")
        self.repo.commit(FakeRepo.WORK, {"App/Code/x.c": b"int b; //? note\n"}, "b+cmt")
        real, _ = cb.real_drift()
        self.assertIn("App/Code/x.c", real)

    def test_real_drift_reports_added_file(self):
        # 工作分支独有的 .c（干净分支没有）-> 单边新增，一律真 drift
        self.repo.commit(FakeRepo.WORK, {"App/Code/only_work.c": b"int q;\n"}, "only work")
        real, _ = cb.real_drift()
        self.assertIn("App/Code/only_work.c", real)

    def test_real_drift_non_c_file_always_real(self):
        # 非 .c/.h（.uvprojx）差异 -> 一律真 drift（不做剥离比较）
        self.repo.commit(FakeRepo.CLEAN, {"App/Proj/x.uvprojx": b"<a/>\n"}, "proj x")
        self.repo.commit(FakeRepo.WORK, {"App/Proj/x.uvprojx": b"<b/>\n"}, "proj y")
        real, _ = cb.real_drift()
        self.assertIn("App/Proj/x.uvprojx", real)


class ClassifyDriftTests(_StripInjectBase):
    """EXPECTED_DRIFT 白名单：刻意漂移不该让 verify 报 FAIL，但仍要打印出来。"""

    def setUp(self):
        super().setUp()
        self._orig_expected = cb.EXPECTED_DRIFT
        cb.EXPECTED_DRIFT = {"App/Code/cfg.h"}

    def tearDown(self):
        cb.EXPECTED_DRIFT = self._orig_expected
        super().tearDown()

    def _diverge(self, rel, clean_bytes, work_bytes):
        self.repo.commit(FakeRepo.CLEAN, {rel: clean_bytes}, f"c {rel}")
        self.repo.commit(FakeRepo.WORK, {rel: work_bytes}, f"w {rel}")

    def test_whitelisted_file_is_expected_not_unexpected(self):
        self._diverge("App/Code/cfg.h", b"#define M 1\n", b"#define M 2\n")
        expected, unexpected, exp_stat, unexp_stat = cb.classify_drift()
        self.assertEqual(expected, ["App/Code/cfg.h"])
        self.assertEqual(unexpected, [])
        self.assertIn("cfg.h", exp_stat)     # 仍然可见，不是静默吞掉
        self.assertEqual(unexp_stat, "")

    def test_non_whitelisted_file_is_unexpected(self):
        self._diverge("App/Code/x.c", b"int a;\n", b"int b;\n")
        expected, unexpected, _, unexp_stat = cb.classify_drift()
        self.assertEqual(expected, [])
        self.assertEqual(unexpected, ["App/Code/x.c"])
        self.assertIn("x.c", unexp_stat)

    def test_mixed_partitions_and_stats_do_not_bleed(self):
        self._diverge("App/Code/cfg.h", b"#define M 1\n", b"#define M 2\n")
        self._diverge("App/Code/x.c", b"int a;\n", b"int b;\n")
        expected, unexpected, exp_stat, unexp_stat = cb.classify_drift()
        self.assertEqual(expected, ["App/Code/cfg.h"])
        self.assertEqual(unexpected, ["App/Code/x.c"])
        self.assertIn("cfg.h", exp_stat)
        self.assertNotIn("x.c", exp_stat)
        self.assertIn("x.c", unexp_stat)
        self.assertNotIn("cfg.h", unexp_stat)

    def test_comment_only_drift_on_whitelisted_file_is_not_drift_at_all(self):
        # 纯注释差异先被 strip 判据滤掉，压根不进 expected（白名单不该掩盖它）
        content = b"int a; //? note\n"
        self.repo.commit(FakeRepo.CLEAN, {"App/Code/cfg.h": content}, "x cfg")
        self.repo.commit(FakeRepo.WORK, {"App/Code/cfg.h": content}, "q cfg")
        expected, unexpected, _, _ = cb.classify_drift()
        self.assertEqual(expected, [])
        self.assertEqual(unexpected, [])

    def test_verify_passes_with_only_expected_drift(self):
        self._diverge("App/Code/cfg.h", b"#define M 1\n", b"#define M 2\n")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cb.verify()
        out = buf.getvalue()
        self.assertEqual(rc, 0, f"仅有白名单漂移时 verify 应通过。输出:\n{out}")
        self.assertIn("预期漂移", out)
        self.assertIn("cfg.h", out)

    def test_verify_fails_when_unexpected_drift_present(self):
        self._diverge("App/Code/x.c", b"int a;\n", b"int b;\n")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cb.verify()
        self.assertEqual(rc, 1)
        self.assertIn("FAIL", buf.getvalue())


class CommitContentOnWorkTests(_StripInjectBase):
    """detect 的第二道筛子：内容已在工作分支的干净分支 commit 不该报「待 cherry-pick」。

    真实触发场景：同一改动以不同 subject 分别落在两边（干净分支直接提交、工作分支早已有
    该内容），此时 subject 筛子失效，照 detect 的命令跑会在工作分支造空提交。
    """

    def _both(self, files, msg):
        """同内容同 subject 提交到两边，供 find_sync_point 认作同步点。"""
        self.repo.commit(FakeRepo.CLEAN, files, msg)
        self.repo.commit(FakeRepo.WORK, files, msg)

    def test_content_already_on_work_is_true(self):
        self.repo.commit(FakeRepo.WORK, {"App/Code/c.c": b"int v = 2;\n"}, "工作分支先落地")
        h = self.repo.commit(FakeRepo.CLEAN, {"App/Code/c.c": b"int v = 2;\n"}, "干净分支另一个 subject")
        self.assertTrue(cb.commit_content_on_work(h))

    def test_content_not_on_work_is_false(self):
        self.repo.commit(FakeRepo.WORK, {"App/Code/c.c": b"int v = 1;\n"}, "工作分支旧值")
        h = self.repo.commit(FakeRepo.CLEAN, {"App/Code/c.c": b"int v = 2;\n"}, "干净分支新值")
        self.assertFalse(cb.commit_content_on_work(h))

    def test_comment_only_difference_counts_as_aligned(self):
        # 工作分支带私人注释、干净分支不带 —— 剥离后一致，属已同步
        self.repo.commit(FakeRepo.WORK, {"App/Code/c.c": b"int v = 2;  //? my note\n"}, "work+注释")
        h = self.repo.commit(FakeRepo.CLEAN, {"App/Code/c.c": b"int v = 2;\n"}, "clean 纯净")
        self.assertTrue(cb.commit_content_on_work(h))

    def test_commit_touching_no_code_path_is_false(self):
        # 没碰 CODE_PATHS -> 不敢判「已同步」，交人判断
        h = self.repo.commit(FakeRepo.CLEAN, {"README_x.md": b"hi\n"}, "非代码路径")
        self.assertFalse(cb.commit_content_on_work(h))

    def test_partially_aligned_commit_is_false(self):
        # 改了两个文件，只有一个内容已在工作分支 -> 仍需搬
        self.repo.commit(FakeRepo.WORK, {"App/Code/a.c": b"int a = 9;\n"}, "work a")
        h = self.repo.commit(FakeRepo.CLEAN,
                             {"App/Code/a.c": b"int a = 9;\n", "App/Code/b.c": b"int b = 9;\n"},
                             "clean a+b")
        self.assertFalse(cb.commit_content_on_work(h))

    def test_detect_does_not_list_content_already_on_work(self):
        # 端到端：新 subject + 内容已在工作分支 -> detect 不该把它列进待 pick，且退 0
        self._both({"App/Code/base2.c": b"int base2;\n"}, "共同基点")
        self.repo.commit(FakeRepo.WORK, {"App/Code/c.c": b"int v = 2;\n"}, "工作分支侧 subject")
        self.repo.commit(FakeRepo.CLEAN, {"App/Code/c.c": b"int v = 2;\n"}, "干净分支侧完全不同的 subject")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cb.detect()
        out = buf.getvalue()
        self.assertNotIn("干净分支侧完全不同的 subject", out)
        self.assertIn("待 cherry-pick 到工作分支] 无", out)
        self.assertEqual(rc, 0, f"内容已同步，detect 应退 0。输出:\n{out}")


class MarkerLeakTests(_StripInjectBase):
    def test_verify_clean_tree_no_leaks(self):
        # 默认 fixture 只有无 marker 的 base.c
        self.assertEqual(cb.clean_marker_leaks(), [])

    def test_verify_detects_planted_marker(self):
        self._plant_on_clean("App/Code/leak.c", b"int a; //? secret\n")
        self.assertIn("App/Code/leak.c", cb.clean_marker_leaks())

    def test_ternary_not_prefiltered_and_not_leak(self):
        # 预筛带上注释开头符(`//`/`/*`)后，三元 `a ? b : c` 不再命中——这正是把 255 个文件的
        # 权威 strip 调用从 30 次压到 ~0 次的原因。它当然也不是 leak。
        # 护栏：若有人把 _MARKER_RE 放宽回不带开头符的版本，本用例会失败。
        code = b"int r = a ? b : c;\n"
        self.assertIsNone(cb._MARKER_RE.search(code))
        self.repo.commit(FakeRepo.CLEAN, {"App/Code/tern.c": code}, "ternary")
        self.assertNotIn("App/Code/tern.c", cb.clean_marker_leaks())

    def test_prefilter_never_misses_a_leak(self):
        # 预筛的唯一正确性要求是「超集」：真 leak 必须命中预筛，否则会被静默跳过。
        for data in (b"int a; //? x\n",                    # 半角 + 空格
                     b"int a; //?\tx\n",                   # 半角 + Tab
                     b"int a; /*? x */\n",                 # 块注释
                     b"int a; //\xa3\xbf\xa1\xa1x\n"):     # 全角 '？' + 全角空格
            self.assertIsNotNone(cb._MARKER_RE.search(data), data)
            self.assertNotEqual(cb.strip_bytes(data), data, data)

    def test_degraded_question_marks_not_leak(self):
        # 真实退化问号片段：'?' 后不跟空白 -> 既不命中预筛，也非 leak
        self.assertIsNone(cb._MARKER_RE.search(REAL_DEGRADED))
        self.repo.commit(FakeRepo.CLEAN, {"App/Code/deg.c": REAL_DEGRADED}, "degraded")
        self.assertNotIn("App/Code/deg.c", cb.clean_marker_leaks())

    def test_fullwidth_marker_detected(self):
        # 全角 '？'(A3BF) + 全角空格(A1A1) -> 真 marker，剥离前提交则判为 leak
        data = b"int a; //\xa3\xbf\xa1\xa1secret\n"
        self.assertIsNotNone(cb._MARKER_RE.search(data))
        self._plant_on_clean("App/Code/fw.c", data)
        self.assertIn("App/Code/fw.c", cb.clean_marker_leaks())


# ---------------------------------------------------------------------------
# 用例组 5：PickToClean.main()（Task 3）
#   复用 _StripInjectBase：clean worktree 继承共享 filter（= 假剥离脚本），
#   work worktree filter=cat（保留注释）。pick 脚本本身不调 strip，剥离全靠 git 的
#   clean filter 在 `git add` 时发生——正是本方案的立身之本。
#   注意：pick 脚本用 cb.clean_wt()，其结果由 BaseCleanBranchTest 注入的
#   cb._WT_CACHE 提供。
# ---------------------------------------------------------------------------
class PickToCleanTests(_StripInjectBase):
    def _run_pick(self, argv, cfg=EMPTY_CFG):
        """跑 main(argv)，吞掉 stdout/stderr，返回 (rc, stdout_text, stderr_text)。

        显式传 cfg：不传的话 main() 会去加载**这台机器上真实的**两层配置，
        用例结果就跟谁在跑它有关了。"""
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = PickToClean.main(argv, cfg=cfg)
        return rc, out.getvalue(), err.getvalue()

    def _clean(self, *args, **kw):
        return self.repo.git(FakeRepo.CLEAN, *args, **kw)

    def _clean_head(self):
        return self._clean("rev-parse", "HEAD").stdout.strip()

    def _commit_both(self, files, msg):
        """把同一内容提交到干净分支与工作分支（各自过自己的 filter），供后续做 modify/delete。"""
        self.repo.commit(FakeRepo.CLEAN, files, msg + " (clean)")
        return self.repo.commit(FakeRepo.WORK, files, msg + " (work)")

    def test_pick_strips_comments(self):
        # 工作分支上带私人注释的新文件 -> pick 后干净分支 BLOB 不含注释
        src = self.repo.commit(
            FakeRepo.WORK, {"App/Code/x.c": b"int a;\n//? secret\nint b;\n"}, "add x")
        # 工作分支 blob 确应保留注释（cat 直通），确认前提成立
        qblob = self.repo.git(FakeRepo.WORK, "show", f"{src}:App/Code/x.c",
                              binary=True).stdout
        self.assertIn(b"secret", qblob)

        rc, _out_, _err = self._run_pick([src])
        self.assertEqual(rc, 0, _err)

        new = self._clean_head()
        xblob = self._clean("show", f"{new}:App/Code/x.c", binary=True).stdout
        self.assertNotIn(b"secret", xblob)
        self.assertIn(b"int a;", xblob)
        self.assertIn(b"int b;", xblob)
        # 工作区磁盘上也不留私人注释（步骤 f 的 checkout）
        with open(os.path.join(self.repo.clean_wt, "App", "Code", "x.c"), "rb") as f:
            self.assertNotIn(b"secret", f.read())

    def test_pick_resets_author(self):
        src = self.repo.commit(FakeRepo.WORK, {"App/Code/x.c": b"int a;\n"}, "add x")
        work_ae = self.repo.git(FakeRepo.WORK, "log", "-1", "--format=%ae", src).stdout.strip()

        rc, _out_, _err = self._run_pick([src])
        self.assertEqual(rc, 0, _err)

        new = self._clean_head()
        new_ae = self._clean("log", "-1", "--format=%ae", new).stdout.strip()
        clean_email = self._clean("config", "user.email").stdout.strip()
        self.assertEqual(new_ae, clean_email)
        self.assertNotEqual(new_ae, work_ae)

    def test_pick_keeps_message(self):
        msg = "feat: 主题行\n\n正文说明一行"
        src = self.repo.commit(FakeRepo.WORK, {"App/Code/x.c": b"int a;\n"}, msg)

        rc, _out_, _err = self._run_pick([src])
        self.assertEqual(rc, 0, _err)

        new = self._clean_head()
        s = self._clean("log", "-1", "--format=%s", new).stdout.strip()
        b = self._clean("log", "-1", "--format=%b", new).stdout.strip()
        self.assertEqual(s, "feat: 主题行")
        self.assertEqual(b, "正文说明一行")

    def test_pick_refuses_dirty_worktree(self):
        src = self.repo.commit(FakeRepo.WORK, {"App/Code/x.c": b"int a;\n"}, "add x")
        before = self._clean_head()
        # 弄脏干净分支工作区（未跟踪文件即可让 status --porcelain 非空）
        with open(os.path.join(self.repo.clean_wt, "dirty.txt"), "wb") as f:
            f.write(b"junk\n")

        rc, _out_, err = self._run_pick([src])
        self.assertEqual(rc, 2)
        self.assertIn("先清理干净分支 worktree", err)
        self.assertEqual(self._clean_head(), before)   # 未产生新 commit

    def test_pick_refuses_doc_commit(self):
        # 第一条是干净代码 commit，第二条含文档 -> 预检阶段整体拒绝，第一条也不得被 pick
        good = self.repo.commit(FakeRepo.WORK, {"App/Code/g.c": b"int g;\n"}, "good code")
        doc = self.repo.commit(FakeRepo.WORK, {"docs/a.md": b"# doc\n"}, "add doc")
        before = self._clean_head()

        rc, _out_, err = self._run_pick([good, doc])
        self.assertEqual(rc, 2)
        self.assertIn("docs/a.md", err)
        self.assertEqual(self._clean_head(), before)   # 预检语义：good 也没被 pick

    def test_pick_skips_comment_only_commit(self):
        # y.c 在两分支上内容相同；工作分支上再加一行纯私人注释
        self._commit_both({"App/Code/y.c": b"int y;\n"}, "base y")
        src = self.repo.commit(
            FakeRepo.WORK, {"App/Code/y.c": b"int y;\n//? x\n"}, "comment only")
        before = self._clean_head()

        rc, out, _err = self._run_pick([src])
        self.assertEqual(rc, 0)
        self.assertIn("skip", out)
        self.assertEqual(self._clean_head(), before)   # HEAD 未变
        # 工作区干净（skip 分支的 reset --hard 清掉了未剥离残留）
        self.assertEqual(self._clean("status", "--porcelain").stdout.strip(), "")

    def test_pick_comment_only_after_stripped_pick(self):
        """回归：真仓库实跑抓到的坑。

        一次 pick 两条：第一条(代码+私人注释)被剥离落到干净分支后，那边的文件里已无那行
        注释；第二条(纯注释)的 diff 正以那行为上下文 → 裸 cherry-pick 会冲突，
        永远走不到「剥离后为空」的跳过分支。修法是在 cherry-pick **之前**就识别纯注释 commit。
        """
        self._commit_both({"App/Code/z.c": b"int z;\n"}, "base z")
        c1 = self.repo.commit(
            FakeRepo.WORK, {"App/Code/z.c": b"int z;\nint w = 1;  //? note\n"}, "code + comment")
        c2 = self.repo.commit(
            FakeRepo.WORK, {"App/Code/z.c": b"int z;\nint w = 1;  //? note\n//? more\n"},
            "comment only")
        before = self._clean_head()

        rc, out, err = self._run_pick([c1, c2])
        self.assertEqual(rc, 0, f"应两条都处理完，不该冲突。err={err}")
        self.assertIn("skip", out)                       # 第二条被跳过
        self.assertNotIn("冲突", err)
        # 干净分支只多了一条 commit
        self.assertEqual(int(self._clean("rev-list", "--count", f"{before}..HEAD").stdout), 1)
        blob = self._clean("show", "HEAD:App/Code/z.c", binary=True).stdout
        self.assertIn(b"int w = 1;", blob)
        self.assertNotIn(b"//?", blob)
        self.assertEqual(self._clean("status", "--porcelain").stdout.strip(), "")

    def test_pick_conflict_leaves_state(self):
        # 两分支对同一文件同一行做不同修改 -> cherry-pick 冲突
        self._commit_both({"App/Code/c.c": b"base\n"}, "base c")
        self.repo.commit(FakeRepo.CLEAN, {"App/Code/c.c": b"CLEAN\n"}, "clean change")
        src = self.repo.commit(FakeRepo.WORK, {"App/Code/c.c": b"WORK\n"}, "work change")
        before = self._clean_head()

        rc, _out_, err = self._run_pick([src])
        self.assertEqual(rc, 1)
        self.assertIn("冲突", err)
        # 未产生新 commit
        self.assertEqual(self._clean_head(), before)
        # 现场保留：注意 cherry-pick -n 冲突**不会**设 CHERRY_PICK_HEAD（见 report），
        # 真正留下的是未合并的 index 条目——以此判定“现场已保留”。
        unmerged = self._clean("ls-files", "-u").stdout.strip()
        self.assertNotEqual(unmerged, "")

    def test_pick_handles_deletion(self):
        # d.c 两分支相同；工作分支删除它 -> pick 后干净分支也删除
        self._commit_both({"App/Code/d.c": b"int d;\n"}, "base d")
        self.repo.git(FakeRepo.WORK, "rm", "App/Code/d.c")
        self.repo.git(FakeRepo.WORK, "commit", "-m", "delete d")
        src = self.repo.git(FakeRepo.WORK, "rev-parse", "HEAD").stdout.strip()

        rc, _out_, _err = self._run_pick([src])
        self.assertEqual(rc, 0, _err)

        tracked = self._clean("ls-files", "App/Code/d.c").stdout.strip()
        self.assertEqual(tracked, "")   # 已从干净分支索引移除
        self.assertFalse(os.path.exists(
            os.path.join(self.repo.clean_wt, "App", "Code", "d.c")))

    def _cfg_with_identity(self, name, email):
        data = {"identity": {"clean": {"name": name, "email": email}}}
        merged, origins = local_config.merge_layers([(local_config.GLOBAL, data)])
        return local_config.Config(
            merged, origins,
            {local_config.GLOBAL: None, local_config.PROJECT: None})

    def test_pick_refuses_when_worktree_would_sign_as_someone_else(self):
        # 这是整个方案要防的事故:一份没配过身份的检出会把对外分支的提交签上个人
        # 身份,而且成功退出、看起来一切正常。宁可停下。
        src = self.repo.commit(FakeRepo.WORK, {"App/Code/x.c": b"int a;\n"}, "add x")
        before = self._clean_head()
        cfg = self._cfg_with_identity("someone-else", "else@example.com")

        rc, _out_, err = self._run_pick([src], cfg=cfg)
        self.assertEqual(rc, 2)
        self.assertIn("提交身份与配置不符", err)
        self.assertIn("someone-else", err)
        self.assertEqual(self._clean_head(), before)   # 未产生新 commit

    def test_pick_proceeds_when_identity_matches(self):
        src = self.repo.commit(FakeRepo.WORK, {"App/Code/x.c": b"int a;\n"}, "add x")
        cfg = self._cfg_with_identity(FakeRepo.SHARED_NAME, FakeRepo.SHARED_EMAIL)

        rc, _out_, err = self._run_pick([src], cfg=cfg)
        self.assertEqual(rc, 0, err)

    def test_pick_skips_the_identity_check_when_none_configured(self):
        # 没声明身份就没什么可核对的 —— 不能因为「没配」就拦住所有人。
        src = self.repo.commit(FakeRepo.WORK, {"App/Code/x.c": b"int a;\n"}, "add x")
        rc, _out_, err = self._run_pick([src])
        self.assertEqual(rc, 0, err)

    def test_pick_multiple_in_order(self):
        h1 = self.repo.commit(FakeRepo.WORK, {"App/Code/m1.c": b"int m1;\n"}, "add m1")
        h2 = self.repo.commit(FakeRepo.WORK, {"App/Code/m2.c": b"int m2;\n"}, "add m2")

        rc, _out_, _err = self._run_pick([h1, h2])
        self.assertEqual(rc, 0, _err)

        # 两条新 commit，顺序：m1 在前(旧)、m2 在后(新)
        subjects = self._clean("log", "-2", "--format=%s", "HEAD").stdout.split("\n")
        subjects = [s for s in subjects if s]
        self.assertEqual(subjects, ["add m2", "add m1"])
        # 两文件都到位
        self.assertNotEqual(self._clean("ls-files", "App/Code/m1.c").stdout.strip(), "")
        self.assertNotEqual(self._clean("ls-files", "App/Code/m2.c").stdout.strip(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
