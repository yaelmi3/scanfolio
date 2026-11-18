# Scanfolio

Scanfolio transcribes museum catalog cards into a bilingual CSV and crops their mounted photos to PNG.
Use uv to set up Python 3.12 and start the guided launch or pass a flat scan folder and an output folder.

```sh
uv sync --python 3.12
uv run python museum_cli.py
uv run python museum_cli.py /path/to/scans /path/to/output
```

In the guided launch, choose the scan folder once. Scanfolio saves the results in `scanfolio-export/` inside
that folder and offers to open it after showing the results.
The guided launch and the CLI in an interactive terminal ask for an OpenAI API key when the first uncached scan needs
one. Scanfolio hides the input and saves the key as plain text in `~/.scanfolio/openai.key` under your home directory.
If saving fails, Scanfolio warns you and uses the key for the current run only.

Run `uv run python museum_cli.py --update-key` in a terminal to replace the saved key without choosing folders or
running extraction. Blank input or cancellation leaves the saved key unchanged.
`OPENAI_API_KEY` takes priority over the saved key. For unattended runs, set that variable or save a key in
advance.

Each new extraction sends a full-resolution JPEG copy to `gpt-5.4` with original image detail, low reasoning,
and `store=False`. Each request has a five-minute timeout.

- `catalog.csv` opens in Excel with Hebrew and English preserved. Each row includes the exact filename stem,
  the printed registration number, transcribed notes, review flags, and a JSON array of relative `photo_paths`.
- `photos/` holds PNG crops from the orientation-corrected source pixels.
- `.museum-state/` holds raw structured extractions and SDK usage in JSON files keyed by source SHA-256.
  Each new extraction also prints its input, cached input, and output token counts.

To resume, choose the same scan folder in the guided launch or pass the same two folder arguments.
The CLI reuses saved extractions, retries failures, and can rebuild the CSV and crops offline.
Changed scans get new checkpoints and crop paths. The CSV references only crops for the current scans.
The CLI writes successful rows even when a scan fails, records failures in their rows, and exits with code `1`.
Review flags alone do not change the exit code.

## Windows x64

Use the [checked-in Scanfolio.exe](Scanfolio.exe) or [download it directly from GitHub Releases][windows-download].
Double-click the executable in Explorer to start the guided launch.

[windows-download]: https://github.com/yaelmi3/scanfolio-public/releases/download/v0.1.1/Scanfolio.exe

### Build from source

With uv installed, run these commands in PowerShell on Windows. The build uses PyInstaller from the dev group.

```powershell
uv sync --python 3.12
uv run pyinstaller --onefile --console --name Scanfolio museum_cli.py
.\dist\Scanfolio.exe --help
.\dist\Scanfolio.exe 'C:\path\to\scans' 'C:\path\to\output'
.\dist\Scanfolio.exe --update-key
```

Double-click `dist/Scanfolio.exe` in Explorer to use the guided launch on Windows.
