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

## Development

```sh
uv sync                      # create .venv with dev tools
uv run pytest                # tests (offline, no network)
uv run ruff check .          # lint
uv run chan --help           # run from the checkout
```
