# Entry point for the Nuitka build of CCSessionSync.exe (Windows installer version; structure ported from CC Msg).
#   CCSessionSync.exe                -> desktop window (same as: cc-sessionsync desktop)
#   CCSessionSync.exe --port 8799    -> desktop window on another port
#   CCSessionSync.exe <subcommand>   -> same subcommands as cc-sessionsync (status, sync, codex, sessions, selfcheck, key, ...)
import sys

from cc_sessionsync.cli import main

if len(sys.argv) == 1 or (sys.argv[1].startswith("-") and sys.argv[1] not in ("--version", "-h", "--help")):
    sys.argv.insert(1, "desktop")
sys.exit(main() or 0)
