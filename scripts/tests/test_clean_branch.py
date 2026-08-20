#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_clean_branch.py — CleanBranch.py / PickToClean.py 的 stdlib unittest 测试。

运行:
    py -3 -m pytest tests/test_clean_branch.py -q

=== 设计要点 ===

1. `_make_repo()` 造一个镜像真实布局的临时 git 仓库:`extensions.worktreeConfig=true`、
   两个 linked worktree(clean/work)、两 worktree 用不同提交身份。返回 `FakeRepo`
   对象(含各路径 + `commit()`/`git()` 助手)。

2. **让 CleanBranch.py 指向临时仓库的办法(monkeypatch)**:
   分支引用是模块级名字 `CLEAN_REF / WORK_REF / MAIN_REF`(正常由 `configure()` 从两层
   配置填入),worktree 解析结果缓存在模块级 `_WT_CACHE`(惰性、import 无副作用)。
   `BaseCleanBranchTest.setUp` 里:
     - 直接覆盖三个 REF 为临时仓库的分支名,**不走 configure()**——测试要的是逻辑,
       不是配置加载;配置加载自己有 test_local_config.py 管;
     - 直接把 `cb._WT_CACHE` 塞成 `(clean_wt, work_wt)`,绕过 `_resolve_worktrees()`
       (它依赖 `os.getcwd()` 在真仓库内,测试里不成立)。
   `tearDown` 恢复原值并把 `_WT_CACHE` 复位为 None,避免测试间状态泄漏。

3. **CRLF**:fixture 里 `core.autocrlf=false`,否则 git 的换行转换会破坏字节级 blob 断言。

4. **Windows 清理**:git 对象文件常为只读,`shutil.rmtree` 直删会 PermissionError;
   `_rm_readonly` 处理器先 chmod 再重试。
"""
import io
import json
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


# ---------------------------------------------------------------------------
# fixture：临时 git 仓库 + 两个 worktree
# ---------------------------------------------------------------------------
class FakeRepo:
    """
    镜像真实「干净/工作」双分支布局的临时仓库。属性：
      root          — 主 worktree（默认分支）目录
      clean_wt      — 干净分支 linked worktree 目录
      work_wt       — 工作分支 linked worktree 目录
      main_branch / clean_branch / work_branch — 分支名
    方法：
      git(which, *args, ...)            — 在某 worktree 跑 git
      commit(which, files, message)     — 提交 {relpath: bytes}，返回新 HEAD hash
      cleanup()                         — 递归删除临时目录（处理只读）
    """
    MAIN = "main"
    CLEAN = "clean"
    WORK = "work"

    # 身份：干净分支用共享 config，工作分支用 worktree-local override，
    # 好让作者归一的用例分得出两者。
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
        self._build()

    # -- 构建 --
    def _build(self):
        os.makedirs(self.root, exist_ok=True)

        # 1. init（显式默认分支名，避免 master/main 歧义）
        _git(self.root, "init", "-b", self.main_branch)

        # 2. 共享 config
        _git(self.root, "config", "extensions.worktreeConfig", "true")
        _git(self.root, "config", "core.autocrlf", "false")
        _git(self.root, "config", "user.name", self.SHARED_NAME)
        _git(self.root, "config", "user.email", self.SHARED_EMAIL)

        # 3. 初始提交 + 两分支
        with open(os.path.join(self.root, "base.c"), "wb") as f:
            f.write(b"int base;\n")
        _git(self.root, "add", "base.c")
        _git(self.root, "commit", "-m", "base")
        _git(self.root, "branch", self.clean_branch)
        _git(self.root, "branch", self.work_branch)

        # 4. 两个 linked worktree
        _git(self.root, "worktree", "add", self.clean_wt, self.clean_branch)
        _git(self.root, "worktree", "add", self.work_wt, self.work_branch)

        # 5. 工作分支 worktree：独立身份
        _git(self.work_wt, "config", "--worktree", "user.name", self.WORK_NAME)
        _git(self.work_wt, "config", "--worktree", "user.email", self.WORK_EMAIL)

    # -- 助手 --
    def _wt(self, which):
        return {self.CLEAN: self.clean_wt, self.WORK: self.work_wt,
                self.MAIN: self.root}.get(which, which)

    def git(self, which, *args, **kw):
        return _git(self._wt(which), *args, **kw)

    def commit(self, which, files, message):
        """向干净/工作分支(或 worktree 路径)提交 {relpath: bytes}。"""
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
# 用例组 1：fixture 冒烟
# ---------------------------------------------------------------------------
class FixtureTests(BaseCleanBranchTest):
    def test_fixture_builds(self):
        self.assertTrue(os.path.isdir(self.repo.clean_wt))
        self.assertTrue(os.path.isdir(self.repo.work_wt))

    def test_the_two_worktrees_sign_differently(self):
        # 作者归一的用例要靠这个差别才有意义
        c = self.repo.git(FakeRepo.CLEAN, "config", "user.email").stdout.strip()
        w = self.repo.git(FakeRepo.WORK, "config", "user.email").stdout.strip()
        self.assertNotEqual(c, w)


# ---------------------------------------------------------------------------
# 用例组 2：real_drift()
# ---------------------------------------------------------------------------
class RealDriftTests(BaseCleanBranchTest):
    def test_identical_content_is_not_drift(self):
        content = b"int a = 1;\n"
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

    def test_comment_difference_is_now_ordinary_drift(self):
        # 剥离退役后不再有「只差注释不算 drift」这条例外:注释也是内容。
        self.repo.commit(FakeRepo.CLEAN, {"App/Code/x.c": b"int a;\n"}, "a")
        self.repo.commit(FakeRepo.WORK, {"App/Code/x.c": b"int a; // note\n"}, "a+cmt")
        real, _ = cb.real_drift()
        self.assertIn("App/Code/x.c", real)

    def test_real_drift_reports_added_file(self):
        # 工作分支独有的 .c（干净分支没有）-> 单边新增，一律真 drift
        self.repo.commit(FakeRepo.WORK, {"App/Code/only_work.c": b"int q;\n"}, "only work")
        real, _ = cb.real_drift()
        self.assertIn("App/Code/only_work.c", real)

    def test_file_outside_code_paths_is_ignored(self):
        # CODE_PATHS 之外的差异不算代码 drift（文档归 stray_docs 管）
        self.repo.commit(FakeRepo.WORK, {"notes/x.c": b"int q;\n"}, "outside")
        real, _ = cb.real_drift()
        self.assertEqual(real, [])

    def test_real_drift_non_c_file_counts_too(self):
        self.repo.commit(FakeRepo.CLEAN, {"App/Proj/x.uvprojx": b"<a/>\n"}, "proj x")
        self.repo.commit(FakeRepo.WORK, {"App/Proj/x.uvprojx": b"<b/>\n"}, "proj y")
        real, _ = cb.real_drift()
        self.assertIn("App/Proj/x.uvprojx", real)


# ---------------------------------------------------------------------------
# 用例组 3：classify_drift() —— expected_drift 白名单
# ---------------------------------------------------------------------------
class ClassifyDriftTests(BaseCleanBranchTest):
    def setUp(self):
        super().setUp()
        cb.EXPECTED_DRIFT = {"App/Code/cfg.h": "刻意保留的分歧"}

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

    def test_verify_passes_with_only_expected_drift(self):
        self._diverge("App/Code/cfg.h", b"#define M 1\n", b"#define M 2\n")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cb.verify()
        out = buf.getvalue()
        self.assertEqual(rc, 0, f"仅有白名单漂移时 verify 应通过。输出:\n{out}")
        self.assertIn("预期漂移", out)
        self.assertIn("cfg.h", out)

    def test_verify_prints_the_reason_for_expected_drift(self):
        # 名单是文件级的:该文件将来的差异也不再 FAIL。理由是下一个人唯一能据以
        # 判断「这条还成立吗」的东西,不打印等于没有。
        self._diverge("App/Code/cfg.h", b"#define M 1\n", b"#define M 2\n")
        buf = io.StringIO()
        with redirect_stdout(buf):
            cb.verify()
        self.assertIn("刻意保留的分歧", buf.getvalue())

    def test_verify_fails_when_unexpected_drift_present(self):
        self._diverge("App/Code/x.c", b"int a;\n", b"int b;\n")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cb.verify()
        self.assertEqual(rc, 1)
        self.assertIn("FAIL", buf.getvalue())


# ---------------------------------------------------------------------------
# 用例组 4：detect 的第二道筛子
#   内容已在工作分支的干净分支 commit 不该报「待 cherry-pick」。
#   真实触发场景：同一改动以不同 subject 分别落在两边，此时 subject 筛子失效，
#   照 detect 打印的命令跑会在工作分支造空提交。
# ---------------------------------------------------------------------------
class CommitContentOnWorkTests(BaseCleanBranchTest):
    def _both(self, files, msg):
        """同内容同 subject 提交到两边，供 find_sync_point 认作同步点。"""
        self.repo.commit(FakeRepo.CLEAN, files, msg)
        self.repo.commit(FakeRepo.WORK, files, msg)

    def test_content_already_on_work_is_true(self):
        self.repo.commit(FakeRepo.WORK, {"App/Code/c.c": b"int v = 2;\n"}, "工作分支先落地")
        h = self.repo.commit(FakeRepo.CLEAN, {"App/Code/c.c": b"int v = 2;\n"},
                             "干净分支另一个 subject")
        self.assertTrue(cb.commit_content_on_work(h))

    def test_content_not_on_work_is_false(self):
        self.repo.commit(FakeRepo.WORK, {"App/Code/c.c": b"int v = 1;\n"}, "工作分支旧值")
        h = self.repo.commit(FakeRepo.CLEAN, {"App/Code/c.c": b"int v = 2;\n"},
                             "干净分支新值")
        self.assertFalse(cb.commit_content_on_work(h))

    def test_commit_touching_no_code_path_is_false(self):
        # 没碰 CODE_PATHS -> 不敢判「已同步」，交人判断
        h = self.repo.commit(FakeRepo.CLEAN, {"README_x.md": b"hi\n"}, "非代码路径")
        self.assertFalse(cb.commit_content_on_work(h))

    def test_partially_aligned_commit_is_false(self):
        # 改了两个文件，只有一个内容已在工作分支 -> 仍需搬
        self.repo.commit(FakeRepo.WORK, {"App/Code/a.c": b"int a = 9;\n"}, "work a")
        h = self.repo.commit(FakeRepo.CLEAN,
                             {"App/Code/a.c": b"int a = 9;\n",
                              "App/Code/b.c": b"int b = 9;\n"},
                             "clean a+b")
        self.assertFalse(cb.commit_content_on_work(h))

    def test_detect_does_not_list_content_already_on_work(self):
        # 端到端：新 subject + 内容已在工作分支 -> detect 不该把它列进待 pick，且退 0
        self._both({"App/Code/base2.c": b"int base2;\n"}, "共同基点")
        self.repo.commit(FakeRepo.WORK, {"App/Code/c.c": b"int v = 2;\n"}, "工作分支侧 subject")
        self.repo.commit(FakeRepo.CLEAN, {"App/Code/c.c": b"int v = 2;\n"},
                         "干净分支侧完全不同的 subject")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cb.detect()
        out = buf.getvalue()
        self.assertNotIn("干净分支侧完全不同的 subject", out)
        self.assertIn("待 cherry-pick 到工作分支] 无", out)
        self.assertEqual(rc, 0, f"内容已同步，detect 应退 0。输出:\n{out}")

    def test_detect_blacklists_never_pick(self):
        self._both({"App/Code/base2.c": b"int base2;\n"}, "共同基点")
        h = self.repo.commit(FakeRepo.CLEAN, {"App/Code/n.c": b"int n;\n"}, "永不搬运")
        cb.NEVER_PICK = {h: "刻意不同步"}
        buf = io.StringIO()
        with redirect_stdout(buf):
            cb.detect()
        out = buf.getvalue()
        self.assertIn("[永不搬运]", out)
        self.assertIn("刻意不同步", out)


# ---------------------------------------------------------------------------
# 用例组 5：越界文档
# ---------------------------------------------------------------------------
class StrayDocTests(BaseCleanBranchTest):
    def test_tracked_doc_on_clean_is_stray(self):
        self.repo.commit(FakeRepo.CLEAN, {"App/Code/note.md": b"# n\n"}, "doc")
        tracked, _ = cb.stray_docs()
        self.assertIn("App/Code/note.md", tracked)

    def test_allow_listed_doc_is_not_stray(self):
        cb.ALLOW = ["App/Code/note.md"]
        self.repo.commit(FakeRepo.CLEAN, {"App/Code/note.md": b"# n\n"}, "doc")
        tracked, _ = cb.stray_docs()
        self.assertNotIn("App/Code/note.md", tracked)

    def test_untracked_stray_is_reported(self):
        p = os.path.join(self.repo.clean_wt, "loose.md")
        with open(p, "wb") as f:
            f.write(b"# loose\n")
        _, untracked = cb.stray_docs()
        self.assertIn("loose.md", untracked)


# ---------------------------------------------------------------------------
# 用例组 6：PickToClean.main()
# ---------------------------------------------------------------------------
class PickToCleanTests(BaseCleanBranchTest):
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
        """把同一内容提交到两边（供后续做 modify/delete）。"""
        self.repo.commit(FakeRepo.CLEAN, files, msg + " (clean)")
        return self.repo.commit(FakeRepo.WORK, files, msg + " (work)")

    def test_pick_lands_the_content(self):
        src = self.repo.commit(FakeRepo.WORK,
                               {"App/Code/x.c": b"int a;\nint b;\n"}, "add x")
        rc, _out_, err = self._run_pick([src])
        self.assertEqual(rc, 0, err)
        blob = self._clean("show", "HEAD:App/Code/x.c", binary=True).stdout
        self.assertEqual(blob, b"int a;\nint b;\n")

    def test_pick_resets_author(self):
        src = self.repo.commit(FakeRepo.WORK, {"App/Code/x.c": b"int a;\n"}, "add x")
        work_ae = self.repo.git(FakeRepo.WORK, "log", "-1", "--format=%ae",
                                src).stdout.strip()

        rc, _out_, err = self._run_pick([src])
        self.assertEqual(rc, 0, err)

        new_ae = self._clean("log", "-1", "--format=%ae", "HEAD").stdout.strip()
        clean_email = self._clean("config", "user.email").stdout.strip()
        self.assertEqual(new_ae, clean_email)
        self.assertNotEqual(new_ae, work_ae)

    def test_pick_keeps_message(self):
        msg = "feat: 主题行\n\n正文说明一行"
        src = self.repo.commit(FakeRepo.WORK, {"App/Code/x.c": b"int a;\n"}, msg)

        rc, _out_, err = self._run_pick([src])
        self.assertEqual(rc, 0, err)

        s = self._clean("log", "-1", "--format=%s", "HEAD").stdout.strip()
        b = self._clean("log", "-1", "--format=%b", "HEAD").stdout.strip()
        self.assertEqual(s, "feat: 主题行")
        self.assertEqual(b, "正文说明一行")

    def test_pick_refuses_dirty_worktree(self):
        src = self.repo.commit(FakeRepo.WORK, {"App/Code/x.c": b"int a;\n"}, "add x")
        before = self._clean_head()
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

    def test_pick_refuses_unresolvable_hash(self):
        rc, _out_, err = self._run_pick(["deadbeef"])
        self.assertEqual(rc, 2)
        self.assertIn("无法解析 commit", err)

    def test_pick_skips_a_commit_whose_content_is_already_there(self):
        # 两边先各自落地同样的内容，再 pick 工作分支那条 -> 空提交，跳过而不是留个空壳
        self._commit_both({"App/Code/y.c": b"int y;\n"}, "base y")
        src = self.repo.commit(FakeRepo.WORK, {"App/Code/y.c": b"int y = 1;\n"}, "bump y")
        self.repo.commit(FakeRepo.CLEAN, {"App/Code/y.c": b"int y = 1;\n"}, "同样的改动")
        before = self._clean_head()

        rc, out, err = self._run_pick([src])
        self.assertEqual(rc, 0, err)
        self.assertIn("skip", out)
        self.assertEqual(self._clean_head(), before)
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
        self.assertEqual(self._clean_head(), before)   # 未产生新 commit
        unmerged = self._clean("ls-files", "-u").stdout.strip()
        self.assertNotEqual(unmerged, "")              # 现场保留

    def test_pick_refuses_when_a_pick_is_already_in_progress(self):
        # 上一场冲突没收拾就再来一次 -> 在动手前拒绝，别把两次搬运缠在一起
        self._commit_both({"App/Code/c.c": b"base\n"}, "base c")
        self.repo.commit(FakeRepo.CLEAN, {"App/Code/c.c": b"CLEAN\n"}, "clean change")
        src = self.repo.commit(FakeRepo.WORK, {"App/Code/c.c": b"WORK\n"}, "work change")
        self._run_pick([src])                          # 制造冲突现场
        rc, _out_, err = self._run_pick([src])
        self.assertEqual(rc, 2)
        self.assertIn("进行中的 cherry-pick", err)

    def test_pick_handles_deletion(self):
        # d.c 两分支相同；工作分支删除它 -> pick 后干净分支也删除
        self._commit_both({"App/Code/d.c": b"int d;\n"}, "base d")
        self.repo.git(FakeRepo.WORK, "rm", "App/Code/d.c")
        self.repo.git(FakeRepo.WORK, "commit", "-m", "delete d")
        src = self.repo.git(FakeRepo.WORK, "rev-parse", "HEAD").stdout.strip()

        rc, _out_, err = self._run_pick([src])
        self.assertEqual(rc, 0, err)

        self.assertEqual(self._clean("ls-files", "App/Code/d.c").stdout.strip(), "")
        self.assertFalse(os.path.exists(
            os.path.join(self.repo.clean_wt, "App", "Code", "d.c")))

    def test_pick_multiple_in_order(self):
        h1 = self.repo.commit(FakeRepo.WORK, {"App/Code/m1.c": b"int m1;\n"}, "add m1")
        h2 = self.repo.commit(FakeRepo.WORK, {"App/Code/m2.c": b"int m2;\n"}, "add m2")

        rc, _out_, err = self._run_pick([h1, h2])
        self.assertEqual(rc, 0, err)

        subjects = [s for s in
                    self._clean("log", "-2", "--format=%s", "HEAD").stdout.split("\n") if s]
        self.assertEqual(subjects, ["add m2", "add m1"])
        self.assertNotEqual(self._clean("ls-files", "App/Code/m1.c").stdout.strip(), "")
        self.assertNotEqual(self._clean("ls-files", "App/Code/m2.c").stdout.strip(), "")

    # -- 身份闸 --
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
        self.assertEqual(self._clean_head(), before)

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


class CleanBranchCliTests(BaseCleanBranchTest):
    """CleanBranch 的 CLI-AI 机器通道(信封 / 退出码 / eager ai-help)。

    直接跑 main()/command()：configure() 会去读真实的双层配置，测试里把它
    monkeypatch 成 no-op —— 模块设置已在 setUp 注入。
    """

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        orig_configure = cb.configure
        cb.configure = lambda cfg=None: None
        try:
            code = cb.main(list(argv), sinks=cb.cc.Sinks(out=out, err=err))
        finally:
            cb.configure = orig_configure
        return code, out.getvalue(), err.getvalue()

    def test_detect_json_success_envelope(self):
        code, out, err = self.run_main("detect", "--json")
        self.assertEqual(code, 0)
        obj = json.loads(out)
        self.assertTrue(obj["ok"])
        self.assertIsNone(obj["error"])
        self.assertEqual(obj["data"]["action"], "detect")
        self.assertIn("pending", obj["data"])
        self.assertEqual(err, "")

    def test_verify_json_pass_envelope(self):
        code, out, err = self.run_main("verify", "--json")
        self.assertEqual(code, 0)
        obj = json.loads(out)
        self.assertTrue(obj["ok"])
        self.assertEqual(obj["data"]["action"], "verify")
        self.assertTrue(obj["data"]["passed"])

    def test_verify_fail_is_verification_failed_rc1(self):
        self.repo.commit(FakeRepo.CLEAN, {"App/Code/x.c": b"int a;\n"}, "a")
        self.repo.commit(FakeRepo.WORK, {"App/Code/x.c": b"int b;\n"}, "b")
        code, out, err = self.run_main("verify", "--json")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        obj = json.loads(err)
        self.assertFalse(obj["ok"])
        self.assertEqual(obj["error"]["code"], "E_VERIFICATION_FAILED")
        self.assertFalse(obj["error"]["details"]["passed"])
        self.assertIn("log", obj["error"]["details"])

    def test_bad_arg_json_envelope(self):
        code, out, err = self.run_main("--contract-test-invalid", "--json")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertEqual(json.loads(err)["error"]["code"], "E_VALIDATION")

    def test_format_json_equiv(self):
        code, out, err = self.run_main(
            "--format", "json", "--contract-test-invalid")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error"]["code"], "E_VALIDATION")

    def test_ai_help_eager(self):
        code, out, err = self.run_main("--contract-test-invalid", "--ai-help")
        self.assertEqual(code, 0)
        self.assertIn("name: CleanBranch", out)
        self.assertEqual(err, "")

    def test_missing_action_human_shows_help(self):
        code, out, err = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn("usage:", out)
        self.assertEqual(err, "")

    def test_missing_action_json_is_validation_rc2(self):
        code, out, err = self.run_main("--json")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertEqual(json.loads(err)["error"]["code"], "E_VALIDATION")

    def test_git_failure_maps_to_external_tool(self):
        # 机器模式下 _resolve_worktrees 的 git 失败 -> E_EXTERNAL_TOOL + rc1
        def boom(*a, **k):
            raise cb.cc.ExternalToolError(
                "E_EXTERNAL_TOOL", "git failed", details={"tool": "git"})

        orig_resolve = cb._resolve_worktrees
        cb._resolve_worktrees = boom
        cb._WT_CACHE = None
        try:
            code, out, err = self.run_main("detect", "--json")
        finally:
            cb._resolve_worktrees = orig_resolve
            cb._WT_CACHE = (self.repo.clean_wt, self.repo.work_wt)
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        obj = json.loads(err)
        self.assertFalse(obj["ok"])
        self.assertEqual(obj["error"]["code"], "E_EXTERNAL_TOOL")
        self.assertEqual(obj["error"]["details"]["tool"], "git")


if __name__ == "__main__":
    unittest.main(verbosity=2)
