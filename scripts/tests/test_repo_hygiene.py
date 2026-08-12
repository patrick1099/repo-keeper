import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import RepoHygiene as rh
from toolname import PROJECT_CONFIG_NAME, REANCHOR_EXE


def _git(args, cwd):
    subprocess.run(['git'] + args, cwd=str(cwd), check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def make_repo(tmp, files, gitignore=None, commit=True):
    """A throwaway repo with ``files`` committed. Returns the root Path."""
    root = Path(tmp)
    _git(['init', '-q'], root)
    _git(['config', 'user.email', 't@t'], root)
    _git(['config', 'user.name', 't'], root)
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        # write_bytes, not write_text: on Windows the text path turns '\n' into
        # '\r\n', which makes every fixture file a CRLF-worktree/LF-index case
        # and quietly changes what these tests are testing.
        p.write_bytes(text.encode('utf-8'))
    if gitignore is not None:
        (root / '.gitignore').write_bytes(gitignore.encode('utf-8'))
    if commit:
        _git(['add', '-A'], root)
        _git(['commit', '-qm', 'init'], root)
    return root


class TestPatternTranslation(unittest.TestCase):
    def m(self, pattern, path):
        return bool(rh._translate(pattern).match(path))

    def test_directory_rule_covers_contents_at_any_depth(self):
        self.assertTrue(self.m('Objects/', 'Objects'))
        self.assertTrue(self.m('Objects/', 'Code/App/Proj/Release/Objects/x.d'))

    def test_bare_name_matches_at_any_depth(self):
        self.assertTrue(self.m('.clangd', 'Code/App/.clangd'))
        self.assertTrue(self.m('*.uvguix.*', 'Code/App/Proj/P.uvguix.developer'))

    def test_pattern_with_slash_is_root_anchored(self):
        # git's rule: a slash anywhere in the pattern anchors it. Getting this
        # backwards makes a path rule match a same-named dir deep in the tree.
        self.assertTrue(self.m('Code/App/x.c', 'Code/App/x.c'))
        self.assertFalse(self.m('Code/App/x.c', 'sub/Code/App/x.c'))

    def test_star_does_not_cross_a_slash(self):
        self.assertFalse(self.m('Proj/*.map', 'Proj/sub/a.map'))

    def test_doublestar_crosses_slashes(self):
        self.assertTrue(self.m('RTE/**/RTE_Components.h',
                               'RTE/_Debug/RTE_Components.h'))

    def test_dollar_in_office_lock_pattern_is_literal(self):
        self.assertTrue(self.m('~$*', 'Doc/~$说明书.docx'))
        self.assertFalse(self.m('~$*', 'Doc/说明书.docx'))


class TestMatcherNegation(unittest.TestCase):
    def test_last_match_wins(self):
        m = rh.Matcher(['*.exe', '!keep.exe'])
        self.assertIsNone(m.match('keep.exe'))
        self.assertEqual(m.match('other.exe'), '*.exe')

    def test_negation_before_the_rule_is_cancelled(self):
        # This is exactly why the generated file puts negations last.
        m = rh.Matcher(['!keep.exe', '*.exe'])
        self.assertEqual(m.match('keep.exe'), '*.exe')

    def test_shipped_ruleset_keeps_the_reanchor_exe(self):
        m = rh.Matcher(rh.all_ignore_patterns())
        self.assertIsNone(m.match(REANCHOR_EXE))

    def test_shipped_ruleset_hides_our_own_project_config(self):
        # It holds branch refs, whitelists and the reasons behind deliberate
        # divergence -- private notes about a shared repo. Offering it up for
        # commit even once is the failure mode worth a test.
        m = rh.Matcher(rh.all_ignore_patterns())
        self.assertIsNotNone(m.match(PROJECT_CONFIG_NAME))


class TestPlanClassification(unittest.TestCase):
    def test_untracked_generated_files_are_ignorable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp, {'main.c': 'int main(void){return 0;}\n'})
            (root / 'Code').mkdir()
            (root / 'Code' / '.clangd').write_text('x', encoding='utf-8')
            (root / 'build.log').write_text('x', encoding='utf-8')
            plan = rh.build_plan(rh.RepoState(root))
            self.assertIn('Code/.clangd', plan.newly_ignored)
            self.assertIn('build.log', plan.newly_ignored)

    def test_real_source_is_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp, {'main.c': 'x\n'})
            (root / 'new_feature.c').write_text('x', encoding='utf-8')
            plan = rh.build_plan(rh.RepoState(root))
            self.assertIn('new_feature.c', plan.still_noisy)
            self.assertNotIn('new_feature.c', plan.newly_ignored)

    def test_freeze_covers_every_tracked_match_not_just_dirty_ones(self):
        # The point of freezing *.uvoptx is that it never comes back, not that
        # it stops bothering you until the next time you switch target.
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp, {
                'Proj/A.uvoptx': '<x/>\n',
                'Proj/B.uvoptx': '<x/>\n',
                'Proj/RTE/_T/RTE_Components.h': '#define X\n',
            })
            (root / 'Proj' / 'A.uvoptx').write_bytes(b'<y/>\n')
            plan = rh.build_plan(rh.RepoState(root))
            frozen = [p for p, _ in plan.freeze]
            self.assertEqual(sorted(frozen), ['Proj/A.uvoptx', 'Proj/B.uvoptx',
                                              'Proj/RTE/_T/RTE_Components.h'])
            self.assertEqual(plan.freeze_dirty, ['Proj/A.uvoptx'])

    def test_uvprojx_is_never_frozen(self):
        # The project file carries the real source list and macros; freezing it
        # would hide changes that must be committed.
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp, {'Proj/P.uvprojx': '<x/>\n'})
            plan = rh.build_plan(rh.RepoState(root))
            self.assertEqual(plan.freeze, [])

    def test_tracked_binary_goes_to_review_not_freeze(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp, {'Release/Useful/fw.bin': 'aa\n'})
            (root / 'Release' / 'Useful' / 'fw.bin').write_text('bb\n',
                                                                encoding='utf-8')
            plan = rh.build_plan(rh.RepoState(root))
            self.assertEqual([p for p, _ in plan.review],
                             ['Release/Useful/fw.bin'])
            self.assertEqual(plan.freeze, [])

    def test_rule_shadowed_by_a_tracked_file_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp, {'Code/App/Proj/.clangd': 'a\n'})
            (root / 'Code' / 'App' / 'Proj' / '.clangd').write_text(
                'b\n', encoding='utf-8')
            plan = rh.build_plan(rh.RepoState(root))
            self.assertIn(('.clangd', 'Code/App/Proj/.clangd'), plan.shadowed)


class TestOldGitignoreHandling(unittest.TestCase):
    def build(self, gitignore):
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: None)
        root = make_repo(tmp, {'main.c': 'x\n'}, gitignore=gitignore)
        return rh.build_plan(rh.RepoState(root))

    def test_dead_literal_path_covered_by_a_wildcard_is_absorbed(self):
        plan = self.build("Code/App/Proj/Release/Objects/\n*.o\n")
        self.assertIn('*.o', plan.absorbed)
        self.assertIn('Code/App/Proj/Release/Objects/', plan.absorbed)

    def test_uncovered_line_is_carried_verbatim(self):
        plan = self.build("Doc/内部资料/\n")
        self.assertIn('Doc/内部资料/', plan.carried)

    def test_dangerous_lines_are_flagged_and_dropped(self):
        plan = self.build("*.py\nCMakeLists.txt\n*.0\n")
        flagged = [line for line, _why in plan.dangerous]
        self.assertEqual(sorted(flagged), sorted(['*.py', 'CMakeLists.txt', '*.0']))
        # Compare whole rules, not substrings -- '*.py' lives inside '*.pyc'.
        rules = {l.split('#')[0].strip()
                 for l in rh.render_gitignore(plan).splitlines()}
        self.assertNotIn('*.py', rules)
        self.assertNotIn('CMakeLists.txt', rules)
        self.assertIn('*.pyc', rules)

    def test_dead_uncovered_path_is_reported_but_still_kept(self):
        plan = self.build("Doc/gone/\n")
        self.assertIn('Doc/gone/', plan.dead)
        self.assertIn('Doc/gone/', plan.carried)


class TestRender(unittest.TestCase):
    def plan_for(self, gitignore=''):
        tmp = tempfile.mkdtemp()
        root = make_repo(tmp, {'main.c': 'x\n'}, gitignore=gitignore)
        return rh.build_plan(rh.RepoState(root))

    def test_negations_sit_below_the_manual_block(self):
        plan = self.plan_for("*.exe\n")
        text = rh.render_gitignore(plan)
        self.assertLess(text.index(rh.MANUAL_MARKER),
                        text.index('!' + REANCHOR_EXE))

    def test_rendered_file_actually_keeps_the_exe(self):
        plan = self.plan_for("*.exe\n")
        lines = [l.split('#')[0].strip()
                 for l in rh.render_gitignore(plan).splitlines()]
        m = rh.Matcher([l for l in lines if l])
        self.assertIsNone(m.match(REANCHOR_EXE))
        self.assertEqual(m.match('setup.exe'), '*.exe')

    def test_no_rule_carries_an_inline_comment(self):
        # .gitignore has no inline comments: a '#' outside column one is a
        # literal character in the pattern. A rule written `Objects/  # 输出`
        # matches nothing, and git never says so.
        plan = self.plan_for()
        for line in rh.render_gitignore(plan).splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith('#'):
                continue
            self.assertNotIn('#', stripped, "inline comment in rule: " + line)

    def test_every_generated_rule_is_preceded_by_a_reason(self):
        # Only the generated block: lines carried over from a hand-written
        # file keep whatever shape they had.
        plan = self.plan_for()
        # The reason belongs to a group, so it heads a run of rules; a blank
        # line ends the run.
        generated = rh.render_gitignore(plan).split(rh.MANUAL_MARKER)[0]
        documented = False
        for line in generated.splitlines():
            stripped = line.strip()
            if not stripped:
                documented = False
            elif stripped.startswith('#'):
                documented = True
            else:
                self.assertTrue(documented,
                                "rule with no reason above it: " + line)

    def test_git_itself_confirms_the_rules_bite(self):
        # The authoritative check: our matcher decides the plan, git decides
        # reality, and they have disagreed before.
        tmp = tempfile.mkdtemp()
        root = make_repo(tmp, {'main.c': 'x\n'})
        noise = ['Code/App/Proj/.clangd',
                 'Code/App/Proj/compile_commands.json',
                 'Code/App/Proj/Release/Objects/a.d',
                 'Code/App/Proj/P.uvguix.developer',
                 'Doc/~$说明书.docx',
                 'Code/Boot/build_boot.log']
        for rel in noise:
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text('x', encoding='utf-8')
        (root / 'real_source.c').write_text('x', encoding='utf-8')

        plan = rh.build_plan(rh.RepoState(root))
        rh.write_ignore(plan, shared=True)
        self.assertEqual(rh.verify_with_git(plan), [])

        # Only the real source is left -- plus the freshly written .gitignore,
        # which is untracked because this repo has not committed it yet.
        after = rh.RepoState(root)
        self.assertEqual(after.untracked, ['.gitignore', 'real_source.c'])

    def test_gitattributes_rules_are_accepted_by_git(self):
        # .gitattributes has the same no-inline-comment trap as .gitignore,
        # except git parses the trailing '# ...' as an attribute NAME.
        tmp = tempfile.mkdtemp()
        root = make_repo(tmp, {'Proj/P.uvprojx': '<x/>\n'})
        plan = rh.build_plan(rh.RepoState(root))
        self.assertEqual(plan.renormalize, [])  # no phantom right now...
        self.assertTrue(rh.write_gitattributes(plan))  # ...still written

        proc = subprocess.run(
            ['git', 'check-attr', 'text', '--', 'Proj/P.uvprojx'],
            cwd=str(root), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out = proc.stdout.decode('utf-8', 'replace')
        self.assertNotIn('not a valid attribute name', out)
        self.assertIn('text: unset', out)

    def test_the_exe_survives_a_hand_written_star_exe(self):
        tmp = tempfile.mkdtemp()
        root = make_repo(tmp, {'main.c': 'x\n'}, gitignore="*.exe\n")
        (root / REANCHOR_EXE).write_bytes(b'MZ')
        (root / 'setup.exe').write_bytes(b'MZ')
        rh.write_ignore(rh.build_plan(rh.RepoState(root)), shared=True)

        untracked = rh.RepoState(root).untracked
        self.assertIn(REANCHOR_EXE, untracked)
        self.assertNotIn('setup.exe', untracked)

    def test_regeneration_is_idempotent_and_keeps_the_manual_block(self):
        tmp = tempfile.mkdtemp()
        root = make_repo(tmp, {'main.c': 'x\n'}, gitignore="Doc/私有/\n")
        plan = rh.build_plan(rh.RepoState(root))
        rh.write_ignore(plan, shared=True)
        first = (root / '.gitignore').read_text(encoding='utf-8')
        self.assertIn('Doc/私有/', first)

        (root / '.gitignore').write_text(
            first.rstrip('\n') + '\n手工加的/\n', encoding='utf-8')
        plan2 = rh.build_plan(rh.RepoState(root))
        second = rh.render_gitignore(plan2)
        # Both the original carried line and the hand-added one survive, and
        # the generated block did not duplicate itself.
        self.assertIn('Doc/私有/', second)
        self.assertIn('手工加的/', second)
        self.assertEqual(second.count(rh.GENERATED_BANNER), 1)
        self.assertEqual(second.count(rh.MANUAL_MARKER), 1)
        self.assertEqual(second.count(rh.NEGATION_MARKER), 1)
        self.assertEqual(second.count('!' + REANCHOR_EXE), 1)

    def test_a_rule_appended_at_end_of_file_is_rescued(self):
        # Appending at EOF is the most natural gesture there is, and EOF is
        # below the negation block. Dropping it would be silent data loss.
        tmp = tempfile.mkdtemp()
        root = make_repo(tmp, {'main.c': 'x\n'}, gitignore='')
        rh.write_ignore(rh.build_plan(rh.RepoState(root)), shared=True)
        path = root / '.gitignore'
        path.write_text(path.read_text(encoding='utf-8') + '追加在最后/\n',
                        encoding='utf-8')

        text = rh.render_gitignore(rh.build_plan(rh.RepoState(root)))
        self.assertIn('追加在最后/', text)
        self.assertLess(text.index('追加在最后/'), text.index(rh.NEGATION_MARKER))


class TestLocalMode(unittest.TestCase):
    """The default: fix everything without touching a tracked file."""

    def test_local_writes_info_exclude_and_leaves_gitignore_alone(self):
        tmp = tempfile.mkdtemp()
        root = make_repo(tmp, {'main.c': 'x\n'}, gitignore="*.o\n")
        (root / 'Code').mkdir()
        (root / 'Code' / '.clangd').write_text('x', encoding='utf-8')

        plan = rh.build_plan(rh.RepoState(root))
        rh.write_ignore(plan)                       # local is the default

        self.assertEqual((root / '.gitignore').read_text(encoding='utf-8'),
                         "*.o\n")
        self.assertFalse((root / '.gitignore.bak').exists())
        exclude = (root / '.git' / 'info' / 'exclude').read_text(encoding='utf-8')
        self.assertIn('.clangd', exclude)
        # and the repo has no pending change at all
        self.assertEqual(rh.RepoState(root).modified, set())
        self.assertEqual(rh.RepoState(root).untracked, [])

    def test_local_run_keeps_whatever_was_in_info_exclude(self):
        tmp = tempfile.mkdtemp()
        root = make_repo(tmp, {'main.c': 'x\n'})
        excl = root / '.git' / 'info' / 'exclude'
        excl.write_text("# git 自带的注释\n我自己加的/\n", encoding='utf-8')

        rh.write_ignore(rh.build_plan(rh.RepoState(root)))
        text = excl.read_text(encoding='utf-8')
        self.assertIn('我自己加的/', text)
        self.assertIn('# git 自带的注释', text)

        # re-running does not stack the generated block
        rh.write_ignore(rh.build_plan(rh.RepoState(root)))
        text2 = excl.read_text(encoding='utf-8')
        self.assertEqual(text2.count(rh.GENERATED_BANNER), 1)
        self.assertEqual(text2.count('我自己加的/'), 1)

    def test_a_block_from_the_old_tool_name_is_replaced_not_kept(self):
        # The banner carries the tool's name, and the tool has been renamed
        # (keil2clangd -> repo-keeper). Matching only the current spelling made
        # the old block read as hand-written content: it was preserved and a
        # second block appended below it, ~100 duplicated lines under a banner
        # that says regenerating overwrites everything.
        tmp = tempfile.mkdtemp()
        root = make_repo(tmp, {'main.c': 'x\n'})
        excl = root / '.git' / 'info' / 'exclude'
        old_banner = "# 以下规则由 keil2clangd 的 repo-hygiene 生成 —— 重新生成会整体覆盖。"
        excl.write_text(
            "我自己加的/\n"
            + "# " + "=" * 74 + "\n"
            + old_banner + "\n"
            + "# " + "=" * 74 + "\n\n"
            + "*.old-junk\n",
            encoding='utf-8')

        rh.write_ignore(rh.build_plan(rh.RepoState(root)))
        text = excl.read_text(encoding='utf-8')

        self.assertNotIn(old_banner, text)
        self.assertNotIn('*.old-junk', text)
        self.assertEqual(text.count(rh.GENERATED_BANNER), 1)
        self.assertEqual(text.count('我自己加的/'), 1)

    def test_local_emits_no_negation_because_it_would_lose(self):
        # A repo .gitignore outranks .git/info/exclude, so `!x` here is a lie.
        tmp = tempfile.mkdtemp()
        root = make_repo(tmp, {'main.c': 'x\n'}, gitignore="*.exe\n")
        plan = rh.build_plan(rh.RepoState(root))
        text = rh.render_rule_block(plan, shared=False)
        self.assertNotIn('!' + REANCHOR_EXE, text)
        self.assertIn('!' + REANCHOR_EXE,
                      rh.render_rule_block(plan, shared=True))

    def test_local_attributes_append_never_clobber(self):
        # This file is where clean/smudge filters live; overwriting one breaks
        # it with no visible symptom until wrong bytes are committed.
        tmp = tempfile.mkdtemp()
        root = make_repo(tmp, {'Proj/P.uvprojx': '<x/>\n'})
        attrs = root / '.git' / 'info' / 'attributes'
        attrs.write_text("*.c filter=my-private-filter\n", encoding='utf-8')

        rh.write_gitattributes(rh.build_plan(rh.RepoState(root)))
        text = attrs.read_text(encoding='utf-8')
        self.assertIn('*.c filter=my-private-filter', text)
        self.assertIn('*.uvprojx -text', text)
        self.assertFalse((root / '.gitattributes').exists())

    def test_text_attribute_is_skipped_when_it_would_invent_a_diff(self):
        # A file committed under core.autocrlf keeps CRLF on disk and LF in the
        # index. Turning normalisation off there does not reveal a phantom --
        # it manufactures a real diff, and renormalize would commit the CRLF.
        tmp = tempfile.mkdtemp()
        root = Path(tmp)
        _git(['init', '-q'], root)
        _git(['config', 'user.email', 't@t'], root)
        _git(['config', 'user.name', 't'], root)
        _git(['config', 'core.autocrlf', 'true'], root)
        (root / 'Proj').mkdir()
        (root / 'Proj' / 'P.uvprojx').write_bytes(b'<x/>\r\n')
        _git(['add', '-A'], root)
        _git(['commit', '-qm', 'init'], root)
        (root / 'Proj' / 'P.uvprojx').write_bytes(b'<x/>\r\n')  # still CRLF

        plan = rh.build_plan(rh.RepoState(root))
        self.assertFalse(rh.write_gitattributes(plan))
        self.assertFalse((root / '.git' / 'info' / 'attributes').exists())
        self.assertEqual(rh.RepoState(root).modified, set())

    def test_renormalize_undoes_anything_it_accidentally_stages(self):
        tmp = tempfile.mkdtemp()
        root = Path(tmp)
        _git(['init', '-q'], root)
        _git(['config', 'user.email', 't@t'], root)
        _git(['config', 'user.name', 't'], root)
        _git(['config', 'core.autocrlf', 'true'], root)
        (root / 'Proj').mkdir()
        (root / 'Proj' / 'P.uvprojx').write_bytes(b'<x/>\r\n')
        _git(['add', '-A'], root)
        _git(['commit', '-qm', 'init'], root)
        # force the unsafe state the guard above normally prevents
        (root / '.git' / 'info' / 'attributes').write_text(
            '*.uvprojx -text\n', encoding='utf-8')

        plan = rh.build_plan(rh.RepoState(root))
        rh.apply_renormalize(plan)
        staged = subprocess.run(['git', 'diff', '--cached', '--name-only'],
                                cwd=str(root), stdout=subprocess.PIPE)
        self.assertEqual(staged.stdout.decode().strip(), '')

    def test_a_full_local_apply_leaves_the_repo_untouched(self):
        tmp = tempfile.mkdtemp()
        root = make_repo(tmp, {'main.c': 'x\n', 'Proj/A.uvoptx': '<x/>\n',
                               'Proj/P.uvprojx': '<x/>\n'},
                         gitignore="*.o\n")
        (root / 'Proj' / 'A.uvoptx').write_bytes(b'<y/>\n')
        (root / 'Proj' / '.clangd').write_bytes(b'x')

        plan = rh.build_plan(rh.RepoState(root))
        rh.write_ignore(plan)
        # Same order as the CLI: attributes, then renormalize -- setting -text
        # can itself turn a clean file into a phantom.
        rh.write_gitattributes(plan)
        rh.apply_renormalize(plan)
        rh.apply_freeze(plan)

        after = rh.RepoState(root)
        self.assertEqual(after.untracked, [])
        self.assertEqual(after.modified, set())
        # nothing the repo carries was altered
        self.assertEqual((root / '.gitignore').read_text(encoding='utf-8'),
                         "*.o\n")
        self.assertFalse((root / '.gitattributes').exists())


class TestWriters(unittest.TestCase):
    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp, {'main.c': 'x\n', 'Proj/A.uvoptx': '<x/>\n'},
                             gitignore="*.o\n")
            state = rh.RepoState(root)
            plan = rh.build_plan(state)
            rh.write_ignore(plan, shared=True, dry_run=True)
            rh.apply_freeze(plan, dry_run=True)
            self.assertEqual((root / '.gitignore').read_text(encoding='utf-8'),
                             "*.o\n")
            self.assertFalse((root / '.gitignore.bak').exists())
            self.assertEqual(rh.RepoState(root).frozen, set())

    def test_write_backs_up_the_old_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp, {'main.c': 'x\n'}, gitignore="Doc/x/\n")
            plan = rh.build_plan(rh.RepoState(root))
            rh.write_ignore(plan, shared=True)
            self.assertEqual(
                (root / '.gitignore.bak').read_text(encoding='utf-8'), "Doc/x/\n")

    def test_freeze_then_unfreeze_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp, {'Proj/A.uvoptx': '<x/>\n'})
            plan = rh.build_plan(rh.RepoState(root))
            rh.apply_freeze(plan)

            state = rh.RepoState(root)
            self.assertIn('Proj/A.uvoptx', state.frozen)
            # the file is still in the repo -- freezing is not deletion
            self.assertIn('Proj/A.uvoptx', state.tracked)

            # and a local edit no longer shows up
            (root / 'Proj' / 'A.uvoptx').write_text('<changed/>\n',
                                                    encoding='utf-8')
            self.assertEqual(rh.RepoState(root).modified, set())

            rh.apply_unfreeze(state, ['Proj/A.uvoptx'])
            after = rh.RepoState(root)
            self.assertEqual(after.frozen, set())
            self.assertIn('Proj/A.uvoptx', after.modified)


if __name__ == '__main__':
    unittest.main()
