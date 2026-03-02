# tube_archivist_file_renamer

Renames media files and folders by metadata and runs as a Kubernetes CronJob.

## What it does

- Reads `INPUT_FOLDER` env var as the root directory.
- Recursively finds media files (`.mp3`, `.m4a`, `.mp4`, etc.) and extracts title/artist metadata.
- Renames media file to `<title>.<ext>`.
- Renames matching subtitle files (`.vtt`) that share the same basename.
- Renames folders to the dominant artist name found in that folder.
- Handles conflicts with suffixes like `_1`, `_2`, ...

## Local run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
INPUT_FOLDER=/path/to/media python rename_from_metadata.py
```

## Kubernetes run

Update these values before use:

- `k8s/base/cronjob.yaml`:
  - image (`ghcr.io/...`)
  - hostPath source path (`/mnt/media/input`)
  - schedule

Then deploy from your machine (or from any CI job you control):

```bash
kubectl apply -k k8s/base
```

> Note: this repository intentionally does **not** include an automatic GitHub Action that runs
> `kubectl apply` against your NAS cluster. Deployment is explicit/manual so you stay in control
> of when cluster changes are applied.
