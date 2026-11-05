import sys

ALLOWED = {"MIT", "BSD-3-Clause", "Apache-2.0"}
LICENSES = {"requests": "Apache-2.0", "attrs": "MIT", "padkit": "GPL-3.0", "fastjson": "SSPL-1.0"}

flagged = 0
names = [line.split("==")[0].strip() for line in open(sys.argv[1]) if line.strip()]
for name in names:
    license = LICENSES.get(name, "unknown")
    if license not in ALLOWED:
        flagged += 1
        print(f"{name}: {license} is not allowed")
print(f"{flagged} of {len(names)} packages have licenses that are not allowed")
