#!/usr/bin/python3
"""Stand-in for `ps -o stat= -p PID`, used by Hermes Desktop inside the launcher.

/bin/ps is setuid root, and macOS refuses to run setuid programs inside
sandbox-exec. Desktop only asks for one process's state letter, so answer it
from os.kill/os.waitpid instead. Any other usage falls through to an error.
"""
import os
import sys

args = sys.argv[1:]
if len(args) == 4 and args[0] == "-o" and args[1] == "stat=" and args[2] == "-p":
    try:
        os.kill(int(args[3]), 0)
    except ProcessLookupError:
        sys.exit(1)
    except PermissionError:
        pass
    print("S")
    sys.exit(0)
sys.stderr.write("ps-shim: unsupported arguments\n")
sys.exit(1)
