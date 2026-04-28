"""URL snapshot subsystem.

Public surface:
- `Snapshotter` protocol + `SnapshotResult` dataclass (`base.py`)
- `classify_url` URL/Content-Type classifier (`classifier.py`)
- `PlaywrightSnapshotter` real implementation (`playwright_impl.py`)
- `merge_into_survivor` redirect-collision merge helper (`merge.py`)
"""
