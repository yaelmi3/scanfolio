import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import scanfolio_credentials as credentials


class ScanfolioCredentialsTest(unittest.TestCase):
    def setUp(self) -> None:
        home = Path(self.enterContext(TemporaryDirectory()))
        self.key_file = home / ".scanfolio" / "openai.key"
        self.env: dict[str, str] = {}
        self.home = self.enterContext(patch.object(credentials.Path, "home", return_value=home))
        self.enterContext(patch.object(credentials, "os", SimpleNamespace(environ=self.env, name=os.name)))

    def test_environment_key_takes_precedence_without_resolving_home(self) -> None:
        self.env["OPENAI_API_KEY"] = "  fake-environment-key  "
        self.assertEqual(credentials.load_api_key(), "fake-environment-key")
        self.home.assert_not_called()

    def test_missing_and_blank_files_provide_no_key(self) -> None:
        self.assertIsNone(credentials.load_api_key())
        self.assertFalse(self.key_file.parent.exists())
        self.key_file.parent.mkdir()
        self.key_file.write_text("  \n ", encoding="utf-8")
        self.assertIsNone(credentials.load_api_key())

    def test_save_creates_and_replaces_the_utf8_key_file(self) -> None:
        self.env["OPENAI_API_KEY"] = "   "
        credentials.save_api_key("  fake-key-é  ")
        self.assertEqual(self.key_file.read_bytes(), "fake-key-é".encode("utf-8"))
        self.assertEqual(credentials.load_api_key(), "fake-key-é")

        credentials.save_api_key("fake-replacement-key")
        self.assertEqual(credentials.load_api_key(), "fake-replacement-key")
        self.assertEqual(list(self.key_file.parent.iterdir()), [self.key_file])
        if os.name == "posix":
            self.assertEqual(self.key_file.parent.stat().st_mode & 0o077, 0)
            self.assertEqual(self.key_file.stat().st_mode & 0o077, 0)

    def test_blank_key_keeps_the_saved_key(self) -> None:
        credentials.save_api_key("fake-original-key")
        with self.assertRaisesRegex(credentials.CredentialError, "nonblank"):
            credentials.save_api_key(" \n ")
        self.assertEqual(credentials.load_api_key(), "fake-original-key")

    def test_failed_replace_keeps_the_saved_key_and_cleans_up(self) -> None:
        credentials.save_api_key("fake-original-key")
        with patch.object(Path, "replace", autospec=True,
                          side_effect=PermissionError("fake-sensitive-write-detail")) as replace:
            with self.assertRaisesRegex(credentials.CredentialError, "Could not save") as error:
                credentials.save_api_key("fake-replacement-key")
        temporary, destination = replace.call_args.args
        self.assertEqual(destination, self.key_file)
        self.assertEqual(temporary.parent, destination.parent)
        self.assertEqual(credentials.load_api_key(), "fake-original-key")
        self.assertEqual(list(self.key_file.parent.iterdir()), [self.key_file])
        self.assertIn("~/.scanfolio/openai.key", str(error.exception))
        self.assertNotIn("fake", str(error.exception))
        self.assertIsNone(error.exception.__context__)

    def test_invalid_utf8_reports_the_file_without_raw_details(self) -> None:
        self.key_file.parent.mkdir()
        self.key_file.write_bytes(b"fake-sensitive-read-detail\x80")
        with self.assertRaisesRegex(credentials.CredentialError, "Could not read") as error:
            credentials.load_api_key()
        self.assertIn("~/.scanfolio/openai.key", str(error.exception))
        self.assertNotIn("fake", str(error.exception))
        self.assertIsNone(error.exception.__context__)
