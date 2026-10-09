"""Interactive setup MOCK tests: synthetic keys and disposable homes only."""
from contextlib import ExitStack
from io import StringIO
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import multidot_setup as setup
import multidot_wizard as wizard

MARKER = "LOCAL_WIZARD_TEST_ONLY_NEVER_A_REAL_KEY"


class Terminal(StringIO):
    def isatty(self):
        return True


class WizardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="multidot-wizard-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.env = patch.dict(os.environ, {"HOME": str(self.home)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.config = setup.default_config()
        self.output, self.errors = Terminal(), Terminal()

    def conversation(self, count=1, confirm="yes"):
        answers = []
        for index in range(count):
            answers.extend(["\uc5f0\uad6c \ub2f4\ub2f9 " + str(index),
                            "tunnel_" + format(index + 1, "032x"),
                            "yes" if index + 1 < count else "no"])
        return [*answers, confirm]

    def run_wizard(self, answers=None, keys=None, input_effect=None,
                   secret_effect=None, path=None, state_root=None):
        with ExitStack() as stack:
            for name, stream in (("stdin", Terminal()), ("stdout", self.output),
                                 ("stderr", self.errors)):
                stack.enter_context(patch.object(sys, name, stream))
            stack.enter_context(patch("builtins.input", side_effect=(
                input_effect if input_effect is not None else
                (answers if answers is not None else self.conversation()))))
            stack.enter_context(patch.object(wizard, "read_secret", side_effect=(
                secret_effect if secret_effect is not None else
                (keys if keys is not None else [MARKER]))))
            provision = stack.enter_context(patch.object(setup, "provision_stage"))
            configure = stack.enter_context(patch.object(setup, "configure"))
            result = wizard.collect_and_save(self.config if path is None else path,
                                             state_root=state_root)
            provision.assert_not_called()
            configure.assert_not_called()
        self.assertNotIn(MARKER, self.output.getvalue() + self.errors.getvalue())
        self.assertNotIn(MARKER, json.dumps(result))
        return result

    def assert_no_secret_temporary(self):
        self.assertFalse(list(self.home.rglob("*.pending")))

    def test_arbitrary_count_saves_only_complete_private_config(self):
        result = self.run_wizard(answers=self.conversation(37), keys=[MARKER] * 37)
        self.assertEqual(result, {"ok": True, "saved": True, "requested_setup": True,
                                  "configured_dots": 37, "cancelled": False,
                                  "values_displayed": False})
        rows = setup.load_dots(self.config)
        self.assertEqual(len(rows), 37)
        self.assertEqual(rows[0]["name"], "\uc5f0\uad6c \ub2f4\ub2f9 0")
        self.assertTrue(all(row["runtime_api_key"] == MARKER and row["role"] == "worker"
                            for row in rows))
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.config.parent.stat().st_mode), 0o700)
        self.assertEqual(self.config.stat().st_nlink, 1)
        self.assert_no_secret_temporary()

    def test_each_redirected_stream_is_refused_before_io(self):
        for redirected in ("stdin", "stdout", "stderr"):
            with self.subTest(stream=redirected), ExitStack() as stack:
                for name in ("stdin", "stdout", "stderr"):
                    stack.enter_context(patch.object(sys, name,
                        StringIO() if name == redirected else Terminal()))
                ask = stack.enter_context(patch("builtins.input"))
                secret = stack.enter_context(patch.object(wizard, "read_secret"))
                with self.assertRaises(setup.SetupError) as caught:
                    wizard.collect_and_save(self.config)
                self.assertEqual(caught.exception.code, "interactive_terminal_required")
                ask.assert_not_called()
                secret.assert_not_called()
                self.assertFalse(self.config.parent.exists())

    def test_masked_input_failure_has_no_visible_fallback(self):
        with self.assertRaises(setup.SetupError) as caught:
            self.run_wizard(secret_effect=setup.SetupError("secure_password_entry_unavailable"))
        self.assertEqual(caught.exception.code, "secure_password_entry_unavailable")
        self.assertFalse(self.config.exists())
        self.assert_no_secret_temporary()

    def test_decline_and_default_confirmation_leave_no_config(self):
        for answer in ("no", ""):
            with self.subTest(answer=answer):
                result = self.run_wizard(answers=self.conversation(confirm=answer))
                self.assertFalse(result["saved"])
                self.assertFalse(result["requested_setup"])
                self.assertTrue(result["cancelled"])
                self.assertFalse(self.config.exists())
                self.assert_no_secret_temporary()

    def test_eof_and_interrupt_at_each_visible_prompt_leave_no_config(self):
        answers = self.conversation()
        for failure in (EOFError, KeyboardInterrupt):
            for index in range(len(answers)):
                with self.subTest(failure=failure, index=index):
                    result = self.run_wizard(answers=[*answers[:index], failure()])
                    self.assertTrue(result["cancelled"])
                    self.assertFalse(result["saved"])
                    self.assertFalse(self.config.exists())
                    self.assert_no_secret_temporary()

    def test_eof_and_interrupt_during_masked_entry_leave_no_config(self):
        for failure in (EOFError, KeyboardInterrupt):
            with self.subTest(failure=failure):
                result = self.run_wizard(secret_effect=failure())
                self.assertTrue(result["cancelled"])
                self.assertFalse(result["saved"])
                self.assertFalse(self.config.exists())
                self.assert_no_secret_temporary()

    def test_reviewed_blank_template_is_replaced_atomically(self):
        setup.initialize_config(self.config)
        inode = self.config.stat().st_ino
        self.assertTrue(self.run_wizard()["saved"])
        self.assertNotEqual(self.config.stat().st_ino, inode)
        self.assertEqual(setup.load_dots(self.config)[0]["runtime_api_key"], MARKER)
        self.assert_no_secret_temporary()

    def test_declined_existing_blank_stays_exactly_unchanged(self):
        setup.initialize_config(self.config)
        before = self.config.read_bytes(), self.config.stat().st_ino
        self.run_wizard(answers=self.conversation(confirm="no"))
        self.assertEqual((self.config.read_bytes(), self.config.stat().st_ino), before)

    def test_filled_or_noncanonical_config_is_refused_before_prompts(self):
        setup.initialize_config(self.config)
        variants = [json.dumps({"schema_version": 1, "dots": []}).encode(),
                    setup.json_bytes(setup.EMPTY).replace(b'"name": ""', b'"name": "x"'),
                    json.dumps(setup.EMPTY).encode(), MARKER.encode()]
        for data in variants:
            with self.subTest(data_length=len(data)):
                self.config.write_bytes(data)
                with self.assertRaises(setup.SetupError) as caught:
                    self.run_wizard(input_effect=AssertionError("No input expected"))
                self.assertEqual(caught.exception.code,
                                 "existing_config_not_blank_use_non_interactive")
                self.assertEqual(self.config.read_bytes(), data)
                self.assertNotIn(MARKER, str(caught.exception))
                self.assert_no_secret_temporary()

    def test_insecure_files_and_symlinks_are_never_replaced(self):
        setup.initialize_config(self.config)
        self.config.chmod(0o644)
        before = self.config.read_bytes()
        with self.assertRaises(setup.SetupError):
            self.run_wizard()
        self.assertEqual(self.config.read_bytes(), before)
        self.config.unlink()
        other = self.root / "other.private.json"
        other.write_bytes(setup.json_bytes(setup.EMPTY))
        other.chmod(0o600)
        self.config.symlink_to(other)
        with self.assertRaises(setup.SetupError):
            self.run_wizard()
        self.assertTrue(self.config.is_symlink())
        self.assertEqual(other.read_bytes(), before)

    def test_duplicate_tunnel_and_empty_key_repeat_entry_without_leaking(self):
        tunnel_one, tunnel_two = "tunnel_" + "1" * 32, "tunnel_" + "2" * 32
        answers = ["First", tunnel_one, "yes", "Duplicate", tunnel_one,
                   "Second", tunnel_two, "Second", tunnel_two, "no", "yes"]
        result = self.run_wizard(answers=answers, keys=[MARKER, MARKER, "", MARKER])
        self.assertEqual(result["configured_dots"], 2)
        self.assertEqual(self.output.getvalue().count("Invalid dot entry."), 2)
        self.assertEqual([row["name"] for row in setup.load_dots(self.config)],
                         ["First", "Second"])

    def test_unknown_yes_no_answer_is_not_treated_as_approval(self):
        answers = self.conversation()[:-1] + ["maybe", "no"]
        result = self.run_wizard(answers=answers)
        self.assertFalse(result["saved"])
        self.assertIn("Please answer yes or no.", self.output.getvalue())

    def test_filled_config_appearing_during_collection_is_preserved(self):
        answers = iter(self.conversation())
        def ask(prompt):
            if prompt.startswith("Save and prepare"):
                self.config.write_text(MARKER)
                self.config.chmod(0o600)
            return next(answers)
        with self.assertRaises(setup.SetupError) as caught:
            self.run_wizard(input_effect=ask)
        self.assertEqual(caught.exception.code, "config_changed_during_setup")
        self.assertEqual(self.config.read_text(), MARKER)
        self.assert_no_secret_temporary()

    def test_blank_changed_in_place_or_replaced_during_collection_is_preserved(self):
        for replacement in (False, True):
            with self.subTest(replacement=replacement):
                if self.config.exists():
                    self.config.unlink()
                setup.initialize_config(self.config)
                answers = iter(self.conversation())
                def ask(prompt):
                    if prompt.startswith("Save and prepare"):
                        if replacement:
                            self.config.unlink()
                            self.config.write_bytes(setup.json_bytes(setup.EMPTY))
                            self.config.chmod(0o600)
                        else:
                            self.config.write_bytes(MARKER.encode())
                    return next(answers)
                with self.assertRaises(setup.SetupError) as caught:
                    self.run_wizard(input_effect=ask)
                self.assertEqual(caught.exception.code, "config_changed_during_setup")
                self.assertEqual(self.config.read_bytes(),
                    setup.json_bytes(setup.EMPTY) if replacement else MARKER.encode())
                self.assert_no_secret_temporary()

    def test_race_after_absent_recheck_cannot_clobber_existing_config(self):
        real_link = os.link
        def racing_link(source, destination, **kwargs):
            self.config.write_text(MARKER)
            self.config.chmod(0o600)
            return real_link(source, destination, **kwargs)
        with patch.object(wizard.os, "link", side_effect=racing_link), \
                self.assertRaises(setup.SetupError):
            self.run_wizard()
        self.assertEqual(self.config.read_text(), MARKER)
        self.assert_no_secret_temporary()

    def test_second_recheck_detects_blank_edit_while_temp_is_written(self):
        setup.initialize_config(self.config)
        real_fsync = os.fsync
        calls = []
        def racing_fsync(fd):
            calls.append(fd)
            if len(calls) == 1:
                self.config.write_text(MARKER)
            return real_fsync(fd)
        with patch.object(wizard.os, "fsync", side_effect=racing_fsync), \
                self.assertRaises(setup.SetupError) as caught:
            self.run_wizard()
        self.assertEqual(caught.exception.code, "config_changed_during_setup")
        self.assertEqual(self.config.read_text(), MARKER)
        self.assert_no_secret_temporary()

    def test_cooperative_lock_refuses_second_wizard_before_prompts(self):
        parent, name = setup.config_parent(self.config, create=True)
        fd = wizard._lock(parent, name)
        try:
            with self.assertRaises(setup.SetupError) as caught:
                self.run_wizard(input_effect=AssertionError("No input expected"))
            self.assertEqual(caught.exception.code, "config_setup_already_in_progress")
            self.assertFalse(self.config.exists())
        finally:
            os.close(fd)
            os.close(parent)
        self.assertTrue(self.run_wizard()["saved"])

    def test_failure_or_interrupt_before_commit_cleans_secret_temp(self):
        for failure in (OSError(MARKER), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__):
                # Create the app dir before mocking fsync so the injected failure
                # exercises a secret-bearing staged file rather than mkdir.
                self.config.parent.mkdir(mode=0o700, exist_ok=True)
                with patch.object(wizard.os, "fsync", side_effect=failure):
                    if isinstance(failure, KeyboardInterrupt):
                        self.assertTrue(self.run_wizard()["cancelled"])
                    else:
                        with self.assertRaises(setup.SetupError) as caught:
                            self.run_wizard()
                        self.assertEqual(caught.exception.code, "interactive_setup_failed")
                        self.assertNotIn(MARKER, str(caught.exception))
                self.assertFalse(self.config.exists())
                self.assert_no_secret_temporary()

    def test_private_stage_is_complete_before_atomic_link(self):
        real_link = os.link
        inspected = []
        def inspect_link(source, destination, **kwargs):
            pending = self.config.parent / source
            self.assertEqual(stat.S_IMODE(pending.stat().st_mode), 0o600)
            spec = json.loads(pending.read_bytes())
            self.assertEqual(len(setup.validate_dots(spec)), 1)
            self.assertFalse(self.config.exists())
            inspected.append(True)
            return real_link(source, destination, **kwargs)
        with patch.object(wizard.os, "link", side_effect=inspect_link):
            self.assertTrue(self.run_wizard()["saved"])
        self.assertEqual(inspected, [True])
        self.assert_no_secret_temporary()

    def test_parent_sync_failure_retains_complete_committed_config(self):
        self.config.parent.mkdir(mode=0o700)
        real_fsync = os.fsync
        def fail_parent(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError(MARKER)
            return real_fsync(fd)
        with patch.object(wizard.os, "fsync", side_effect=fail_parent), \
                self.assertRaises(setup.SetupError) as caught:
            self.run_wizard()
        self.assertEqual(caught.exception.code, "config_saved_sync_unconfirmed")
        self.assertTrue(caught.exception.config_saved)
        self.assertEqual(len(setup.load_dots(self.config)), 1)
        self.assert_no_secret_temporary()

    def test_fdopen_failure_cleans_pending_file_and_keeps_blank(self):
        setup.initialize_config(self.config)
        before = self.config.read_bytes()
        real_fdopen = os.fdopen
        def fail_write(fd, mode):
            if mode == "wb":
                raise OSError(MARKER)
            return real_fdopen(fd, mode)
        with patch.object(wizard.os, "fdopen", side_effect=fail_write), \
                self.assertRaises(setup.SetupError) as caught:
            self.run_wizard()
        self.assertEqual(caught.exception.code, "interactive_setup_failed")
        self.assertFalse(caught.exception.config_saved)
        self.assertEqual(self.config.read_bytes(), before)
        self.assert_no_secret_temporary()

    def test_interrupt_after_atomic_commit_keeps_complete_config_and_stops_setup(self):
        real_mask = signal.pthread_sigmask
        def interrupt_on_restore(operation, mask):
            previous = real_mask(operation, mask)
            if operation == signal.SIG_SETMASK:
                raise KeyboardInterrupt()
            return previous
        with patch.object(wizard.signal, "pthread_sigmask", side_effect=interrupt_on_restore):
            result = self.run_wizard()
        self.assertTrue(result["saved"])
        self.assertTrue(result["cancelled"])
        self.assertFalse(result["requested_setup"])
        self.assertEqual(len(setup.load_dots(self.config)), 1)
        self.assert_no_secret_temporary()

    def test_cleanup_failure_is_reported_without_hiding_committed_config(self):
        real_unlink = os.unlink
        def fail_pending(path, **kwargs):
            if str(path).endswith(".pending"):
                raise OSError(MARKER)
            return real_unlink(path, **kwargs)
        with patch.object(wizard.os, "unlink", side_effect=fail_pending), \
                self.assertRaises(setup.SetupError) as caught:
            self.run_wizard()
        self.assertEqual(caught.exception.code, "config_temporary_cleanup_unconfirmed")
        self.assertTrue(caught.exception.config_saved)
        # This injected filesystem refusal really prevents cleanup. Verify the
        # failure is explicit, then remove the alias in the disposable fixture.
        aliases = list(self.config.parent.glob("*.pending"))
        self.assertEqual(len(aliases), 1)
        self.assertEqual(self.config.read_bytes(), aliases[0].read_bytes())
        aliases[0].unlink()
        self.assertEqual(len(setup.load_dots(self.config)), 1)

    def test_publication_defers_terminal_signals_and_restores_original_mask(self):
        previous = {signal.SIGUSR1}
        with patch.object(wizard.signal, "pthread_sigmask", return_value=previous) as mask:
            self.assertTrue(self.run_wizard()["saved"])
        self.assertEqual(mask.call_args_list[0].args,
            (signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM, signal.SIGHUP}))
        self.assertEqual(mask.call_args_list[-1].args, (signal.SIG_SETMASK, previous))

    def test_oversized_serialized_config_is_not_published(self):
        with patch.object(wizard, "MAX_CONFIG_BYTES", 10), \
                self.assertRaises(setup.SetupError) as caught:
            self.run_wizard()
        self.assertEqual(caught.exception.code, "config_too_large")
        self.assertFalse(self.config.exists())
        self.assert_no_secret_temporary()

    def test_explicit_private_path_and_tilde_use_only_fake_home(self):
        path = self.home / "alternate.private.json"
        result = self.run_wizard(path="~/alternate.private.json")
        self.assertTrue(result["saved"])
        self.assertEqual(len(setup.load_dots(path)), 1)
        self.assertFalse(self.config.parent.exists())

    def test_summary_discloses_actual_config_state_paths_and_plaintext_storage(self):
        state = self.home / "custom-state"
        self.run_wizard(state_root="~/custom-state")
        output = self.output.getvalue()
        self.assertIn("Configuration file: " + json.dumps(str(self.config)), output)
        self.assertIn("Local state directory: " + json.dumps(str(state)), output)
        self.assertIn("store API keys in plaintext", output)
        self.assertIn("issue internal runtime credentials", output)
        self.assertIn("will not contact the network or start services", output)
        self.assertFalse(state.exists())

    def test_temporary_secret_filenames_are_ignored_inside_repository(self):
        path = "config/.multidot-config-" + "0" * 32 + ".pending"
        result = subprocess.run(["git", "check-ignore", "--no-index", "-q", "--stdin"],
                                input=path + "\n", text=True, cwd=ROOT,
                                env=dict(os.environ, HOME=str(self.home)),
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
