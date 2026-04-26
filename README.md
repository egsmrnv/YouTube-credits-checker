# YouTube Credits Checker

Small OCR-based tool for checking whether selected names appear in the end credits of videos from a YouTube playlist.

It downloads the tail of each video, extracts frames, runs Tesseract OCR, and writes matches to CSV and JSONL reports.

## Requirements

- Python 3
- yt-dlp
- ffmpeg / ffprobe
- tesseract
- Russian OCR language data for Tesseract

On macOS:

```bash
brew install yt-dlp ffmpeg tesseract tesseract-lang
```

Check dependencies:

```bash
python3 scan_youtube_credits.py --check
```

## Settings

Private names live in `settings.json`, which is ignored by Git.

Create it from the example:

```bash
cp settings.example.json settings.json
```

Then edit `settings.json`:

```json
{
  "names": [
    "First Person",
    "Second Person"
  ]
}
```

You can also pass names directly:

```bash
python3 scan_youtube_credits.py --name "First Person" --name "Second Person"
```

## Usage

Run the scan with the default playlist:

```bash
python3 scan_youtube_credits.py
```

Scan another playlist:

```bash
python3 scan_youtube_credits.py --playlist "https://www.youtube.com/playlist?list=..."
```

Test only a few videos:

```bash
python3 scan_youtube_credits.py --limit 3
```

## Output

Results are written to:

- `credits_scan/results.csv`
- `credits_scan/results.jsonl`

Generated clips, frames, OCR cache, and reports are stored in `credits_scan/` and ignored by Git.
