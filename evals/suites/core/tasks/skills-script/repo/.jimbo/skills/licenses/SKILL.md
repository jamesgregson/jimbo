---
name: licenses
description: Check whether the project's dependencies have licenses we are allowed to ship
---
1. Run `python3 <skill directory>/check_licenses.py requirements.txt` from the workspace.
2. Report each package it flags, with its license. Packages it doesn't flag are fine.

Don't guess licenses yourself: the script's table is the one legal approved.
