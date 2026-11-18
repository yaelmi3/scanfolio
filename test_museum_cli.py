import base64
import csv
import json
import sys
import unittest
import warnings
from contextlib import redirect_stderr, redirect_stdout
from io import BytesIO, StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from openai import OpenAI
from PIL import Image
from rich.text import Text
from typer.testing import CliRunner

import museum_cli


class MuseumCliTest(unittest.TestCase):
    def test_oriented_tiff_with_two_photos_resumes_when_optional_sdk_usage_is_absent(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory(dir=Path(__file__).parent) as folder:
            work = Path(folder)
            scans = work / "scans"
            scans.mkdir()
            with Image.new("RGB", (12, 8), "red") as image:
                image.paste("blue", (0, 4, 12, 8))
                exif = Image.Exif()
                exif[274] = 6
                image.save(scans / "MHP410.60D.jpg", format="TIFF", exif=exif)

            client = OpenAI(api_key="offline-placeholder", base_url=museum_cli.OPENAI_URL,
                            http_client=httpx.Client(transport=httpx.MockTransport(self.mock_response)))
            output = work / "output"
            args = [str(scans), str(output)]
            with (patch.object(museum_cli.scanfolio_credentials, "load_api_key",
                               return_value="offline-placeholder") as vault,
                  patch.object(museum_cli, "OpenAI", return_value=client) as factory,
                  patch.object(museum_cli, "_crop_photos", side_effect=OSError("mock crop write failure"))):
                failed = runner.invoke(museum_cli.app, args)
            self.assertEqual(failed.exit_code, 1, (failed.output, failed.exception))
            self.assertIn("mock crop write failure", failed.output)
            factory.assert_called_once()
            vault.assert_called_once()

            with (patch.object(museum_cli, "sys", self.mock_console()),
                  patch.object(museum_cli, "getpass") as prompt,
                  patch.object(museum_cli.scanfolio_credentials, "load_api_key") as vault,
                  patch.object(museum_cli, "OpenAI") as factory):
                resumed = runner.invoke(museum_cli.app, args)
            self.assertEqual(resumed.exit_code, 0, (resumed.output, resumed.exception))
            self.assertIn("Successful scans 1\nFailed scans 0\nPhotos 2\nCards needing review 1", resumed.output)
            vault.assert_not_called()
            factory.assert_not_called()
            prompt.assert_not_called()
            catalog = output / "catalog.csv"
            self.assertTrue(catalog.read_bytes().startswith(b"\xef\xbb\xbf"))
            with catalog.open(encoding="utf-8-sig", newline="") as file:
                [row] = list(csv.DictReader(file))
            self.assertEqual(row["mhp_id"], "MHP410.60D")
            self.assertEqual(row["notes"], "הערה\nRef 47")
            self.assertEqual(row["receipt_number"], "' =1+2")
            self.assertEqual(row["needs_review"], "yes")
            [left, right] = [output / path for path in json.loads(row["photo_paths"])]
            for path, color in [(left, (0, 0, 255)), (right, (255, 0, 0))]:
                with Image.open(path) as crop:
                    self.assertEqual(crop.size, (4, 12))
                    self.assertEqual(crop.getcolors(), [(48, color)])

    def test_folder_command_prompts_once_and_keeps_scanning_if_the_save_is_blocked(self) -> None:
        runner = CliRunner()
        for platform, blocked in (("win32", False), ("win32", True), ("darwin", False)):
            with self.subTest(platform=platform, blocked=blocked), TemporaryDirectory() as folder:
                scans, output = Path(folder) / "scans", Path(folder) / "chosen-output"
                scans.mkdir()
                for number, color in enumerate(("red", "blue")):
                    with Image.new("RGB", (8, 12), color) as image:
                        image.save(scans / f"{number}.png")
                saved = None

                def save_key(key: str) -> None:
                    nonlocal saved
                    if blocked:
                        raise museum_cli.scanfolio_credentials.CredentialError("fake storage details")
                    saved = key

                def client(**kwargs) -> OpenAI:
                    return OpenAI(**kwargs, http_client=httpx.Client(transport=httpx.MockTransport(self.mock_response)))

                args = [str(scans), str(output)]
                with (patch.object(museum_cli, "sys", self.mock_console(platform)),
                      patch.object(museum_cli, "getpass", return_value="  fake-new-key  ") as prompt,
                      patch.object(museum_cli.scanfolio_credentials, "load_api_key", side_effect=lambda: saved) as load,
                      patch.object(museum_cli.scanfolio_credentials, "save_api_key", side_effect=save_key) as save,
                      patch.object(museum_cli, "OpenAI", side_effect=client) as factory):
                    first = runner.invoke(museum_cli.app, args)
                    self.assertEqual(first.exit_code, 0, (first.output, first.exception))
                    self.assertIn("Successful scans 2", first.output)
                    self.assertEqual(first.output.count("for this run only"), int(blocked))
                    if blocked:
                        self.assertIn("~/.scanfolio/openai.key", first.output)
                    self.assertNotIn("fake storage details", first.output)
                    self.assertNotIn("fake-new-key", first.output)
                    load.assert_called_once()
                    self.assertEqual(factory.call_count, 2)
                    self.assertTrue((output / "catalog.csv").is_file())
                    self.assertFalse((scans / "scanfolio-export").exists())

                    if not blocked:
                        with Image.new("RGB", (8, 12), "green") as image:
                            image.save(scans / "2.png")
                        later = runner.invoke(museum_cli.app, args)
                        self.assertEqual(later.exit_code, 0, (later.output, later.exception))
                        self.assertIn("Successful scans 3", later.output)
                        self.assertEqual(load.call_count, 2)
                        self.assertEqual(factory.call_count, 3)
                prompt.assert_called_once_with("OpenAI API key: ")
                save.assert_called_once_with("fake-new-key")
                self.assertTrue(all(call.kwargs["api_key"] == "fake-new-key" for call in factory.call_args_list))

    def test_folder_command_reports_unavailable_credentials_once_without_prompting(self) -> None:
        scenarios = [
            ("win32", False, None, "An OpenAI API key is required"),
            ("win32", True, museum_cli.scanfolio_credentials.CredentialError("fake storage details"),
             "Could not read the OpenAI API key from ~/.scanfolio/openai.key"),
        ]
        with TemporaryDirectory() as folder:
            scans = Path(folder) / "scans"
            scans.mkdir()
            with Image.new("RGB", (8, 8)) as image:
                for name in ("1.png", "2.png"):
                    image.save(scans / name)
            for index, (platform, interactive, error, message) in enumerate(scenarios):
                with self.subTest(platform=platform, interactive=interactive, error=error):
                    output = Path(folder) / f"output-{index}"
                    with (patch.object(museum_cli, "sys", self.mock_console(platform, interactive)),
                          patch.object(museum_cli, "getpass") as prompt,
                          patch.object(museum_cli.scanfolio_credentials, "load_api_key", return_value=None,
                                       side_effect=error) as load,
                          patch.object(museum_cli.scanfolio_credentials, "save_api_key") as save,
                          patch.object(museum_cli, "OpenAI") as factory):
                        result = CliRunner().invoke(museum_cli.app, [str(scans), str(output)])
                    self.assertEqual(result.exit_code, 1, (result.output, result.exception))
                    self.assertEqual(result.output.count(message), 1)
                    self.assertNotIn("fake storage details", result.output)
                    with (output / "catalog.csv").open(encoding="utf-8-sig", newline="") as file:
                        first, second = csv.DictReader(file)
                    self.assertIn(message, first["error"])
                    self.assertEqual(first["error"], second["error"])
                    load.assert_called_once()
                    prompt.assert_not_called()
                    save.assert_not_called()
                    factory.assert_not_called()

    def test_update_key_is_discoverable_and_replaces_a_key_without_reading_it(self) -> None:
        runner = CliRunner()
        help_text = Text.from_ansi(runner.invoke(museum_cli.app, ["--help"]).output).plain
        self.assertIn("--update-key", help_text)
        for stored in (None, "fake-stored-rejected-key"):
            with self.subTest(stored=stored):
                with (patch.object(museum_cli, "sys", self.mock_console()),
                      patch.object(museum_cli, "getpass", return_value="  fake-new-key  ") as prompt,
                      patch.object(museum_cli.scanfolio_credentials, "load_api_key", return_value=stored) as load,
                      patch.object(museum_cli.scanfolio_credentials, "save_api_key") as save,
                      patch.object(museum_cli, "_run") as extract,
                      patch.object(museum_cli, "_scan_sources") as scan_folders):
                    updated = runner.invoke(museum_cli.app, ["--update-key"])
                self.assertEqual(updated.exit_code, 0, (updated.output, updated.exception))
                self.assertIn("Saved the OpenAI API key to ~/.scanfolio/openai.key", updated.output)
                self.assertNotIn("fake-new-key", updated.output)
                load.assert_not_called()
                save.assert_called_once_with("fake-new-key")
                prompt.assert_called_once_with("New OpenAI API key: ")
                extract.assert_not_called()
                scan_folders.assert_not_called()

    def test_update_key_reports_when_no_key_was_saved(self) -> None:
        def unmasked_input(label: str) -> str:
            warnings.warn("Fake unmasked input warning", museum_cli.GetPassWarning, stacklevel=1)
            self.fail("The fallback must stop before reading the key")

        scenarios = [
            (False, "fake-key", None),
            (True, "  ", None),
            (True, KeyboardInterrupt(), None),
            (True, EOFError(), None),
            (True, "\x1a", None),
            (True, unmasked_input, None),
            (True, "fake-key", museum_cli.scanfolio_credentials.CredentialError("fake storage details")),
        ]
        for interactive, entry, error in scenarios:
            with self.subTest(interactive=interactive, entry=entry, error=error):
                with (patch.object(museum_cli, "sys", self.mock_console(interactive=interactive)),
                      patch.object(museum_cli, "getpass") as prompt,
                      patch.object(museum_cli.scanfolio_credentials, "load_api_key") as load,
                      patch.object(museum_cli.scanfolio_credentials, "save_api_key", side_effect=error) as save):
                    if isinstance(entry, str):
                        prompt.return_value = entry
                    else:
                        prompt.side_effect = entry
                    result = CliRunner().invoke(museum_cli.app, ["--update-key"])
                self.assertEqual(result.exit_code, 1, (result.output, result.exception))
                self.assertEqual(prompt.call_count, int(interactive))
                load.assert_not_called()
                if error is None:
                    save.assert_not_called()
                    self.assertIn("No key was saved", result.output)
                else:
                    save.assert_called_once_with("fake-key")
                    self.assertIn("No saved key was updated", result.output)
                    self.assertIn("~/.scanfolio/openai.key", result.output)
                self.assertNotIn("fake", result.output.lower())

    def test_guided_run_retries_folders_and_stops_requests_after_rejected_key(self) -> None:
        with TemporaryDirectory(dir=Path(__file__).parent) as folder:
            work = Path(folder)
            invalid_scans, scans = work / "invalid", work / "scans"
            output = scans / "scanfolio-export"
            invalid_scans.mkdir()
            (invalid_scans / "empty.jpg").touch()
            scans.mkdir()
            (scans / "01-bad.jpg").write_bytes(b"broken image")
            with Image.new("RGB", (8, 8)) as image:
                for name in ("02.png", "03.png"):
                    image.save(scans / name)
            dialogs = self.mock_dialogs([invalid_scans, scans], key="offline-placeholder")
            dialogs.Tk.return_value.tk.call.return_value = "aqua"
            dialogs.messagebox.askyesno.return_value = True
            reject = Mock(return_value=httpx.Response(401, json={"error": {"message": "offline-placeholder"}}))
            client = OpenAI(api_key="offline-placeholder", base_url=museum_cli.OPENAI_URL, max_retries=0,
                            http_client=httpx.Client(transport=httpx.MockTransport(reject)))
            log = StringIO()
            with (patch.dict(sys.modules, tkinter=dialogs),
                  patch.object(museum_cli, "sys", self.mock_console(platform="darwin")),
                  patch.object(museum_cli.scanfolio_credentials, "load_api_key", return_value=None) as load,
                  patch.object(museum_cli.scanfolio_credentials, "save_api_key") as save,
                  patch.object(museum_cli, "OpenAI", return_value=client),
                  patch.object(museum_cli, "_open_output") as open_output,
                  redirect_stdout(log), redirect_stderr(log)):
                self.assertEqual(museum_cli._guided_run(), 1)
            self.assertNotIn("offline-placeholder", log.getvalue())
            self.assertEqual(dialogs.filedialog.askdirectory.call_count, 2)
            for picker in dialogs.filedialog.askdirectory.call_args_list:
                self.assertEqual(picker.kwargs["title"], "Choose the scan folder")
                self.assertIsNone(picker.kwargs.get("parent"))
            for error in dialogs.messagebox.showerror.call_args_list:
                self.assertIsNone(error.kwargs.get("parent"))
            self.assertEqual(dialogs.messagebox.showerror.call_count, 1)
            self.assertIsNone(dialogs.messagebox.askyesno.call_args.kwargs.get("parent"))
            dialogs.simpledialog.askstring.assert_called_once()
            prompt = dialogs.simpledialog.askstring.call_args
            self.assertIn("OpenAI API key", prompt.args[1])
            self.assertEqual(prompt.kwargs["show"], "*")
            self.assertIs(prompt.kwargs["parent"], dialogs.Tk.return_value)
            load.assert_called_once()
            save.assert_called_once_with("offline-placeholder")
            reject.assert_called_once()
            open_output.assert_called_once_with(output.resolve())
            summary = dialogs.messagebox.askyesno.call_args.args[1]
            self.assertIn("Failed scans 3", summary)
            self.assertIn("OPENAI_API_KEY", summary)
            self.assertIn("--update-key", summary)
            self.assertNotIn("offline-placeholder", summary)
            with (output / "catalog.csv").open(encoding="utf-8-sig", newline="") as file:
                bad, first, second = csv.DictReader(file)
            self.assertIn("could not identify", bad["error"])
            self.assertIn("rejected the API key", first["error"])
            self.assertEqual(first["error"], second["error"])
            self.assertNotIn("offline-placeholder", first["error"])

    def test_cancelled_key_prompt_keeps_existing_export(self) -> None:
        with TemporaryDirectory(dir=Path(__file__).parent) as folder:
            scans = Path(folder) / "scans"
            output = scans / "scanfolio-export"
            scans.mkdir()
            output.mkdir()
            with Image.new("RGB", (8, 8)) as image:
                image.save(scans / "scan.png")
            catalog = output / "catalog.csv"
            catalog.write_bytes(b"earlier export")
            checkpoints = output / museum_cli.CHECKPOINT_DIR
            checkpoints.mkdir()
            earlier_checkpoint = checkpoints / "earlier.json"
            earlier_checkpoint.write_bytes(b"earlier checkpoint")
            dialogs = self.mock_dialogs([scans], key=None)
            dialogs.Tk.return_value.tk.call.return_value = "win32"
            with (patch.dict(sys.modules, tkinter=dialogs),
                  patch.object(museum_cli, "sys", self.mock_console()),
                  patch.object(museum_cli.scanfolio_credentials, "load_api_key", return_value=None),
                  patch.object(museum_cli.scanfolio_credentials, "save_api_key") as save,
                  patch.object(museum_cli, "OpenAI") as client):
                self.assertEqual(museum_cli._guided_run(), 0)
            self.assertEqual(catalog.read_bytes(), b"earlier export")
            self.assertEqual(earlier_checkpoint.read_bytes(), b"earlier checkpoint")
            save.assert_not_called()
            client.assert_not_called()
            dialogs.filedialog.askdirectory.assert_called_once_with(
                parent=dialogs.Tk.return_value, title="Choose the scan folder", mustexist=True)
            dialogs.Tk.return_value.destroy.assert_called_once()

    def test_guided_run_reports_blocked_export_before_accessing_credentials(self) -> None:
        with TemporaryDirectory(dir=Path(__file__).parent) as folder:
            scans = Path(folder)
            output = scans / "scanfolio-export"
            output.touch()
            with Image.new("RGB", (8, 8)) as image:
                image.save(scans / "scan.png")
            dialogs = self.mock_dialogs([scans], key=None)
            with (patch.dict(sys.modules, tkinter=dialogs),
                  patch.object(museum_cli.scanfolio_credentials, "load_api_key") as load):
                self.assertEqual(museum_cli._guided_run(), 1)
            dialogs.filedialog.askdirectory.assert_called_once()
            dialogs.simpledialog.askstring.assert_not_called()
            load.assert_not_called()
            dialogs.messagebox.showerror.assert_called_once()
            message = dialogs.messagebox.showerror.call_args.args[1]
            self.assertIn(str(output), message)
            self.assertIn("pass a different output folder on the command line", message)

    @staticmethod
    def mock_console(platform: str = "win32", interactive: bool = True) -> SimpleNamespace:
        stream = Mock(isatty=Mock(return_value=interactive))
        return SimpleNamespace(platform=platform, stdin=stream, stderr=stream)

    @staticmethod
    def mock_dialogs(folders: list[Path], key: str | None) -> SimpleNamespace:
        return SimpleNamespace(TclError=RuntimeError, Tk=Mock(), filedialog=Mock(
            askdirectory=Mock(side_effect=[str(folder) for folder in folders])),
            messagebox=Mock(), simpledialog=Mock(askstring=Mock(return_value=key)))

    def mock_response(self, request: httpx.Request) -> httpx.Response:
        self.assertEqual(str(request.url), "https://api.openai.com/v1/responses")
        data = json.loads(request.content)
        self.assertIs(data["store"], False)
        self.assertIs(data["text"]["format"]["strict"], True)
        image = data["input"][0]["content"][1]
        self.assertEqual(image["detail"], "original")
        with Image.open(BytesIO(base64.b64decode(image["image_url"].split(",", 1)[1]))) as sent:
            self.assertEqual((sent.format, sent.size), ("JPEG", (8, 12)))
        extraction = dict.fromkeys(museum_cli.CardExtraction.model_fields)
        extraction.update(printed_registration_number="MHP 41060", receipt_number=" =1+2",
                          notes=["הערה", "Ref 47"], review_issues=[],
                          photo_boxes=[{"left": 0, "top": 0, "right": 500, "bottom": 1000},
                                       {"left": 500, "top": 0, "right": 1000, "bottom": 1000}])
        return httpx.Response(200, json={
            "id": "resp_offline", "object": "response", "created_at": 1.0, "status": "completed",
            "model": "gpt-5.4-mini", "parallel_tool_calls": False, "tool_choice": "auto", "tools": [],
            "output": [{"id": "msg_mock", "type": "message", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": json.dumps(extraction), "annotations": []}]}],
            "usage": {"input_tokens": 137, "input_tokens_details": {"cached_tokens": 9}, "output_tokens": 27,
                      "output_tokens_details": {"reasoning_tokens": 4}, "total_tokens": 164},
        })
