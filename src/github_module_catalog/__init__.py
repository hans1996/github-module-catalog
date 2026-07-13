"""GitHub Module Catalog package."""

from typing import Final

__version__: Final = "0.1.0"


def main() -> None:
    """Run the package command-line interface."""

    from github_module_catalog.cli import main as cli_main

    cli_main()
