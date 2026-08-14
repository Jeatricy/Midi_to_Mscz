"""Package entry point: open the local Web UI without arguments."""

from __future__ import annotations

import sys


def main() -> int:
    arguments = sys.argv[1:]
    if not arguments or arguments == ["--gui"]:
        from .webui import main as gui_main

        return gui_main()
    from .cli import main as cli_main

    return cli_main(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
