#!/usr/bin/env python3
import argparse
import csv
import json
import re
import shutil
import subprocess
import sys
from difflib import SequenceMatcher
from pathlib import Path


DEFAULT_PLAYLIST = "https://www.youtube.com/playlist?list=PL4FeNajyKrkY04-0symLYag8LxSy7fZ-K"
DEFAULT_CONFIG = "settings.json"


def run(cmd, *, capture=False, check=True):
    kwargs = {"text": True}
    if capture:
        kwargs.update({"stdout": subprocess.PIPE, "stderr": subprocess.PIPE})
    result = subprocess.run(cmd, **kwargs)
    if check and result.returncode != 0:
        if capture and result.stderr:
            print(result.stderr.strip(), file=sys.stderr)
        raise subprocess.CalledProcessError(result.returncode, cmd)
    return result


def require_tools():
    missing = [tool for tool in ("yt-dlp", "ffmpeg", "ffprobe", "tesseract") if not shutil.which(tool)]
    if not missing:
        return

    print("Не хватает утилит: " + ", ".join(missing), file=sys.stderr)
    print("", file=sys.stderr)
    print("На macOS обычно достаточно:", file=sys.stderr)
    print("  brew install yt-dlp ffmpeg tesseract tesseract-lang", file=sys.stderr)
    print("", file=sys.stderr)
    print("После установки проверь русский OCR:", file=sys.stderr)
    print("  tesseract --list-langs | grep rus", file=sys.stderr)
    sys.exit(2)


def normalize(text):
    text = text.lower().replace("ё", "е")
    text = re.sub(r"[^а-яa-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def fuzzy_contains(text, target, threshold):
    text = normalize(text)
    target = normalize(target)
    if target in text:
        return 1.0

    words = text.split()
    target_words = target.split()
    if not words or not target_words:
        return 0.0

    size = len(target_words)
    best = 0.0
    for extra in range(0, 3):
        window_size = size + extra
        for i in range(0, max(1, len(words) - window_size + 1)):
            candidate = " ".join(words[i : i + window_size])
            best = max(best, SequenceMatcher(None, candidate, target).ratio())

    return best if best >= threshold else 0.0


def load_config(path):
    config_path = Path(path)
    if not config_path.exists():
        return {}

    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Cannot read config {config_path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"Config {config_path} must contain a JSON object.")
    return data


def get_names(args):
    if args.names:
        return args.names

    config = load_config(args.config)
    names = config.get("names", [])
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        raise ValueError(f'Config {args.config} must contain "names": ["Name One", "Name Two"].')

    names = [name.strip() for name in names if name.strip()]
    if names:
        return names

    raise ValueError(
        "No names configured. Add names to settings.json or pass --name multiple times."
    )


def get_playlist_items(playlist_url, cache_path):
    if cache_path.exists():
        return [json.loads(line) for line in cache_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    print("Получаю список видео из плейлиста...")
    result = run(
        ["yt-dlp", "--flat-playlist", "--dump-single-json", playlist_url],
        capture=True,
    )
    data = json.loads(result.stdout)
    entries = data.get("entries", [])
    cache_path.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in entries), encoding="utf-8")
    return entries


def get_duration(video_url):
    result = run(["yt-dlp", "--print", "duration", "--no-warnings", video_url], capture=True)
    value = result.stdout.strip().splitlines()[-1]
    return float(value)


def download_tail(video_url, video_id, duration, out_dir, seconds):
    clips_dir = out_dir / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(path for path in clips_dir.glob(f"{video_id}.*") if path.suffix != ".part")
    if existing:
        return existing[0]

    start = max(0, duration - seconds)
    section = f"*{start}-{duration}"
    output_template = str(clips_dir / f"{video_id}.%(ext)s")
    print(f"  скачиваю последние {int(duration - start)} сек.")
    run(
        [
            "yt-dlp",
            "--no-warnings",
            "--download-sections",
            section,
            "-f",
            "bv*[height<=720]+ba/b[height<=720]/b",
            "-o",
            output_template,
            video_url,
        ]
    )
    downloaded = sorted(path for path in clips_dir.glob(f"{video_id}.*") if path.suffix != ".part")
    if not downloaded:
        raise FileNotFoundError(f"yt-dlp did not create a clip for {video_id}")
    return downloaded[0]


def extract_frames(clip, video_id, out_dir, fps):
    frame_dir = out_dir / "frames" / video_id
    frame_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(frame_dir.glob("frame_*.jpg"))
    if existing:
        return existing

    run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(clip),
            "-vf",
            f"fps=1/{fps},scale=1280:-1",
            "-q:v",
            "2",
            str(frame_dir / "frame_%04d.jpg"),
        ]
    )
    return sorted(frame_dir.glob("frame_*.jpg"))


def ocr_frame(frame, lang):
    result = run(["tesseract", str(frame), "stdout", "-l", lang, "--psm", "6"], capture=True, check=False)
    return result.stdout.strip()


def scan_video(item, args, out_dir):
    video_id = item["id"]
    title = item.get("title") or video_id
    video_url = f"https://www.youtube.com/watch?v={video_id}"

    print(f"\nСканирую: {title}")
    matches = []
    ocr_path = out_dir / "ocr" / f"{video_id}.jsonl"
    ocr_path.parent.mkdir(parents=True, exist_ok=True)

    if ocr_path.exists() and ocr_path.stat().st_size > 0 and not args.force_ocr:
        print("  использую сохраненный OCR")
        for line in ocr_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            text = row.get("text", "")
            for name in args.names:
                score = fuzzy_contains(text, name, args.threshold)
                if score:
                    matches.append(
                        {
                            "video_id": video_id,
                            "title": title,
                            "url": video_url,
                            "name": name,
                            "score": round(score, 3),
                            "second": row.get("second", ""),
                            "frame": row.get("frame", ""),
                            "text": " ".join(text.split())[:500],
                        }
                    )
                    print(f"  найдено: {name}, score={score:.3f}, t={row.get('second', '')}s")
        if not matches:
            print("  совпадений нет")
        return matches

    duration = get_duration(video_url)
    clip = download_tail(video_url, video_id, duration, out_dir, args.tail_seconds)
    frames = extract_frames(clip, video_id, out_dir, args.frame_every)

    with ocr_path.open("w", encoding="utf-8") as ocr_file:
        for index, frame in enumerate(frames, start=1):
            text = ocr_frame(frame, args.lang)
            frame_second = max(0, duration - args.tail_seconds) + ((index - 1) * args.frame_every)
            ocr_file.write(
                json.dumps(
                    {"frame": str(frame), "second": round(frame_second, 1), "text": text},
                    ensure_ascii=False,
                )
                + "\n"
            )

            for name in args.names:
                score = fuzzy_contains(text, name, args.threshold)
                if score:
                    matches.append(
                        {
                            "video_id": video_id,
                            "title": title,
                            "url": video_url,
                            "name": name,
                            "score": round(score, 3),
                            "second": round(frame_second, 1),
                            "frame": str(frame),
                            "text": " ".join(text.split())[:500],
                        }
                    )
                    print(f"  найдено: {name}, score={score:.3f}, t={frame_second:.1f}s")

    if not matches:
        print("  совпадений нет")
    return matches


def write_results(rows, out_dir):
    csv_path = out_dir / "results.csv"
    jsonl_path = out_dir / "results.jsonl"
    fields = ["video_id", "title", "url", "name", "score", "second", "frame", "text"]

    with csv_path.open("w", encoding="utf-8", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    jsonl_path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )
    print(f"\nГотово: {csv_path}")
    print(f"JSONL:  {jsonl_path}")


def parse_args():
    parser = argparse.ArgumentParser(description="Ищет имена в титрах YouTube-плейлиста через OCR последних минут.")
    parser.add_argument("--playlist", default=DEFAULT_PLAYLIST)
    parser.add_argument("--name", dest="names", action="append", default=[], help="Имя для поиска. Можно указать несколько раз.")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="JSON-файл с настройками. По умолчанию settings.json.")
    parser.add_argument("--out", default="credits_scan")
    parser.add_argument("--limit", type=int, default=0, help="Сколько первых видео проверить. 0 = все.")
    parser.add_argument("--offset", type=int, default=0, help="Сколько видео пропустить с начала.")
    parser.add_argument("--tail-seconds", type=int, default=90, help="Сколько секунд с конца серии анализировать.")
    parser.add_argument("--frame-every", type=int, default=2, help="Брать один кадр каждые N секунд.")
    parser.add_argument("--lang", default="rus", help="Язык tesseract, например rus или rus+eng.")
    parser.add_argument("--threshold", type=float, default=0.82, help="Порог fuzzy-поиска от 0 до 1.")
    parser.add_argument("--force-ocr", action="store_true", help="Заново выполнить OCR даже если сохраненный текст уже есть.")
    parser.add_argument("--check", action="store_true", help="Только проверить зависимости и выйти.")
    return parser.parse_args()


def main():
    args = parse_args()
    args.names = get_names(args)
    require_tools()
    if args.check:
        print("Все нужные утилиты найдены.")
        return

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    items = get_playlist_items(args.playlist, out_dir / "playlist.jsonl")
    items = items[args.offset :]
    if args.limit:
        items = items[: args.limit]

    all_matches = []
    for item in items:
        try:
            all_matches.extend(scan_video(item, args, out_dir))
            write_results(all_matches, out_dir)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            print(f"  ошибка: {exc}", file=sys.stderr)

    write_results(all_matches, out_dir)


if __name__ == "__main__":
    main()
