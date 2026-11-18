import base64
import csv
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import unicodedata
import warnings
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from getpass import GetPassWarning, getpass
from io import BytesIO
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Annotated

import typer
from openai import APIConnectionError, APIStatusError, OpenAI, OpenAIError
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError
from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn

import scanfolio_credentials

MODEL = "gpt-5.4"
OPENAI_URL = "https://api.openai.com/v1"
SCAN_SUFFIXES = {".jpg", ".jpeg", ".jpe", ".png", ".tif", ".tiff", ".webp", ".bmp"}
PHOTO_DIR = "photos"
CHECKPOINT_DIR = ".museum-state"
BOX_SCALE = 1000
KEY_FILE = scanfolio_credentials.KEY_FILE_DISPLAY
KEY_OVERRIDE = "Set OPENAI_API_KEY to a valid key and rerun Scanfolio."
KEY_REPLACEMENT_HELP = f"Run Scanfolio with --update-key to replace the saved key. {KEY_OVERRIDE}"
KEY_SAVE_FAILED = f"Could not save the OpenAI API key to {KEY_FILE}."

app = typer.Typer(pretty_exceptions_show_locals=False, add_completion=False)

EXTRACTION_INSTRUCTIONS = """
Transcribe a museum catalog card with printed and handwritten Hebrew and English. Preserve the language,
spelling, numerals, and line order as written. Never translate, infer missing information, or use the pictured
object or its photo to guess field values. Omit the אוס and מוזיאון תל אביב branding and empty form labels.

Copy the printed registration number exactly, including MHP if printed as part of the number. Keep crossed-out,
old, film, receipt, and photo numbers separate. Use null for an absent field or a reading too uncertain to enter.
Copy the film number beside פלם or פילם, often just below a photo. The number beside מס׳ ישן is the old number.
Use מוצא for origin, תקופה for period, and טיפוס for typology, following the individual field boundaries.
Copy dimensions in cm as strings when clear. Preserve measurements in other units in notes. Use notes for any
other readable annotations, marginalia, references, alternate numbers, and unmodeled fields. Use review_issues
to name an ambiguous field and include any legible fragments or provisional reading. Include every readable
note outside the mounted photos. Return empty lists when there are no entries.

For condition, check the title including any parenthetical line under the object name, and the free-text
description below the form. Copy only the written phrases that describe the object's physical state,
damage, missing parts, reconstruction or repairs. Common cues include משוחזר, מתפרק, סדק, חור, פגום,
שבור and repairs בגבס. Copy a cue only when it is written on the card. Keep the original wording in
the transcribed title or notes as well. Shape, decoration, material, numbers and storage information
(מקום החפץ במוזיאון or מקום החפץ בבית הנכות) belong in their own fields or notes. Return null for condition
if there is no readable statement of physical condition. Use the card text as your only evidence.

Find every mounted photograph, including when a scan has more than one. For each, return a tight box around
the photographic image. Exclude text, captions, paper, and borders beyond the photo edges. Keep all photo
pixels at the edges. Order photos top to bottom, then left to right. Coordinates are integers on a 0 to 1000
scale for each axis of the entire supplied image. The top left of the image is (0, 0), and the bottom right
edge is (1000, 1000). Return left, top, right, bottom edges, with x measured from the left and y from the top.
Return an empty photo_boxes list if there are no photos. Flag uncertain photo boundaries in review_issues.
""".strip()


class PhotoBox(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    left: int
    top: int
    right: int
    bottom: int

    def pixels(self, image: Image.Image) -> tuple[int, int, int, int] | None:
        if not (0 <= self.left < self.right <= BOX_SCALE and 0 <= self.top < self.bottom <= BOX_SCALE):
            return None
        left, top = round(self.left * image.width / BOX_SCALE), round(self.top * image.height / BOX_SCALE)
        right, bottom = round(self.right * image.width / BOX_SCALE), round(self.bottom * image.height / BOX_SCALE)
        return (left, top, right, bottom) if left < right and top < bottom else None


class CardExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    printed_registration_number: str | None
    object_name: str | None
    condition: str | None
    material: str | None
    period: str | None
    origin: str | None
    typology: str | None
    height_cm: str | None
    width_cm: str | None
    length_cm: str | None
    diameter_cm: str | None
    old_number: str | None
    receipt_number: str | None
    film_number: str | None
    photo_label: str | None
    storage_location: str | None
    notes: list[str]
    review_issues: list[str]
    photo_boxes: list[PhotoBox]


CSV_FIELDS = [name for name in CardExtraction.model_fields if name not in {"review_issues", "photo_boxes"}]


class TokenUsage(BaseModel):
    model_config = ConfigDict(extra="allow")

    input_tokens: int
    output_tokens: int
    input_tokens_details: dict[str, JsonValue] | None = None


class Checkpoint(BaseModel):
    source_sha256: str
    response_id: str
    model: str
    usage: TokenUsage | None
    extraction: CardExtraction


@dataclass
class ScanResult:
    source: Path
    extraction: CardExtraction | None = None
    photo_paths: list[str] = field(default_factory=list)
    review_issues: list[str] = field(default_factory=list)
    error: str = ""

    def csv_row(self) -> dict[str, str | None]:
        fields = {} if self.extraction is None else self.extraction.model_dump(include=set(CSV_FIELDS) - {"notes"})
        notes = "" if self.extraction is None else "\n".join(self.extraction.notes)
        return {"source_scan": self.source.name, "mhp_id": self.source.stem, **fields, "notes": notes,
                "photo_paths": json.dumps(self.photo_paths, ensure_ascii=False),
                "needs_review": "yes" if self.review_issues or self.error else "no",
                "review_issues": "\n".join(self.review_issues), "error": self.error}


class ScanError(Exception):
    pass


class RunCancelled(Exception):
    pass


def _interactive_console() -> bool:
    return (sys.stdin is not None and sys.stdin.isatty()
            and sys.stderr is not None and sys.stderr.isatty())


def _prompt_console_key(label: str) -> str:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", GetPassWarning)
            key = getpass(label).strip()
    except (EOFError, GetPassWarning, KeyboardInterrupt, OSError):
        return ""
    # Windows getpass can return Ctrl+Z as a literal character.
    return key if key.isprintable() else ""


def _prompt_scan_key(progress: Progress) -> str:
    progress.stop()
    try:
        return _prompt_console_key("OpenAI API key: ")
    finally:
        progress.start()


def _update_key() -> None:
    if not _interactive_console():
        raise ScanError("No key was saved. Run --update-key in an interactive terminal.")
    key = _prompt_console_key("New OpenAI API key: ")
    if not key:
        raise ScanError("No key was saved. Enter a key in a terminal that supports hidden input.")
    try:
        scanfolio_credentials.save_api_key(key)
    except scanfolio_credentials.CredentialError:
        raise ScanError(f"{KEY_SAVE_FAILED} No saved key was updated. {KEY_OVERRIDE}") from None
    typer.echo(f"Saved the OpenAI API key to {KEY_FILE}.")


@dataclass
class ExtractionAccess:
    prompt: Callable[[], str] | None = None
    warn: Callable[[str], None] | None = None
    error: str = ""
    _key: str | None = field(default=None, init=False, repr=False)

    def client(self) -> OpenAI:
        if self.error:
            raise ScanError(self.error)
        if self._key is None:
            try:
                self._key = self._load_key()
            except scanfolio_credentials.CredentialError:
                self.error = f"Could not read the OpenAI API key from {KEY_FILE}. {KEY_REPLACEMENT_HELP}"
                raise ScanError(self.error) from None
            if self._key is None:
                self.error = f"An OpenAI API key is required for new scans. {KEY_OVERRIDE}"
                raise ScanError(self.error)
        return OpenAI(api_key=self._key, base_url=OPENAI_URL, max_retries=0, timeout=300.0)

    def _load_key(self) -> str | None:
        key = scanfolio_credentials.load_api_key()
        if key or self.prompt is None:
            return key
        key = self.prompt()
        if not key:
            raise RunCancelled
        try:
            scanfolio_credentials.save_api_key(key)
        except scanfolio_credentials.CredentialError:
            warning = f"{KEY_SAVE_FAILED} Scanfolio will use this key for this run only."
            if self.warn is None:
                typer.echo(f"Warning: {warning}", err=True)
            else:
                self.warn(warning)
        return key


@dataclass(frozen=True)
class RunSummary:
    succeeded: int
    failed: int
    photos: int
    review: int

    def __str__(self) -> str:
        return (f"Successful scans {self.succeeded}\nFailed scans {self.failed}\n"
                f"Photos {self.photos}\nCards needing review {self.review}")


def _log_usage(source_name: str, checkpoint: Checkpoint) -> None:
    usage = checkpoint.usage
    tokens = "usage unavailable"
    if usage:
        cached = (usage.input_tokens_details or {}).get("cached_tokens")
        cache_info = f" ({cached} cached)" if cached is not None else ""
        tokens = f"{usage.input_tokens} input{cache_info}, {usage.output_tokens} output tokens"
    typer.echo(f"{source_name!r} extracted with {checkpoint.model}, {tokens}")


def _extract(*, image: Image.Image, source_sha256: str, access: ExtractionAccess) -> Checkpoint:
    with access.client() as client:
        buffer = BytesIO()
        image.convert("RGB").save(buffer, format="JPEG", quality=95, subsampling=0, optimize=True)
        image_url = "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
        response = client.responses.create(
            model=MODEL, store=False, reasoning={"effort": "medium"}, instructions=EXTRACTION_INSTRUCTIONS,
            input=[{"role": "user", "content": [
                {"type": "input_text", "text": f"Transcribe this {image.width} × {image.height} scan."},
                {"type": "input_image", "image_url": image_url, "detail": "original"},
            ]}],
            text={"format": {"type": "json_schema", "name": "museum_card", "strict": True,
                             "schema": CardExtraction.model_json_schema()}},
        )
    usage = response.usage
    tokens = f" ({usage.input_tokens} input, {usage.output_tokens} output tokens)" if usage else ""
    if response.status != "completed" or not response.output_text:
        raise ScanError(f"OpenAI response {response.id} returned no completed extraction{tokens}.")
    try:
        extraction = CardExtraction.model_validate_json(response.output_text)
    except ValidationError:
        raise ScanError(f"OpenAI response {response.id} did not match the card schema{tokens}.") from None
    return Checkpoint(source_sha256=source_sha256, response_id=response.id, model=response.model,
                      usage=TokenUsage.model_validate(usage.model_dump(mode="json")) if usage else None,
                      extraction=extraction)


@contextmanager
def _atomic_output(path: Path) -> Iterator[Path]:
    with NamedTemporaryFile(dir=path.parent, prefix=".museum-", delete=False) as temporary:
        temp_path = Path(temporary.name)
    try:
        yield temp_path
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)


def _checkpoint(*, image: Image.Image, output: Path, source_sha256: str, source_name: str,
                access: ExtractionAccess) -> Checkpoint:
    path = output / CHECKPOINT_DIR / f"{source_sha256}.json"
    try:
        saved = path.read_bytes()
    except FileNotFoundError:
        checkpoint = _extract(image=image, source_sha256=source_sha256, access=access)
        with _atomic_output(path) as temporary:
            temporary.write_text(checkpoint.model_dump_json(indent=2) + "\n", encoding="utf-8")
        _log_usage(source_name, checkpoint)
        return checkpoint
    try:
        checkpoint = Checkpoint.model_validate_json(saved)
    except ValidationError:
        raise ScanError(f"Unreadable checkpoint {path.name}.") from None
    if checkpoint.source_sha256 != source_sha256:
        raise ScanError(f"Source fingerprint mismatch in checkpoint {path.name}.")
    typer.echo(f"{source_name!r} resumed from checkpoint")
    return checkpoint


def _crop_photos(*, image: Image.Image, output: Path, source_sha256: str, result: ScanResult,
                 boxes: list[PhotoBox]) -> None:
    if not boxes:
        result.review_issues.append("No mounted photo found.")
        return
    label = re.sub(r"[^A-Za-z0-9_.-]+", "_", result.source.stem).strip("._-")[:64] or "scan"
    file_key = f"{source_sha256}:{result.source.name}".encode("utf-8", errors="surrogatepass")
    suffix = hashlib.sha256(file_key).hexdigest()[:16]
    for index, box in enumerate(boxes, start=1):
        bounds = box.pixels(image)
        if bounds is None:
            result.review_issues.append(f"Photo {index} has invalid bounds.")
            continue
        relative_path = Path(PHOTO_DIR) / f"scan-{label}-{suffix}-photo-{index:02}.png"
        with image.crop(bounds) as photo, _atomic_output(output / relative_path) as target:
            photo.save(target, format="PNG", exif=b"")
        result.photo_paths.append(relative_path.as_posix())


def _process_scan(*, result: ScanResult, output: Path, access: ExtractionAccess) -> None:
    content = result.source.read_bytes()
    source_sha256 = hashlib.sha256(content).hexdigest()
    with Image.open(BytesIO(content)) as decoded:
        if getattr(decoded, "n_frames", 1) != 1:
            raise ScanError("Expected one image per card. Export each page or frame as a separate scan.")
        image = ImageOps.exif_transpose(decoded)
    with image:
        if image.mode not in {"1", "L", "LA", "I;16", "I;16B", "P", "RGB", "RGBA"}:
            image = image.convert("RGB")
        checkpoint = _checkpoint(image=image, output=output, source_sha256=source_sha256,
                                 source_name=result.source.name, access=access)
        result.extraction = checkpoint.extraction
        result.review_issues.extend(checkpoint.extraction.review_issues)
        _crop_photos(image=image, output=output, source_sha256=source_sha256, result=result,
                     boxes=checkpoint.extraction.photo_boxes)


def _registration_key(number: str) -> str:
    normalized = unicodedata.normalize("NFKC", number).casefold()
    return "".join(char for char in normalized if not char.isspace() and char != ".").removeprefix("mhp")


def _flag_registrations(results: list[ScanResult]) -> None:
    stems = [_registration_key(result.source.stem) for result in results]
    printed = [_registration_key(result.extraction.printed_registration_number or "")
               if result.extraction else "" for result in results]
    stem_counts = Counter(stems)
    printed_counts = Counter(printed)
    for result, stem, registration in zip(results, stems, printed, strict=True):
        if stem_counts[stem] > 1:
            result.review_issues.append("Duplicate filename identifier.")
        if result.extraction is not None and not registration:
            result.review_issues.append("Missing printed registration number.")
        if registration and registration != stem:
            result.review_issues.append("Printed registration number differs from the filename identifier.")
        if registration and printed_counts[registration] > 1:
            result.review_issues.append("Duplicate printed registration number.")


def _excel_cell(value: str | None) -> str:
    if value is None:
        return ""
    if value.startswith(("\t", "\r", "\n")) or value.lstrip().startswith(tuple("=+-@＝＋－＠")):
        return "'" + value
    return value


def _write_catalog(results: list[ScanResult], output: Path) -> None:
    columns = ["source_scan", "mhp_id", *CSV_FIELDS, "photo_paths", "needs_review", "review_issues", "error"]
    with (_atomic_output(output / "catalog.csv") as temporary,
          temporary.open("w", encoding="utf-8-sig", newline="") as csv_file):
        writer = csv.DictWriter(csv_file, fieldnames=columns, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        for result in results:
            writer.writerow({name: _excel_cell(value) for name, value in result.csv_row().items()})


@contextmanager
def _output_lock(output: Path) -> Iterator[None]:
    with (output / CHECKPOINT_DIR / "run.lock").open("a+b") as lock_file:
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ScanError("Could not lock this output folder. Check for another running museum CLI.") from None
        yield


def _process_result(*, result: ScanResult, output: Path, access: ExtractionAccess) -> None:
    previous_access_error = access.error
    try:
        _process_scan(result=result, output=output, access=access)
    except APIStatusError as exc:
        if exc.status_code in {401, 403}:
            access.error = f"OpenAI rejected the API key or its permissions. {KEY_REPLACEMENT_HELP}"
            result.error = access.error
        else:
            result.error = f"OpenAI returned HTTP {exc.status_code}."
    except APIConnectionError:
        result.error = "Could not reach OpenAI or the request timed out."
    except OpenAIError:
        result.error = "The OpenAI SDK could not complete the request."
    except UnidentifiedImageError:
        result.error = "Pillow could not identify the image format."
    except (SyntaxError, EOFError):
        result.error = "Pillow could not read the scan."
    except (OSError, Image.DecompressionBombError, ScanError) as exc:
        result.error = str(exc)
    if result.error and result.error != previous_access_error:
        typer.echo(f"{result.source.name!r} failed. {result.error}", err=True)


def _run(*, scans: list[Path], output: Path, access: ExtractionAccess | None = None) -> RunSummary:
    results = [ScanResult(source) for source in scans]
    console = Console()
    with (_output_lock(output),
          Progress(TextColumn("{task.description}", markup=False), BarColumn(), MofNCompleteColumn(),
                   console=console, disable=not console.is_terminal) as progress):
        if access is None:
            access = ExtractionAccess()
            if _interactive_console():
                access.prompt = lambda: _prompt_scan_key(progress)
        task = progress.add_task("Processing scans", total=len(results))
        for result in results:
            progress.update(task, description=repr(result.source.name), refresh=True)
            _process_result(result=result, output=output, access=access)
            progress.advance(task)
        _flag_registrations(results)
        _write_catalog(results, output)
    failed = sum(bool(result.error) for result in results)
    summary = RunSummary(succeeded=len(results) - failed, failed=failed,
                         photos=sum(len(result.photo_paths) for result in results),
                         review=sum(bool(result.review_issues or result.error) for result in results))
    typer.echo(f"Wrote {len(results)} card rows to {output / 'catalog.csv'}.\n{summary}")
    return summary


def _scan_sources(folder: Path) -> list[Path]:
    try:
        entries = sorted(folder.iterdir())
    except OSError:
        raise ScanError("Choose an existing scan folder that Scanfolio can list.") from None
    sources = []
    for path in entries:
        if path.suffix.casefold() in SCAN_SUFFIXES:
            try:
                if not path.is_file():
                    continue
            except OSError:
                pass  # Keep inaccessible scan entries for per-card reporting in a mixed batch.
            sources.append(path)
    for path in sources:
        try:
            with Image.open(path) as image:
                if getattr(image, "n_frames", 1) == 1:
                    image.verify()
                    return sources
        except (OSError, SyntaxError, EOFError, Image.DecompressionBombError):
            continue
    raise ScanError("Choose a folder containing at least one readable, single-image scan.")


def _prepare_output(output: Path) -> Path:
    output = output.resolve()
    for folder in (output, output / PHOTO_DIR, output / CHECKPOINT_DIR):
        if folder.resolve() != folder:
            raise ScanError(f"Use a local directory for {folder.name!r} inside the output folder.")
        folder.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(dir=folder, prefix=".museum-"):
            pass
    if (output / "catalog.csv").is_dir():
        raise ScanError("Choose an output folder without a directory named catalog.csv.")
    return output


@app.command()
def main(scans: Annotated[Path | None, typer.Argument(help="Flat scan folder", exists=True, file_okay=False)] = None,
         output: Annotated[Path | None, typer.Argument(help="Output folder", file_okay=False)] = None,
         update_key: Annotated[bool, typer.Option("--update-key", help="Replace the saved OpenAI API key.")] = False,
         ) -> None:
    """Transcribe museum cards into a CSV and crop their mounted photos."""
    try:
        if update_key:
            if scans is not None or output is not None:
                raise typer.BadParameter("Run --update-key without folder arguments.")
            _update_key()
            return
        if scans is None or output is None:
            raise typer.BadParameter("Pass a scan folder and an output folder.")
        sources = _scan_sources(scans)
        summary = _run(scans=sources, output=_prepare_output(output))
    except RunCancelled:
        typer.echo(f"No OpenAI API key was entered. {KEY_REPLACEMENT_HELP}", err=True)
        raise typer.Exit(1) from None
    except (OSError, ScanError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None
    if summary.failed:
        raise typer.Exit(1)


def _open_output(output: Path) -> None:
    if sys.platform == "win32":
        os.startfile(output)
    else:
        command = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.run([command, str(output)], check=True)


def _guided_run() -> int:
    try:
        from tkinter import TclError, Tk, filedialog, messagebox, simpledialog
    except ImportError:
        typer.echo("Folder dialogs are unavailable. Run with a scan folder and an output folder.", err=True)
        return 1
    try:
        root = Tk()
    except TclError:
        typer.echo("Could not start folder dialogs. Run with a scan folder and an output folder.", err=True)
        return 1
    title = "Scanfolio"
    try:
        root.withdraw()
        # Aqua attaches parented native dialogs as sheets, which cannot appear on a withdrawn window.
        native_parent = None if root.tk.call("tk", "windowingsystem") == "aqua" else root
        while selected := filedialog.askdirectory(parent=native_parent, title="Choose the scan folder", mustexist=True):
            scans = Path(selected)
            try:
                sources = _scan_sources(scans)
                break
            except ScanError as exc:
                messagebox.showerror(title, str(exc), parent=native_parent)
        else:
            return 0

        output_folder = scans / "scanfolio-export"
        try:
            output = _prepare_output(output_folder)
        except (OSError, ScanError) as exc:
            messagebox.showerror(title, f"Could not use the export folder {output_folder}.\n\n{exc}\n\n"
                                 "Make this folder writable or pass a different output folder on the command line.",
                                 parent=native_parent)
            return 1

        def prompt() -> str:
            message = "Enter your OpenAI API key. Scanfolio saves it when you select OK."
            while (entered := simpledialog.askstring(title, message, show="*", parent=root)) is not None:
                if key := entered.strip():
                    return key
            return ""

        access = ExtractionAccess(prompt=prompt,
                                  warn=lambda message: messagebox.showwarning(title, message, parent=native_parent))
        try:
            summary = _run(scans=sources, output=output, access=access)
        except RunCancelled:
            return 0
        except (OSError, ScanError) as exc:
            messagebox.showerror(title, str(exc), parent=native_parent)
            return 1
        message = f"{summary}\n\n{access.error}\n\n" if access.error else f"{summary}\n\n"
        if messagebox.askyesno(title, f"{message}Open the export folder?", parent=native_parent):
            try:
                _open_output(output)
            except (OSError, subprocess.CalledProcessError):
                messagebox.showerror(title, f"Could not open the export folder.\n\n{output}", parent=native_parent)
        return int(bool(summary.failed))
    finally:
        root.destroy()


if __name__ == "__main__":
    logging.getLogger("openai").setLevel(logging.WARNING)
    if len(sys.argv) == 1:
        raise SystemExit(_guided_run())
    app()
