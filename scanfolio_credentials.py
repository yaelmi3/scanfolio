import os
from contextlib import suppress
from pathlib import Path
from tempfile import NamedTemporaryFile


_KEY_FILE = Path(".scanfolio") / "openai.key"
KEY_FILE_DISPLAY = f"~/{_KEY_FILE.as_posix()}"


class CredentialError(Exception):
    """Scanfolio could not read or save its OpenAI API key."""


def load_api_key() -> str | None:
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if key:
        return key

    try:
        return (Path.home() / _KEY_FILE).read_text(encoding="utf-8").strip() or None
    except FileNotFoundError:
        return None
    except (OSError, RuntimeError, UnicodeError):
        pass
    raise CredentialError(f"Could not read the OpenAI API key from {KEY_FILE_DISPLAY}.")


def save_api_key(key: str) -> None:
    key = key.strip()
    if not key:
        raise CredentialError("Enter a nonblank OpenAI API key.")

    temporary = None
    try:
        path = Path.home() / _KEY_FILE
        path.parent.mkdir(mode=0o700 if os.name == "posix" else 0o777, exist_ok=True)
        with NamedTemporaryFile(dir=path.parent, prefix=".openai-", mode="w", encoding="utf-8", delete=False) as file:
            temporary = Path(file.name)
            file.write(key)
        temporary.replace(path)
        return
    except (OSError, RuntimeError, UnicodeError):
        pass
    finally:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
    raise CredentialError(f"Could not save the OpenAI API key to {KEY_FILE_DISPLAY}.")
