"""hooks/pre-push 的单测（第二阶段第 2 批）。

A 路：直接调用钩子（sh hooks/pre-push origin <url> < stdin），不经过 git，
覆盖钩子自身行为矩阵（武装判定 / stdin 重放 / 退出码传播 / 接力路径 /
退出码区分 / 临时文件清理 / fail-closed）。B 路：真实 git push 到本地 bare 仓，
只验三件靠构造证不了的事（git 确实调到钩子、非 github 目标 exit 0 且没有
python 子进程、钩子非零挡住 push）。

环境注入约定（对应 hooks/pre-push 的解析顺序，全部只用于测试注入）：
  REPO_KEEPER_GATECHECK -> GateCheck.py 绝对路径
  REPO_KEEPER_HOME      -> 配置根（gate-exempt.txt 所在目录）
  REPO_KEEPER_PYTHON    -> python 解释器（可含参数），缺省 py -3
  USERPROFILE / HOME    -> 覆盖，让 python Path.home() 指向临时目录（词表读这里）

假词用 carol；敏感形状是一个邮箱形，域名不在 EMAIL_OK 白名单里，属 block
命中，不依赖真实词表。完整形状在本文件里始终拼接构造、从不整着写出来——
本文件自己也会被发布闸扫到。
"""

import os
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HOOK = REPO / "hooks" / "pre-push"
GATECHECK = REPO / "scripts" / "GateCheck.py"

SAFE_EMAIL = "bob@" + "example.com"
SENSITIVE_EMAIL = "bob@" + "projx.com"
FAKE_WORD = "carol"
ZEROS40 = "0" * 40

GITHUB_URL = "https://github.com/bob/projx.git"
GERRIT_URL = "https://gerrit.company.com/projx"
GITLAB_GITHUB_URL = "https://gitlab.example.com/github/x"


def _git(repo, *args, env=None, check=True):
    full = dict(os.environ)
    if env:
        full.update(env)
    proc = subprocess.run(["git", "-C", str(repo), *args], env=full,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          encoding="utf-8", errors="replace")
    if check and proc.returncode != 0:
        raise AssertionError("git {0} failed: {1}".format(args, proc.stderr.strip()))
    return proc.stdout


def _make_repo(root, name="bob"):
    repo = root / "repo"
    repo.mkdir()
    # 显式钉住分支名：本机 init.defaultBranch 是 master，而用例里引用的是 main
    _git(repo, "init", "-q", "-b", "main")
    empty_hooks = root / "empty-hooks"
    empty_hooks.mkdir()
    _git(repo, "config", "core.hooksPath", str(empty_hooks))
    _git(repo, "config", "user.name", name)
    _git(repo, "config", "user.email", SAFE_EMAIL)
    return repo


def _commit(repo, files, message):
    for rel, content in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


def _sha(repo, ref="HEAD"):
    return _git(repo, "rev-parse", ref).strip()


def _common_hooks_dir(repo):
    """仓库本地钩子目录（绝对路径）。

    必须带 --path-format=absolute：git 打印的 common dir 是相对路径（.git）,
    直接拿来拼会落到当前工作目录去, 而不是这个临时仓里。
    """
    common = Path(_git(repo, "rev-parse", "--path-format=absolute",
                       "--git-common-dir").strip())
    hooks_dir = common / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    return hooks_dir


def _install_local_hook(repo, sentinel):
    """把哨兵装成仓库本地 pre-push, 供接力路径的用例使用。"""
    hooks_dir = _common_hooks_dir(repo)
    dst = hooks_dir / "pre-push"
    dst.write_bytes(sentinel.read_bytes())
    dst.chmod(0o755)
    return hooks_dir


def _bare_refs(bare):
    """列 bare 仓的 ref。

    必须显式给 --git-dir：本机全局 safe.bareRepository 是 explicit,
    git -C 进 bare 仓会被直接拒掉。
    """
    proc = subprocess.run(["git", "--git-dir", str(bare), "for-each-ref"],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise AssertionError("git for-each-ref failed: " + proc.stderr.strip())
    return proc.stdout


def _init_bare(path):
    """建一个本地 bare 仓。

    不能走 _git——它是 git -C <path>, 而这时目录还不存在, git 先在 -C 上就失败了。
    """
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "--bare", "-q")
    return path


def _make_cfg(root):
    """临时配置根：.repo-keeper 下有 audit-words.txt（假词 carol）。"""
    cfg = root / ".repo-keeper"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "audit-words.txt").write_text(FAKE_WORD + "\n", encoding="utf-8")
    return cfg


def _env_for(cfg, extra=None):
    """钩子运行环境：从 os.environ 复制再注入，保住 PATH 等必要变量。"""
    env = dict(os.environ)
    env.update({
        "REPO_KEEPER_GATECHECK": str(GATECHECK),
        "REPO_KEEPER_HOME": str(cfg),
        "USERPROFILE": str(cfg.parent),
        "HOME": str(cfg.parent),
    })
    if extra:
        env.update(extra)
    return env


def _run_hook(cwd, url, stdin_bytes=b"", env=None, remote="origin"):
    # 子进程是字节模式：传 str 会让 subprocess 的 stdin 写线程抛 TypeError 而死掉,
    # communicate() 再去 join 这个已死的线程就永远阻塞, 表现为整轮测试挂死。
    if isinstance(stdin_bytes, str):
        stdin_bytes = stdin_bytes.encode("utf-8")
    proc = subprocess.run(
        ["sh", str(HOOK), remote, url],
        cwd=str(cwd), input=stdin_bytes, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return proc.returncode, proc.stdout, proc.stderr


def _stdin_line(local_ref, local_sha, remote_ref, remote_sha):
    return "{0} {1} {2} {3}\n".format(local_ref, local_sha, remote_ref, remote_sha)


def _new_ref_stdin(sha):
    return _stdin_line("refs/heads/main", sha, "refs/heads/main", ZEROS40)


def _write_script(path, body, mode=0o755):
    """写一个可执行的 sh 哨兵/帮手（测试用，不进仓库）。

    必须走 write_bytes：write_text 在 Windows 上会把换行翻成 CRLF,
    shebang 尾巴上多一个 CR, sh 就起不来了。
    """
    if isinstance(body, str):
        body = body.encode("utf-8")
    path.write_bytes(body)
    path.chmod(mode)
    return path


def _write_canary(path, marker):
    """canary：调起时落标记、必退 97。用来证明 python 是否被启动。"""
    return _write_script(path, (
        "#!/bin/sh\n"
        "echo invoked >> \"{m}\"\n"
        "exit 97\n").format(m=str(marker).replace("\\", "/")))


def _repo_with_leak(root):
    """一个含敏感 blob（EMAIL block）的临时仓，供武装判定测试用。"""
    repo = _make_repo(root)
    sha = _commit(repo, {"notes.txt": "private note " + SENSITIVE_EMAIL}, "add notes")
    return repo, sha


class TestArmDecision(unittest.TestCase):
    def test_https_github_armed_scans_and_blocks(self):
        # github 目标 + 无豁免 -> 武装 -> 扫描 -> 发现泄漏 -> 拦下
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, sha = _repo_with_leak(root)
            cfg = _make_cfg(root)
            rc, out, err = _run_hook(repo, GITHUB_URL, _new_ref_stdin(sha),
                                     env=_env_for(cfg))
            self.assertEqual(rc, 1)
            text = err.decode("utf-8", "replace")
            self.assertIn("E_LEAK_FOUND", text)
            self.assertIn("敏感词", text)

    def test_non_github_not_armed_silent(self):
        # 公司 Gerrit 形状：不含 github -> sh 预筛就放行，不扫描（哪怕仓里有泄漏）。
        # 这个仓没有本地 pre-push，所以放行是静默的；有本地钩子时必须接力，
        # 见 TestRelayPath.test_relay_on_non_github。
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, sha = _repo_with_leak(root)
            cfg = _make_cfg(root)
            rc, out, err = _run_hook(repo, GERRIT_URL, _new_ref_stdin(sha),
                                     env=_env_for(cfg))
            self.assertEqual(rc, 0)
            self.assertEqual(out, b"")
            self.assertEqual(err, b"")

    def test_github_host_not_github_python_decides(self):
        # URL 含 github 但主机不是 github.com：sh 过近似放进第二段，python 权威判
        # not_github -> 不武装 -> exit 0（哪怕仓里有泄漏）
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, sha = _repo_with_leak(root)
            cfg = _make_cfg(root)
            rc, out, err = _run_hook(repo, GITLAB_GITHUB_URL, _new_ref_stdin(sha),
                                     env=_env_for(cfg))
            self.assertEqual(rc, 0)
            self.assertEqual(err, b"")

    def test_sh_prefilter_is_over_approximation(self):
        # 关键证：gitlab.example.com/github/x 这种"含 github 但不是 github 主机"，
        # sh 必须放进第二段而不是放掉。用 canary（必退 97）证明它进到了 arm 段。
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, sha = _repo_with_leak(root)
            cfg = _make_cfg(root)
            marker = root / "canary.called"
            canary = _write_canary(root / "canary.py", marker)
            env = _env_for(cfg, {"REPO_KEEPER_PYTHON": str(canary)})
            rc, out, err = _run_hook(repo, GITLAB_GITHUB_URL, _new_ref_stdin(sha),
                                     env=env)
            self.assertNotEqual(rc, 0)
            self.assertTrue(marker.exists(),
                            "sh 预筛把含 github 的 URL 放掉了（过近似失效）")

    def test_non_github_sh_does_not_start_python(self):
        # 不含 github 的 URL：sh 直接 exit 0，canary 不被调起（进程树里没有 python）
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, sha = _repo_with_leak(root)
            cfg = _make_cfg(root)
            marker = root / "canary.called"
            canary = _write_canary(root / "canary.py", marker)
            env = _env_for(cfg, {"REPO_KEEPER_PYTHON": str(canary)})
            rc, out, err = _run_hook(repo, GERRIT_URL, _new_ref_stdin(sha), env=env)
            self.assertEqual(rc, 0)
            self.assertFalse(marker.exists(), "非 github 目标不该启动 python")

    def test_exempt_fresh_not_armed(self):
        # 豁免命中且新鲜 -> 不武装 -> 静默 exit 0（哪怕仓里有泄漏）
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, sha = _repo_with_leak(root)
            cfg = _make_cfg(root)
            (cfg / "gate-exempt.txt").write_text(
                "github.com/bob/projx " + datetime.now().isoformat() + "\n",
                encoding="utf-8")
            rc, out, err = _run_hook(repo, GITHUB_URL, _new_ref_stdin(sha),
                                     env=_env_for(cfg))
            self.assertEqual(rc, 0)
            self.assertEqual(err, b"")

    def test_exempt_expired_armed(self):
        # 豁免过期（TTL 7 天）-> 视为未豁免 -> 武装 -> 扫描 -> 拦下
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, sha = _repo_with_leak(root)
            cfg = _make_cfg(root)
            old = (datetime.now() - timedelta(days=9)).isoformat()
            (cfg / "gate-exempt.txt").write_text(
                "github.com/bob/projx " + old + "\n", encoding="utf-8")
            rc, out, err = _run_hook(repo, GITHUB_URL, _new_ref_stdin(sha),
                                     env=_env_for(cfg))
            self.assertEqual(rc, 1)
            self.assertIn("E_LEAK_FOUND", err.decode("utf-8", "replace"))


class TestStdinReplay(unittest.TestCase):
    def _sentinel(self, root, outfile, exitcode=0):
        """仓库本地 pre-push 哨兵：把 stdin 原样转储到 outfile，退 exitcode。"""
        return _write_script(root / "local-pre-push", (
            "#!/bin/sh\n"
            "cat > \"{o}\"\n"
            "exit {c}\n").format(o=str(outfile).replace("\\", "/"), c=exitcode))

    def _install_local_hook(self, repo, sentinel):
        return _install_local_hook(repo, sentinel)

    def test_replay_exact_bytes(self):
        # 多行、末尾有换行：哨兵收到的字节与喂给钩子的完全一致
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _make_repo(root)
            sha = _commit(repo, {"a.txt": "clean"}, "first")
            cfg = _make_cfg(root)
            dumped = root / "dumped.bin"
            sentinel = self._sentinel(root, dumped, 0)
            self._install_local_hook(repo, sentinel)
            stdin_bytes = (
                "refs/heads/main {0} refs/heads/main {1}\n"
                "refs/heads/feat {0} refs/heads/feat {1}\n"
            ).format(sha, ZEROS40).encode("utf-8")
            rc, out, err = _run_hook(repo, GITHUB_URL, stdin_bytes,
                                     env=_env_for(cfg))
            self.assertEqual(rc, 0, err)
            self.assertEqual(dumped.read_bytes(), stdin_bytes)

    def test_replay_exact_bytes_no_trailing_newline(self):
        # 末尾没有换行也必须原样
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _make_repo(root)
            sha = _commit(repo, {"a.txt": "clean"}, "first")
            cfg = _make_cfg(root)
            dumped = root / "dumped.bin"
            sentinel = self._sentinel(root, dumped, 0)
            self._install_local_hook(repo, sentinel)
            stdin_bytes = (
                "refs/heads/main {0} refs/heads/main {1}"
            ).format(sha, ZEROS40).encode("utf-8")
            rc, out, err = _run_hook(repo, GITHUB_URL, stdin_bytes,
                                     env=_env_for(cfg))
            self.assertEqual(rc, 0, err)
            self.assertEqual(dumped.read_bytes(), stdin_bytes)


class TestExitCodePropagation(unittest.TestCase):
    def _run_with_sentinel(self, exitcode):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _make_repo(root)
            sha = _commit(repo, {"a.txt": "clean"}, "first")
            cfg = _make_cfg(root)
            dumped = root / "dumped.bin"
            sentinel = _write_script(root / "local-pre-push", (
                "#!/bin/sh\n"
                "cat > \"{o}\"\n"
                "exit {c}\n").format(o=str(dumped).replace("\\", "/"), c=exitcode))
            _install_local_hook(repo, sentinel)
            rc, out, err = _run_hook(repo, GITHUB_URL, _new_ref_stdin(sha),
                                     env=_env_for(cfg))
            return rc

    def test_propagate_exit_3(self):
        self.assertEqual(self._run_with_sentinel(3), 3)

    def test_propagate_exit_1(self):
        self.assertEqual(self._run_with_sentinel(1), 1)

    def test_propagate_exit_0(self):
        self.assertEqual(self._run_with_sentinel(0), 0)


class TestRelayPath(unittest.TestCase):
    def test_no_local_hook_exit_zero(self):
        # 仓库本地 pre-push 不存在 -> 跳过接力，正常退 0 且不报错
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _make_repo(root)
            sha = _commit(repo, {"a.txt": "clean"}, "first")
            cfg = _make_cfg(root)
            rc, out, err = _run_hook(repo, GITHUB_URL, _new_ref_stdin(sha),
                                     env=_env_for(cfg))
            self.assertEqual(rc, 0, err)

    def test_no_self_relay_recursion(self):
        # 本文件同时也是仓库本地 pre-push 时不得自己调自己——会把 push 挂死。
        # 这里必须带 timeout: 守卫失效时表现是永不返回, 不带就是整轮测试挂死。
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _make_repo(root)
            sha = _commit(repo, {"a.txt": "clean"}, "first")
            cfg = _make_cfg(root)
            hooks_dir = _common_hooks_dir(repo)
            (hooks_dir / "pre-push").write_bytes(HOOK.read_bytes())
            (hooks_dir / "pre-push").chmod(0o755)
            proc = subprocess.run(
                ["sh", str(HOOK), "origin", GITHUB_URL], cwd=str(repo),
                input=_new_ref_stdin(sha).encode("utf-8"), env=_env_for(cfg),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stderr)

    def _relay_sentinel(self, root, marker, exitcode=0):
        """仓库本地 pre-push 哨兵：把 stdin 原样转储到 marker，退 exitcode。"""
        return _write_script(root / "local-pre-push", (
            "#!/bin/sh\n"
            "cat > \"{m}\"\n"
            "exit {c}\n").format(m=str(marker).replace("\\", "/"), c=exitcode))

    def _assert_relayed(self, url, exitcode, extra_cfg=None):
        """无条件接力：本闸不管从哪条出口放行，仓库本地 pre-push 都得被跑到。

        core.hooksPath 是"替换"不是"追加"（方案 §D3）——本闸只要装成全局钩子，
        各仓 .git/hooks/pre-push 就再也不会被 git 调起。所以本闸每一条 exit 0
        都必须先接力，否则那条路径上的本地钩子被静默废掉，盘上毫无痕迹。
        顺带验两件事：接力拿到的是原样 stdin，退出码原样传播。
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, sha = _repo_with_leak(root)
            cfg = _make_cfg(root)
            if extra_cfg:
                extra_cfg(cfg)
            marker = root / "relayed-stdin"
            _install_local_hook(repo, self._relay_sentinel(root, marker, exitcode))
            payload = _new_ref_stdin(sha)
            rc, out, err = _run_hook(repo, url, payload, env=_env_for(cfg))
            self.assertTrue(marker.exists(),
                            "这条出口没接力仓库本地 pre-push：" + url)
            self.assertEqual(marker.read_bytes(), payload.encode("utf-8"),
                             "接力给本地钩子的 stdin 不是原样重放")
            self.assertEqual(rc, exitcode, err)

    def test_relay_on_non_github(self):
        # 出口一：不含 github，sh 预筛就放行（python 都没启动）——仍须接力
        self._assert_relayed(GERRIT_URL, 3)

    def test_relay_on_github_in_path_other_host(self):
        # 出口二：含 github 但主机不是 github.com，python 判 not_github——仍须接力
        self._assert_relayed(GITLAB_GITHUB_URL, 4)

    def test_relay_on_fresh_exempt(self):
        # 出口三：豁免命中且新鲜，不武装——仍须接力
        def fresh(cfg):
            (cfg / "gate-exempt.txt").write_text(
                "github.com/bob/projx " + datetime.now().isoformat() + "\n",
                encoding="utf-8")
        self._assert_relayed(GITHUB_URL, 5, extra_cfg=fresh)

    def test_relay_in_linked_worktree(self):
        # linked worktree：本地 pre-push 在 common dir 里（--git-common-dir 的用处）
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _make_repo(root)
            _commit(repo, {"a.txt": "clean"}, "first")
            cfg = _make_cfg(root)
            marker = root / "wt-marker"
            sentinel = _write_script(root / "local-pre-push", (
                "#!/bin/sh\n"
                "echo relayed >> \"{m}\"\n"
                "cat > /dev/null\n"
                "exit 0\n").format(m=str(marker).replace("\\", "/")))
            _install_local_hook(repo, sentinel)
            wt = root / "wt"
            # main 已被主工作树占用, 这里只要一个能跑钩子的 linked worktree,
            # 走 --detach 即可, 不必再占一个分支名。
            _git(repo, "worktree", "add", "-q", "--detach", str(wt), "main")
            sha = _sha(repo, "main")
            rc, out, err = _run_hook(wt, GITHUB_URL, _new_ref_stdin(sha),
                                     env=_env_for(cfg))
            self.assertEqual(rc, 0, err)
            self.assertTrue(marker.exists(),
                            "worktree 里没接力到 common dir 的本地 pre-push")


class TestExitCodeDistinction(unittest.TestCase):
    def test_leak_found_and_gate_unavailable_wording_differ(self):
        # E_LEAK_FOUND：仓里有敏感内容
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, sha = _repo_with_leak(root)
            cfg = _make_cfg(root)
            rc, out, err = _run_hook(repo, GITHUB_URL, _new_ref_stdin(sha),
                                     env=_env_for(cfg))
            self.assertEqual(rc, 1)
            text = err.decode("utf-8", "replace")
            self.assertIn("E_LEAK_FOUND", text)
            self.assertNotIn("E_GATE_UNAVAILABLE", text)
            self.assertIn("发现了敏感词", text)

        # E_GATE_UNAVAILABLE：空词表 -> pre-push 判不了（fail-closed）
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, sha = _repo_with_leak(root)
            cfg = _make_cfg(root)
            (cfg / "audit-words.txt").write_text("", encoding="utf-8")
            rc, out, err = _run_hook(repo, GITHUB_URL, _new_ref_stdin(sha),
                                     env=_env_for(cfg))
            self.assertEqual(rc, 1)
            text = err.decode("utf-8", "replace")
            self.assertIn("E_GATE_UNAVAILABLE", text)
            self.assertNotIn("E_LEAK_FOUND", text)
            self.assertIn("闸没有跑成", text)


class TestTempCleanup(unittest.TestCase):
    def test_no_leftover_temp_files(self):
        # 放行、拦下、出错三条路径跑完，TMPDIR 里没有残留
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tdir = root / "tmpdir"
            tdir.mkdir()
            repo, sha = _repo_with_leak(root)
            cfg = _make_cfg(root)
            env = _env_for(cfg, {"TMPDIR": str(tdir).replace("\\", "/")})
            # 非 github -> exit 0
            rc, _, _ = _run_hook(repo, GERRIT_URL, _new_ref_stdin(sha), env=env)
            self.assertEqual(rc, 0)
            # github + 泄漏 -> 拦下
            rc, _, _ = _run_hook(repo, GITHUB_URL, _new_ref_stdin(sha), env=env)
            self.assertEqual(rc, 1)
            # 空词表 -> 闸没跑成
            (cfg / "audit-words.txt").write_text("", encoding="utf-8")
            rc, _, _ = _run_hook(repo, GITHUB_URL, _new_ref_stdin(sha), env=env)
            self.assertNotEqual(rc, 0)
            self.assertEqual(list(tdir.iterdir()), [])


class TestFailClosed(unittest.TestCase):
    def _cfg_and_repo(self, root):
        repo, sha = _repo_with_leak(root)
        cfg = _make_cfg(root)
        return repo, sha, cfg

    def test_python_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, sha, cfg = self._cfg_and_repo(root)
            env = _env_for(cfg, {"REPO_KEEPER_PYTHON": str(root / "no-such-python")})
            rc, out, err = _run_hook(repo, GITHUB_URL, _new_ref_stdin(sha), env=env)
            self.assertNotEqual(rc, 0)

    def test_gatecheck_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, sha, cfg = self._cfg_and_repo(root)
            env = _env_for(cfg, {"REPO_KEEPER_GATECHECK": str(root / "no-gatecheck.py")})
            rc, out, err = _run_hook(repo, GITHUB_URL, _new_ref_stdin(sha), env=env)
            self.assertNotEqual(rc, 0)

    def test_arm_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, sha, cfg = self._cfg_and_repo(root)
            marker = root / "canary.called"
            canary = _write_canary(root / "canary.py", marker)
            env = _env_for(cfg, {"REPO_KEEPER_PYTHON": str(canary)})
            rc, out, err = _run_hook(repo, GITHUB_URL, _new_ref_stdin(sha), env=env)
            self.assertNotEqual(rc, 0)

    def test_arm_output_unparseable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, sha, cfg = self._cfg_and_repo(root)
            canary = _write_script(root / "canary.py",
                                   "#!/bin/sh\necho not-json-at-all\nexit 0\n")
            env = _env_for(cfg, {"REPO_KEEPER_PYTHON": str(canary)})
            rc, out, err = _run_hook(repo, GITHUB_URL, _new_ref_stdin(sha), env=env)
            self.assertNotEqual(rc, 0)

    def test_stdin_cache_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, sha, cfg = self._cfg_and_repo(root)
            env = _env_for(cfg, {"TMPDIR": "/nonexistent-dir-for-mktemp"})
            rc, out, err = _run_hook(repo, GITHUB_URL, _new_ref_stdin(sha), env=env)
            self.assertNotEqual(rc, 0)

    def test_git_common_dir_failure(self):
        # 不在任何 git 仓库里：github 目标 + 无豁免（武装）-> git rev-parse 失败 -> 拒绝
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plain = root / "not-a-repo"
            plain.mkdir()
            cfg = _make_cfg(root)
            rc, out, err = _run_hook(plain, GITHUB_URL, _new_ref_stdin("a" * 40),
                                     env=_env_for(cfg))
            self.assertNotEqual(rc, 0)

    def test_common_dir_failure_blocks_even_non_github(self):
        # 连"本闸根本不管"的非 github 目标也拒绝：定不出本地 pre-push 在哪，
        # 就无法保证接力，放行等于可能把本地钩子静默吞掉。
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plain = root / "not-a-repo"
            plain.mkdir()
            cfg = _make_cfg(root)
            rc, out, err = _run_hook(plain, GERRIT_URL, _new_ref_stdin("a" * 40),
                                     env=_env_for(cfg))
            self.assertNotEqual(rc, 0)


class TestRealPush(unittest.TestCase):
    def _work_repo(self, root):
        work = root / "work"
        work.mkdir()
        _git(work, "init", "-q")
        _git(work, "config", "user.name", "bob")
        _git(work, "config", "user.email", SAFE_EMAIL)
        return work

    def _push_env(self, root, extra=None):
        env = dict(os.environ)
        env.update({
            "REPO_KEEPER_GATECHECK": str(GATECHECK),
            "REPO_KEEPER_HOME": str(root / "cfg"),
        })
        if extra:
            env.update(extra)
        return env

    def _install_hook(self, hooks_dir, body):
        """装到 core.hooksPath 指的那个目录本身, 不要再往下套一层 hooks。"""
        hooks_dir.mkdir(parents=True, exist_ok=True)
        h = hooks_dir / "pre-push"
        if isinstance(body, str):
            body = body.encode("utf-8")
        h.write_bytes(body)
        h.chmod(0o755)
        return h

    def test_git_invokes_hook(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = self._work_repo(root)
            _commit(work, {"a.txt": "hello"}, "first")
            bare = root / "mirror.git"
            _init_bare(bare)
            marker = root / "hook-ran"
            hook_path = str(HOOK).replace("\\", "/")
            wrapper = ("#!/bin/sh\n"
                       "echo invoked >> \"{m}\"\n"
                       "exec \"{h}\" \"$@\"\n").format(
                m=str(marker).replace("\\", "/"), h=hook_path)
            self._install_hook(root / "hooksdir", wrapper)
            _git(work, "config", "core.hooksPath", str(root / "hooksdir"))
            env = self._push_env(root)
            proc = subprocess.run(
                ["git", "-C", str(work), "push", str(bare), "HEAD:refs/heads/main"],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                encoding="utf-8", errors="replace")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(marker.exists(), "git 没调到 pre-push 钩子")

    def test_non_github_no_python(self):
        # 非 github 目标：push 成功，且 canary（必退 97）根本没被调起
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = self._work_repo(root)
            _commit(work, {"a.txt": "hello"}, "first")
            bare = root / "mirror.git"
            _init_bare(bare)
            cfg = root / "cfg"
            cfg.mkdir()
            (cfg / "audit-words.txt").write_text(FAKE_WORD + "\n", encoding="utf-8")
            canary_marker = root / "canary.called"
            canary = _write_canary(root / "canary.py", canary_marker)
            real_hook = HOOK.read_bytes()
            self._install_hook(root / "hooksdir", real_hook.decode("utf-8"))
            _git(work, "config", "core.hooksPath", str(root / "hooksdir"))
            env = self._push_env(root, {"REPO_KEEPER_PYTHON": str(canary)})
            proc = subprocess.run(
                ["git", "-C", str(work), "push", str(bare), "HEAD:refs/heads/main"],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                encoding="utf-8", errors="replace")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertFalse(canary_marker.exists(),
                             "非 github 目标的 push 不该启动 python")

    def test_hook_nonzero_blocks_push(self):
        # 目标路径里含 "github" -> 钩子进第二段 -> canary 失败 -> 拒绝 -> ref 未动
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = self._work_repo(root)
            _commit(work, {"a.txt": "hello"}, "first")
            bare = root / "github-mirror.git"
            _init_bare(bare)
            cfg = root / "cfg"
            cfg.mkdir()
            canary_marker = root / "canary.called"
            canary = _write_canary(root / "canary.py", canary_marker)
            real_hook = HOOK.read_bytes()
            self._install_hook(root / "hooksdir", real_hook.decode("utf-8"))
            _git(work, "config", "core.hooksPath", str(root / "hooksdir"))
            env = self._push_env(root, {"REPO_KEEPER_PYTHON": str(canary)})
            proc = subprocess.run(
                ["git", "-C", str(work), "push", str(bare), "HEAD:refs/heads/main"],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                encoding="utf-8", errors="replace")
            self.assertNotEqual(proc.returncode, 0)
            self.assertTrue(canary_marker.exists(),
                            "含 github 的目标应该启动 python（canary）")
            refs = _bare_refs(bare).strip()
            self.assertEqual(refs, "", "被拦下的 push 不该移动远端 ref")


if __name__ == "__main__":
    unittest.main()
