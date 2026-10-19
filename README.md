# Snapchat Export Processor

Prepares Snapchat data exports for self-hosted photo managers (Immich, Google Photos, PhotoPrism) by baking overlay graphics, restoring EXIF/QuickTime metadata, and archiving chat logs to Markdown. Only media assets and chat transcripts are mirrored to output.

## Prerequisites (Ubuntu)

```bash
sudo apt update && sudo apt install -y exiftool python3-pip
pip install -r requirements.txt
```

## Usage

Extract your Snapchat export archive, then run:

```bash
# Dry run to preview changes
python3 process_snapchat_export.py --source snapchat-data --output snapchat-processed --dry-run

# Run full processing
python3 process_snapchat_export.py --source snapchat-data --output snapchat-processed
```

### CLI Options

| Option | Description |
| :--- | :--- |
| `-s, --source PATH` | Path to extracted Snapchat export directory (required). |
| `-o, --output PATH` | Path to destination directory (required). |
| `-n, --dry-run` | Analyze data without writing files or tags. |
| `--overwrite` | Allow writing into an existing output folder. |
| `--chunk-size INT` | Messages per chat markdown file (default: `500`). |
| `--temp-staging {auto,always,never}` | Fast Linux `/tmp` staging mode for WSL / 9p mounts (default: `auto`). |
| `--exiftool-bin PATH` | Path to ExifTool binary if not on `PATH`. |
| `--no-align-memories-sequence` | Disable chronological sequence alignment fallback for Memories. |
| `-v, --verbose` | Enable verbose progress logs. |

## Testing

```bash
python3 -m unittest test_process_snapchat_export.py
```
