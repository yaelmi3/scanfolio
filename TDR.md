# Scanfolio Windows launch

## TL;DR

Add a guided launch from Windows Explorer. Keep the two-folder CLI for scripts.

## Launch

With scan and output folders supplied, Typer runs the existing command. With no arguments, the native folder
picker asks for a flat scan folder once. Scanfolio writes results to `scanfolio-export/` inside that folder.
Rich shows the current card and progress count. The final summary shows successes, failures, and cards needing
review, with an option to open the export folder.

The guided launch asks for a key when a new extraction needs one. It saves the confirmed key in `~/.scanfolio/openai.key`. `OPENAI_API_KEY` takes priority. Cached runs work offline.

## Processing and export

Both modes use the same extraction and export flow. Scanfolio sends a full-resolution scan copy to OpenAI and uses Pillow to crop original pixels to PNG. The Excel-friendly `catalog.csv` has one row per scan with the filename MHP ID, printed MHP ID, review flags, and relative paths to crops in `photos/`. Local checkpoints let reruns reuse completed extractions and retry failures.

## Windows release

Build one executable with PyInstaller on Windows. Before release, run the pilot on clean Windows without Python and confirm six rows and seven photos.
