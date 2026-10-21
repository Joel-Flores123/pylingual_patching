import logging
from pathlib import Path

import click

from pylingual.utils.version import PythonVersion


@click.command(help="Patch DECOMPILED source in the terminal until it compiles to the same bytecode as ORIGINAL_PYC.", context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("decompiled", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.argument("original_pyc", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("-o", "--output", default=None, type=click.Path(dir_okay=False, path_type=Path), help="Where Save writes the patched source (default: overwrite DECOMPILED).", metavar="PATH")
@click.option("-v", "--version", default=None, type=PythonVersion, help="Python version to compile patches with, default is the version of ORIGINAL_PYC.", metavar="VERSION")
def main(decompiled: Path, original_pyc: Path, output: Path | None, version: PythonVersion | None):
    logging.basicConfig(level="WARNING", format="%(message)s")
    # imported late so --help does not pay for loading the decompiler
    from pylingual.patcher.analysis import PatchSession
    from pylingual.patcher.state import PatchState
    from pylingual.patcher.tui import PatcherApp

    click.echo(f"Checking {decompiled} against {original_pyc}...")
    session = PatchSession(original_pyc, version)
    try:
        PatcherApp(PatchState(session, decompiled, output or decompiled)).run()
    finally:
        session.close()
