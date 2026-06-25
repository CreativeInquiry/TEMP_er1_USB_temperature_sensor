# TEMP_er1 Python Reader

User-facing setup and command documentation lives in the top-level
[README.md](../README.md).

This directory contains:

```text
temper_cli.py      Python HID reader, OSC sender, and optional TSV logger
requirements.txt   Python packages used by the reader
vendor/            project-local installed dependencies
logs/              generated daily TSV logs; created when --log is enabled
```

Implementation notes:

- The sensor is accessed through `hidapi`.
- The OSC sender uses `python-osc`.
- `temper_cli.py` prepends `vendor/` to `sys.path` when present, so no virtualenv
  is required.
- The HID protocol is based on the `0c45:7401` TEMPer1V1.4 path in
  `bitplane/temper`.
