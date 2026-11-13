# Contributing

Thanks for helping! The most useful contributions are **reports from other truck types**.

## Rules

- **No Linde material.** Do not commit Pathfinder files, `.cla` / `.xml` data, PDFs, manuals,
  screenshots of Linde documents or anything copied from them. Pull requests with such content are
  rejected.
- **No licence or copy-protection workarounds**, and no requests for Pathfinder copies or cracks.
- **Read-only by default.** New tools must not write to a truck unless the user explicitly runs
  a write command. Anything that changes settings must be logged as `WRITE`.
- **Strip private data** from logs and reports (serial numbers, company names, locations).

## Workflow

1. Open an issue first (bug report or compatibility report).
2. Fork, create a branch named after the change, e.g. `truck-352-support` or `fix-lac-timeout`.
3. Keep `pathfinder_can.py` working with Python 3.8+ and only `python-can` + `pyserial`.
4. Update `__version__` (see `CHANGELOG.md` for the rules), `CHANGELOG.md` and, for trucks,
   `COMPATIBILITY.md`.
5. Describe in the pull request which truck(s) you tested on.
