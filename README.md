# tube_archivist_file_renamer

Organizes and converts media files by metadata. Runs as a Kubernetes CronJob.

## What it does

- Reads `INPUT_FOLDER` env var as the root directory.
- Recursively finds media files (`.mp3`, `.m4a`, `.mp4`) and extracts title/artist metadata from embedded tags.
- **Does not modify or rename original files** — all output goes into new subfolders.
- For each media file, creates a subfolder named after the **artist** inside the file's current directory.
- Copies the media file into that subfolder, renamed to `<title>.mp3`.
  - `.mp4` files are **converted to MP3** (audio only) using ffmpeg, preserving all metadata including album art.
  - `.mp3` and `.m4a` files are copied as MP3 with metadata preserved.
- Copies matching subtitle files (`.vtt`) into the same subfolder, renamed to `<title>.vtt`.
- **Cron-safe**: after processing a file, a flag is embedded in its metadata. On subsequent runs the file is skipped, so files are never duplicated.
- Handles name conflicts with suffixes like `_1`, `_2`, ...

## Requirements

- `ffmpeg` must be available on the host or in the container (included in the Docker image).

## Local run
```bash
INPUT_FOLDER=/path/to/media python organize_media.py
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