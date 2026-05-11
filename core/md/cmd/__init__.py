"""CLI subcommand modules. One file per command. Each exposes a
`register_parser(sub)` function that adds its subparser to the main
argparse, plus a `cmd_X(args)` handler that runs it.

The dispatcher in md_helper_core.py imports each module and calls its
register_parser. Running the CLI dispatches to the appropriate cmd_X.
"""
