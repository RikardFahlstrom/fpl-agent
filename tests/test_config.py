"""The local ini file. It can hold a password, so masking and precedence are tested."""
import os
import stat
import tempfile
import unittest
from pathlib import Path

from fpl_agent import config

MANAGED = list(config.MAPPING.values())


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in MANAGED}
        self.addCleanup(self._restore)
        for key in MANAGED:
            os.environ.pop(key, None)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def _restore(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _write(self, text, mode=0o600):
        path = self.dir / "fpl-agent.ini"
        path.write_text(text)
        path.chmod(mode)
        return path

    def test_settings_become_environment_variables(self):
        path = self._write("[auth]\nauto_login = true\nemail = a@b.c\npassword = pw\n"
                           "[rivals]\nleagues = 920863\n")
        applied = config.load(path)

        self.assertEqual(os.environ["FPL_AUTO_LOGIN"], "true")
        self.assertEqual(os.environ["FPL_EMAIL"], "a@b.c")
        self.assertEqual(os.environ["FPL_PASSWORD"], "pw")
        self.assertEqual(os.environ["FPL_RIVAL_LEAGUES"], "920863")
        self.assertEqual(set(applied), {"FPL_AUTO_LOGIN", "FPL_EMAIL", "FPL_PASSWORD",
                                        "FPL_RIVAL_LEAGUES"})

    def test_secrets_are_masked_in_the_summary(self):
        """The returned mapping is logged, so it must not carry the credentials."""
        path = self._write("[auth]\nemail = a@b.c\npassword = hunter2\n")
        applied = config.load(path)

        self.assertEqual(applied["FPL_PASSWORD"], "***")
        self.assertEqual(applied["FPL_EMAIL"], "***")
        self.assertNotIn("hunter2", str(applied))
        self.assertNotIn("a@b.c", str(applied))

    def test_the_environment_wins(self):
        """A scheduled run or secret store must be able to override the file."""
        os.environ["FPL_RIVAL_LEAGUES"] = "18891"
        path = self._write("[rivals]\nleagues = 920863\n")
        applied = config.load(path)

        self.assertEqual(os.environ["FPL_RIVAL_LEAGUES"], "18891")
        self.assertNotIn("FPL_RIVAL_LEAGUES", applied)

    def test_a_blank_value_is_not_applied(self):
        """The template ships with empty keys; they must not set empty variables."""
        path = self._write("[auth]\nemail =\npassword =   \n")
        self.assertEqual(config.load(path), {})
        self.assertNotIn("FPL_EMAIL", os.environ)

    def test_a_missing_file_is_not_an_error(self):
        self.assertEqual(config.load(self.dir / "absent.ini"), {})

    def test_malformed_file_warns_rather_than_crashing(self):
        path = self._write("this is not ini\n[[[\n")
        with self.assertLogs("fpl_config", level="WARNING") as logs:
            self.assertEqual(config.load(path), {})
        self.assertTrue(any("could not parse" in line for line in logs.output))

    def test_unrecognised_settings_are_reported(self):
        """A typo should not fail silently - it would look like the setting applied."""
        path = self._write("[auth]\nauto_login = true\nemial = a@b.c\n")
        with self.assertLogs("fpl_config", level="WARNING") as logs:
            config.load(path)
        self.assertTrue(any("auth.emial" in line for line in logs.output))

    def test_loose_permissions_warn(self):
        path = self._write("[auth]\npassword = pw\n", mode=0o644)
        with self.assertLogs("fpl_config", level="WARNING") as logs:
            config.load(path)
        self.assertTrue(any("readable beyond its owner" in line for line in logs.output))

    def test_owner_only_permissions_do_not_warn(self):
        path = self._write("[auth]\npassword = pw\n", mode=0o600)
        with self.assertNoLogs("fpl_config", level="WARNING"):
            config.load(path)

    def test_loading_twice_is_idempotent(self):
        path = self._write("[auth]\nauto_login = true\n")
        config.load(path)
        second = config.load(path)
        self.assertEqual(second, {})     # already in the environment, so nothing to set
        self.assertEqual(os.environ["FPL_AUTO_LOGIN"], "true")


class ExampleFileTests(unittest.TestCase):
    def test_the_shipped_example_only_uses_known_settings(self):
        """The template must not document settings the loader ignores."""
        import configparser
        example = Path(__file__).resolve().parents[1] / "fpl-agent.ini.example"
        self.assertTrue(example.exists(), "the template should be committed")

        parser = configparser.ConfigParser(allow_no_value=True)
        parser.read(example)
        for section in parser.sections():
            for option in parser.options(section):
                self.assertIn((section, option), config.MAPPING,
                              f"{section}.{option} is documented but not mapped")


if __name__ == "__main__":
    unittest.main()
