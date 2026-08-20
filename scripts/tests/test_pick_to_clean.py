#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_pick_to_clean.py — PickToClean 的 CLI-AI 机器通道 + --dry-run 测试。

复用 test_clean_branch.py 的 FakeRepo fixture（同一包，同一份模块实例），
BaseCleanBranchTest.setUp 已注入 CleanBranch 模块设置与 _WT_CACHE。
"""
import io
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import CleanBranch as cb  # noqa: E402
import PickToClean  # noqa: E402
import local_config  # noqa: E402
from tests.test_clean_branch import (BaseCleanBranchTest, FakeRepo,  # noqa: E402
                                     EMPTY_CFG)


class PickToCleanCliTests(BaseCleanBranchTest):
    """PickToClean 的 CLI-AI 机器通道（信封 / 退出码 / eager ai-help / --dry-run）。"""

    def run_main(self, *argv, cfg=EMPTY_CFG):
        out, err = io.StringIO(), io.StringIO()
        code = PickToClean.main(list(argv), cfg=cfg,
                                sinks=PickToClean.cc.Sinks(out=out, err=err))
        return code, out.getvalue(), err.getvalue()

    def _clean(self, *args, **kw):
        return self.repo.git(FakeRepo.CLEAN, *args, **kw)

    def _clean_head(self):
        return self._clean("rev-parse", "HEAD").stdout.strip()

    def _cfg_with_identity(self, name, email):
        data = {"identity": {"clean": {"name": name, "email": email}}}
        merged, origins = local_config.merge_layers([(local_config.GLOBAL, data)])
        return local_config.Config(
            merged, origins,
            {local_config.GLOBAL: None, local_config.PROJECT: None})

    def test_json_success_envelope(self):
        src = self.repo.commit(FakeRepo.WORK, {"App/Code/x.c": b"int a;\n"}, "add x")
        code, out, err = self.run_main(src, "--json")
        self.assertEqual(code, 0)
        obj = json.loads(out)
        self.assertTrue(obj["ok"])
        self.assertIsNone(obj["error"])
        self.assertEqual(obj["data"]["action"], "pick")
        head = self._clean("rev-parse", "--short", "HEAD").stdout.strip()
        self.assertEqual(obj["data"]["picked"], [{"src": src, "hash": head}])
        self.assertEqual(obj["data"]["skipped"], [])
        self.assertEqual(err, "")

    def test_ai_help_eager(self):
        code, out, err = self.run_main("--contract-test-invalid", "--ai-help")
        self.assertEqual(code, 0)
        self.assertIn("name: PickToClean", out)
        self.assertEqual(err, "")

    def test_bad_arg_json_envelope(self):
        code, out, err = self.run_main("--contract-test-invalid", "--json")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertEqual(json.loads(err)["error"]["code"], "E_VALIDATION")

    def test_empty_commits_human_shows_docstring(self):
        code, out, err = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn("PickToClean.py", err)

    def test_empty_commits_json_is_validation_rc2(self):
        code, out, err = self.run_main("--json")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertEqual(json.loads(err)["error"]["code"], "E_VALIDATION")

    def test_identity_mismatch_is_validation_rc2(self):
        src = self.repo.commit(FakeRepo.WORK, {"App/Code/x.c": b"int a;\n"}, "add x")
        cfg = self._cfg_with_identity("someone-else", "else@example.com")
        before = self._clean_head()
        code, out, err = self.run_main(src, "--json", cfg=cfg)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        obj = json.loads(err)
        self.assertEqual(obj["error"]["code"], "E_VALIDATION")
        self.assertEqual(obj["error"]["details"]["state"], "identity_mismatch")
        self.assertEqual(self._clean_head(), before)

    def test_cherry_pick_in_progress_is_validation_rc2(self):
        self.repo.commit(FakeRepo.CLEAN, {"App/Code/c.c": b"base\n"}, "base c")
        self.repo.commit(FakeRepo.WORK, {"App/Code/c.c": b"base\n"}, "base c")
        self.repo.commit(FakeRepo.CLEAN, {"App/Code/c.c": b"CLEAN\n"}, "clean change")
        src = self.repo.commit(FakeRepo.WORK, {"App/Code/c.c": b"WORK\n"}, "work change")
        self.run_main(src)   # 制造冲突现场（CHERRY_PICK_HEAD 留下）
        code, out, err = self.run_main(src, "--json")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        obj = json.loads(err)
        self.assertEqual(obj["error"]["code"], "E_VALIDATION")
        self.assertEqual(obj["error"]["details"]["state"], "cherry_pick_in_progress")

    def test_git_failure_is_external_tool_rc1(self):
        src = self.repo.commit(FakeRepo.WORK, {"App/Code/x.c": b"int a;\n"}, "add x")

        def boom(wt, *args):
            raise PickToClean.cc.ExternalToolError(
                "E_EXTERNAL_TOOL", "git failed", details={"tool": "git"})

        orig_out = PickToClean._out
        PickToClean._out = boom
        try:
            code, out, err = self.run_main(src, "--json")
        finally:
            PickToClean._out = orig_out
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        obj = json.loads(err)
        self.assertEqual(obj["error"]["code"], "E_EXTERNAL_TOOL")
        self.assertEqual(obj["error"]["details"]["tool"], "git")

    def test_dry_run_json_marks_dry_run_and_does_not_mutate(self):
        src = self.repo.commit(FakeRepo.WORK, {"App/Code/x.c": b"int a;\n"}, "add x")
        before = self._clean_head()
        code, out, err = self.run_main(src, "--dry-run", "--json")
        self.assertEqual(code, 0)
        obj = json.loads(out)
        self.assertTrue(obj["ok"])
        self.assertTrue(obj["data"]["dry_run"])
        self.assertEqual(obj["data"]["picked"], [{"src": src, "hash": None}])
        self.assertEqual(obj["data"]["skipped"], [])
        self.assertEqual(self._clean_head(), before)
        self.assertEqual(self._clean("status", "--porcelain").stdout.strip(), "")

    def test_dry_run_json_skips_commit_already_on_clean(self):
        self.repo.commit(FakeRepo.CLEAN, {"App/Code/y.c": b"int y;\n"}, "base y (clean)")
        self.repo.commit(FakeRepo.WORK, {"App/Code/y.c": b"int y;\n"}, "base y (work)")
        src = self.repo.commit(FakeRepo.WORK, {"App/Code/y.c": b"int y = 1;\n"}, "bump y")
        self.repo.commit(FakeRepo.CLEAN, {"App/Code/y.c": b"int y = 1;\n"}, "同样的改动")
        before = self._clean_head()
        code, out, err = self.run_main(src, "--dry-run", "--json")
        self.assertEqual(code, 0)
        obj = json.loads(out)
        self.assertTrue(obj["data"]["dry_run"])
        self.assertEqual(obj["data"]["picked"], [])
        self.assertEqual(obj["data"]["skipped"], [{"src": src}])
        self.assertEqual(self._clean_head(), before)
        self.assertEqual(self._clean("status", "--porcelain").stdout.strip(), "")

    def test_dry_run_human_says_not_landed(self):
        src = self.repo.commit(FakeRepo.WORK, {"App/Code/x.c": b"int a;\n"}, "add x")
        before = self._clean_head()
        code, out, err = self.run_main(src, "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("would pick", out)
        self.assertIn("未落盘", out)
        self.assertEqual(self._clean_head(), before)

    def test_conflict_is_external_tool_rc1(self):
        self.repo.commit(FakeRepo.CLEAN, {"App/Code/c.c": b"base\n"}, "base c")
        self.repo.commit(FakeRepo.WORK, {"App/Code/c.c": b"base\n"}, "base c")
        self.repo.commit(FakeRepo.CLEAN, {"App/Code/c.c": b"CLEAN\n"}, "clean change")
        src = self.repo.commit(FakeRepo.WORK, {"App/Code/c.c": b"WORK\n"}, "work change")
        before = self._clean_head()
        code, out, err = self.run_main(src, "--json")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        obj = json.loads(err)
        self.assertEqual(obj["error"]["code"], "E_EXTERNAL_TOOL")
        self.assertEqual(obj["error"]["details"]["state"], "conflict")
        self.assertIn("cherry-pick", obj["error"]["details"]["guidance"])
        self.assertEqual(self._clean_head(), before)
        self.assertNotEqual(self._clean("ls-files", "-u").stdout.strip(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
