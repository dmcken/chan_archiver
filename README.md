# chan_archiver

Archive 4chan threads (API JSON, HTML with local CSS/JS, images and
optionally thumbnails) and automatically pick up new threads from board
catalogs using regex filters.

## Install / run with uv

```sh
# Run without installing
uvx --from git+https://github.com/dmcken/chan_archiver chan-archiver --help

# Install as a tool (provides `chan-archiver` and the short alias `chan`)
uv tool install git+https://github.com/dmcken/chan_archiver
# ...or from a local checkout
uv tool install .            # add --editable to pick up local changes
uv tool upgrade chan-archiver
```

## Data directory

All state lives in a base directory: `threads.txt`, `boards.json`,
`archive/` and `completed/`. It is chosen in this order:

1. `--dir PATH`
2. the `CHAN_ARCHIVER_DIR` environment variable
3. the current working directory

Copy the example [boards.json](boards.json) into that directory to use
`fetch`.

## Usage

```sh
chan                         # interactive menu
chan add URL [URL ...]       # add thread(s) and archive them once
chan run                     # archive every listed thread once
chan fetch [BOARD ...]       # search catalogs with boards.json filters
chan monitor                 # keep running 'run' until the list is empty
chan list | remove URL | clean
```

Example cron entry:

```
*/10 * * * * CHAN_ARCHIVER_DIR=$HOME/4chan $HOME/.local/bin/chan fetch && $HOME/.local/bin/chan run
```

## From Python

```python
from chan_archiver import Archiver, FourChanAPI

for t in FourChanAPI().get_threads("w"):
    print(t["no"], t.get("sub", ""))

Archiver("/srv/4chan", thumbs=True).add("https://boards.4chan.org/w/thread/123456")
```

## Development

```sh
uv sync                      # create .venv with dev tools
uv run pytest                # tests (offline, no network)
uv run ruff check .          # lint
uv run ruff format .         # format
uv run chan --help           # run from the checkout (or: uv run python -m chan_archiver)
```

Code layout (`src/chan_archiver/`):

| Module          | Purpose                                                    |
| --------------- | ---------------------------------------------------------- |
| `cli.py`        | argparse command line and console-script entry point       |
| `menu.py`       | interactive menu (no subcommand)                           |
| `archiver.py`   | `Archiver`: archive/ and completed/ management, run/fetch  |
| `api.py`        | `FourChanAPI`: catalog, thread and status lookups          |
| `client.py`     | `HttpClient`: per-host throttle, retries, verified downloads |
| `page.py`       | thread HTML rewriting and external link extraction         |
| `filters.py`    | `boards.json` loading and catalog matching                 |
| `threadlist.py` | `threads.txt` with file locking                            |
| `lock.py`       | single-instance lock                                       |
| `urls.py`       | thread URL parsing                                         |
| `config.py`     | constants and base directory default                       |
