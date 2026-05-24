import datetime
import json
import math
import os
import random
import time
from typing import Any, Dict, List, Optional

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from moviepy.audio.fx.all import audio_loop
from moviepy.editor import AudioFileClip, VideoFileClip, concatenate_videoclips
from playwright.sync_api import sync_playwright

# Patch for Pillow 10+ where ANTIALIAS was removed
import PIL.Image

if not hasattr(PIL.Image, "ANTIALIAS"):
    PIL.Image.ANTIALIAS = PIL.Image.LANCZOS


# Configuration
API_URLS = [
    "https://sbsolver.online/today.json",
    "https://spelling-bee-api.sbsolver.workers.dev/today",
]
OFFICIAL_GAME_URL = "https://www.nytimes.com/puzzles/spelling-bee"
GAME_URL = OFFICIAL_GAME_URL
INTRO_VIDEO = "intro.mp4"
BACKGROUND_MUSIC = "song1.mp3"
OUTPUT_VIDEO = "spelling_bee_daily.mp4"
THUMBNAIL_IMAGE = "youtube_thumbnail.jpg"
UPLOAD_PREVIEW_FILE = "youtube_upload_preview.json"
TODAY_PAGE_URL = "https://spellingbeesolver.dev/today/"
VIDEO_DIR = "recordings"
FINAL_FPS = 30
RANK_PCTS = [0, 0.02, 0.05, 0.08, 0.15, 0.25, 0.40, 0.50, 0.70]

# YouTube API Scopes
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def fetch_official_puzzle_date() -> Optional[Dict[str, str]]:
    try:
        response = requests.get(
            OFFICIAL_GAME_URL,
            timeout=30,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                )
            },
        )
        response.raise_for_status()

        start_marker = "window.gameData = "
        start_index = response.text.find(start_marker)
        if start_index == -1:
            return None

        json_start = start_index + len(start_marker)
        script_end = response.text.find("</script>", json_start)
        if script_end == -1:
            return None

        raw_json = response.text[json_start:script_end].strip()
        if raw_json.endswith(";"):
            raw_json = raw_json[:-1].rstrip()

        game_data = json.loads(raw_json)
        today_puzzle = game_data.get("today")
        if not today_puzzle:
            return None

        display_date = today_puzzle.get("displayDate")
        print_date = today_puzzle.get("printDate")
        if not display_date or not print_date:
            return None

        return {
            "date": display_date,
            "print_date": print_date,
        }
    except Exception as exc:
        print(f"Error fetching official puzzle date: {exc}")
        return None


def apply_official_puzzle_date(data: Dict[str, Any]) -> Dict[str, Any]:
    puzzle = data.get("puzzle", {})
    original_date = puzzle.get("date")
    if original_date:
        puzzle["source_date"] = original_date

    official_date = fetch_official_puzzle_date()
    if not official_date:
        return data

    puzzle["date"] = official_date["date"]
    puzzle["print_date"] = official_date["print_date"]
    return data


def fetch_daily_words() -> Dict[str, Any]:
    """Fetch daily answers from the API."""
    for api_url in API_URLS:
        try:
            response = requests.get(api_url, timeout=30)
            response.raise_for_status()
            payload = response.json()
            if payload.get("puzzle") and payload.get("words"):
                print(f"Loaded puzzle data from {api_url}")
                return apply_official_puzzle_date(payload)
        except Exception as exc:
            print(f"Error fetching API from {api_url}: {exc}")
    return {}


def score_valid_word(word: str) -> int:
    """
    Calculate the score of a word based on NYT Spelling Bee rules.
    4-letter words = 1 point.
    Longer words = length of word.
    Pangrams (7 unique letters) = +7 bonus points.
    """
    if len(word) == 4:
        return 1
    score = len(word)
    if len(set(word)) == 7:
        score += 7
    return score


def is_pangram_word(word: str, api_flag: Any = None) -> bool:
    return api_flag == 1 or len(set(word)) == 7


def parse_display_date(display_date: str) -> datetime.datetime:
    try:
        return datetime.datetime.strptime(display_date, "%B %d, %Y")
    except ValueError:
        return datetime.datetime.utcnow()


def warn_if_puzzle_is_stale(data: Dict[str, Any]) -> None:
    display_date = data.get("puzzle", {}).get("date")
    if not display_date:
        return

    parsed_date = parse_display_date(display_date).date()
    current_date = datetime.date.today()
    if parsed_date < current_date:
        print(
            "Warning: puzzle source returned "
            f"{display_date}, which is older than the current local date {current_date:%B %d, %Y}."
        )


def build_word_entries(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not data or "words" not in data:
        return []

    entries: List[Dict[str, Any]] = []
    for item in data["words"]:
        word = item["word"].strip().lower()
        entries.append(
            {
                "word": word,
                "length": len(word),
                "score": score_valid_word(word),
                "is_pangram": is_pangram_word(word, item.get("is_pangram")),
            }
        )
    return entries


def get_daily_rng(data: Dict[str, Any]) -> random.Random:
    puzzle = data.get("puzzle", {})
    seed = f"{puzzle.get('source_date') or puzzle.get('date', '')}|{puzzle.get('letters', '')}|{len(data.get('words', []))}"
    return random.Random(seed)


def get_human_like_word_order(data: Dict[str, Any]) -> List[str]:
    """
    Build a believable solve order:
    start with easier words, mix word lengths, and delay pangrams until later.
    """
    entries = build_word_entries(data)
    if not entries:
        return []

    rng = get_daily_rng(data)
    short_words = [item for item in entries if not item["is_pangram"] and item["length"] <= 5]
    medium_words = [item for item in entries if not item["is_pangram"] and 6 <= item["length"] <= 7]
    long_words = [item for item in entries if not item["is_pangram"] and item["length"] >= 8]
    pangrams = [item for item in entries if item["is_pangram"]]

    for pool in (short_words, medium_words, long_words, pangrams):
        rng.shuffle(pool)

    solve_plan: List[Dict[str, Any]] = []
    starter_count = min(len(short_words), rng.randint(3, 5))
    for _ in range(starter_count):
        solve_plan.append(short_words.pop())

    while len(solve_plan) < 2 and medium_words:
        solve_plan.append(medium_words.pop())

    non_pangram_target = len(short_words) + len(medium_words) + len(long_words) + len(solve_plan)
    pangram_positions: List[int] = []
    if pangrams:
        pangram_positions.append(max(5, int(non_pangram_target * rng.uniform(0.58, 0.68))))
        if len(pangrams) > 1:
            pangram_positions.append(
                max(
                    pangram_positions[0] + 3,
                    int(non_pangram_target * rng.uniform(0.78, 0.9)),
                )
            )

    pattern = ["medium", "short", "medium", "long", "short", "medium", "long"]
    pattern_index = 0
    buckets = {
        "short": short_words,
        "medium": medium_words,
        "long": long_words,
    }

    while any(buckets.values()) or pangrams:
        if pangrams and pangram_positions and len(solve_plan) >= pangram_positions[0]:
            solve_plan.append(pangrams.pop())
            pangram_positions.pop(0)
            continue

        preferred_name = pattern[pattern_index % len(pattern)]
        pattern_index += 1

        ordered_names = [preferred_name, "short", "medium", "long"]
        seen = set()
        deduped_names = []
        for name in ordered_names:
            if name not in seen:
                deduped_names.append(name)
                seen.add(name)

        selected = None
        for name in deduped_names:
            pool = buckets[name]
            if not pool:
                continue

            if rng.random() < 0.22:
                alternates = [alt for alt, alt_pool in buckets.items() if alt != name and alt_pool]
                if alternates:
                    name = rng.choice(alternates)
                    pool = buckets[name]

            selected = pool.pop()
            solve_plan.append(selected)
            break

        if selected:
            continue

        if pangrams:
            solve_plan.append(pangrams.pop())
            continue

        break

    return [item["word"] for item in solve_plan]


def maybe_shuffle_board(page, rng: random.Random, word_index: int) -> None:
    shuffle_chance = 0.0
    if word_index > 0:
        shuffle_chance = 0.18
    if word_index > 3 and word_index % 5 == 0:
        shuffle_chance = 0.32

    if rng.random() < shuffle_chance:
        page.click("button.shuffle-btn")
        time.sleep(rng.uniform(0.6, 1.3))


def type_word_human_like(page, word: str, rng: random.Random) -> None:
    correction_index: Optional[int] = None
    if len(word) >= 5 and rng.random() < 0.35:
        correction_index = rng.randint(2, len(word) - 2)

    pause_index = len(word) // 2 if len(word) > 4 else None

    for index, char in enumerate(word):
        page.keyboard.type(char)
        time.sleep(rng.uniform(0.14, 0.33))

        if correction_index is not None and index == correction_index:
            time.sleep(rng.uniform(0.15, 0.35))
            page.keyboard.press("Backspace")
            time.sleep(rng.uniform(0.25, 0.55))
            page.keyboard.type(char)
            time.sleep(rng.uniform(0.12, 0.24))

        if pause_index is not None and index == pause_index and rng.random() < 0.32:
            time.sleep(rng.uniform(0.3, 0.95))


def run_browser_automation(words: List[str]) -> None:
    """Run Playwright automation to play the game and record."""
    if not os.path.exists(VIDEO_DIR):
        os.makedirs(VIDEO_DIR)

    rng = random.Random(time.time())

    with sync_playwright() as playwright:
        is_headless = os.environ.get("GITHUB_ACTIONS") == "true"
        browser = playwright.chromium.launch(headless=is_headless)
        context = browser.new_context(
            record_video_dir=VIDEO_DIR,
            record_video_size={"width": 1280, "height": 720},
            viewport={"width": 1280, "height": 720},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )

        context.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
            """
        )

        page = context.new_page()
        next_long_break_at = rng.randint(5, 7)

        try:
            print("Navigating to game...")
            page.goto(GAME_URL, wait_until="domcontentloaded")

            try:
                page.wait_for_selector("button:has-text('Play')", timeout=5000)
                page.click("button:has-text('Play')")
            except Exception:
                pass

            time.sleep(rng.uniform(1.8, 2.8))

            for word_index, word in enumerate(words):
                print(f"Typing word {word_index + 1}/{len(words)}: {word}")

                maybe_shuffle_board(page, rng, word_index)

                thinking_delay = rng.uniform(1.2, 2.8) + min(len(word) * 0.1, 1.0)
                if word_index == 0:
                    thinking_delay += rng.uniform(0.8, 1.6)
                time.sleep(thinking_delay)

                type_word_human_like(page, word, rng)
                time.sleep(rng.uniform(0.45, 1.2))
                page.keyboard.press("Enter")
                time.sleep(rng.uniform(1.0, 1.7))

                try:
                    rank_label = page.query_selector("#rank-name")
                    if rank_label:
                        current_rank = rank_label.inner_text().strip()
                        if current_rank == "Genius":
                            print("Target rank 'Genius' reached. Stopping automation.")
                            time.sleep(3)
                            break

                    if page.get_by_text("Queen Bee").is_visible():
                        print("Queen Bee reached. Stopping automation.")
                        break
                except Exception:
                    pass

                if word_index + 1 == next_long_break_at:
                    time.sleep(rng.uniform(4.5, 7.5))
                    next_long_break_at += rng.randint(4, 7)
                else:
                    time.sleep(rng.uniform(1.8, 4.6))

            time.sleep(4)

        except Exception as exc:
            print(f"Browser automation error: {exc}")
        finally:
            page.close()
            context.close()
            browser.close()


def generate_thumbnail_image(video_clip: VideoFileClip) -> Optional[str]:
    try:
        frame_time = min(max(video_clip.duration * 0.35, 0.5), max(video_clip.duration - 0.1, 0))
        video_clip.save_frame(THUMBNAIL_IMAGE, t=frame_time)
        return THUMBNAIL_IMAGE if os.path.exists(THUMBNAIL_IMAGE) else None
    except Exception as exc:
        print(f"Thumbnail generation failed: {exc}")
        return None


def process_video() -> Dict[str, Any]:
    """Merge intro video, gameplay recording, and looped background music."""
    recordings = [
        os.path.join(VIDEO_DIR, file_name)
        for file_name in os.listdir(VIDEO_DIR)
        if file_name.endswith(".webm")
    ]
    if not recordings:
        print("No recordings found.")
        return {}

    latest_recording = max(recordings, key=os.path.getctime)
    print(f"Processing recording: {latest_recording}")

    gameplay_clip: Optional[VideoFileClip] = None
    intro_clip: Optional[VideoFileClip] = None
    music_clip: Optional[AudioFileClip] = None
    final_clip = None

    try:
        gameplay_clip = VideoFileClip(latest_recording)
        target_size = gameplay_clip.size

        if os.path.exists(INTRO_VIDEO):
            intro_clip = VideoFileClip(INTRO_VIDEO).resize(newsize=target_size).set_fps(FINAL_FPS)
        else:
            print(f"Warning: {INTRO_VIDEO} not found. Skipping intro.")

        if os.path.exists(BACKGROUND_MUSIC):
            music_clip = AudioFileClip(BACKGROUND_MUSIC)
            looped_music = audio_loop(music_clip, duration=gameplay_clip.duration).subclip(0, gameplay_clip.duration)
            gameplay_clip = gameplay_clip.set_audio(looped_music.volumex(0.18))
        else:
            print(f"Warning: {BACKGROUND_MUSIC} not found. Gameplay will keep original audio.")

        clips = [gameplay_clip]
        if intro_clip:
            clips.insert(0, intro_clip)

        final_clip = concatenate_videoclips(clips, method="compose")
        final_clip.write_videofile(
            OUTPUT_VIDEO,
            codec="libx264",
            audio_codec="aac",
            fps=FINAL_FPS,
        )
        print(f"Video saved to {OUTPUT_VIDEO}")

        thumbnail_path = generate_thumbnail_image(intro_clip or gameplay_clip)
        return {
            "video_file": OUTPUT_VIDEO,
            "thumbnail_path": thumbnail_path,
            "intro_duration": round(intro_clip.duration, 2) if intro_clip else 0,
            "gameplay_duration": round(gameplay_clip.duration, 2),
            "total_duration": round(final_clip.duration, 2),
        }
    except Exception as exc:
        print(f"Video processing error: {exc}")
        return {}
    finally:
        if final_clip:
            final_clip.close()
        if intro_clip:
            intro_clip.close()
        if gameplay_clip:
            gameplay_clip.close()
        if music_clip:
            music_clip.close()


def format_timestamp(total_seconds: float) -> str:
    rounded_seconds = max(0, int(total_seconds))
    hours, remainder = divmod(rounded_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def build_chapters(video_details: Dict[str, Any]) -> List[str]:
    total_duration = video_details.get("total_duration", 0)
    intro_duration = video_details.get("intro_duration", 0)
    if total_duration < 35:
        return []

    gameplay_start = max(0, int(math.ceil(intro_duration)))
    midpoint = int(gameplay_start + max((total_duration - gameplay_start) * 0.45, 10))
    finish = int(max(midpoint + 10, total_duration - 15))

    chapter_candidates = [
        (0, "Intro"),
        (gameplay_start, "Solve begins"),
        (midpoint, "More answers and progress"),
        (min(int(total_duration) - 1, finish), "Final words and Genius"),
    ]

    chapters: List[str] = []
    last_second = -10
    for second, label in chapter_candidates:
        if second - last_second < 10 and chapters:
            continue
        chapters.append(f"{format_timestamp(second)} {label}")
        last_second = second

    return chapters if len(chapters) >= 3 else []


def build_upload_metadata(data: Dict[str, Any], video_details: Dict[str, Any]) -> Dict[str, Any]:
    display_date = data["puzzle"]["date"]
    print_date = data.get("puzzle", {}).get("print_date")
    if isinstance(print_date, str) and print_date:
        recording_date = f"{print_date}T00:00:00Z"
    else:
        parsed_date = parse_display_date(display_date)
        recording_date = parsed_date.strftime("%Y-%m-%dT00:00:00Z")
    chapters = build_chapters(video_details)

    title = f"Spelling Bee Answer Today | NYT Spelling Bee Answers - {display_date}"

    description_parts = [
        (
            f"Spelling Bee answer today for {display_date}. "
            f"This NYT Spelling Bee answer today video shows the pangram, the full word list, "
            f"and a complete solve to Genius."
        ),
        (
            "If you searched for spelling bee answers today or NYT spelling bee answer today, "
            "this walkthrough has the full puzzle in one place."
        ),
        "Full answer page:",
        TODAY_PAGE_URL,
        "",
        "What you will see:",
        "- The full Spelling Bee answers today list",
        "- The pangram for today's NYT Spelling Bee",
        "- A natural-looking solve from the opening board to Genius",
        "- Quick replay points with video chapters",
    ]

    if chapters:
        description_parts.extend(["", "Chapters:"])
        description_parts.extend(chapters)

    description_parts.extend(
        [
            "",
            "#SpellingBee #NYTSpellingBee #SpellingBeeAnswers",
        ]
    )

    tags = [
        "spelling bee answer today",
        "spelling bee answers today",
        "nyt spelling bee answer today",
        "nyt spelling bee answers today",
        "spelling bee today",
        "spelling bee answers",
        "nyt spelling bee",
        "new york times spelling bee",
        "spelling bee pangram today",
        "daily spelling bee answers",
        "spelling bee puzzle answers",
        "word puzzle answers today",
        "spellingbee answer today",
        "speling bee answer today",
        f"spelling bee {display_date.lower()}",
        f"nyt spelling bee {display_date.lower()}",
    ]

    return {
        "title": title,
        "description": "\n".join(description_parts),
        "tags": tags,
        "category_id": "27",
        "recording_date": recording_date,
        "thumbnail_path": video_details.get("thumbnail_path"),
        "default_language": "en",
    }


def write_upload_preview(metadata: Dict[str, Any]) -> None:
    preview_payload = {
        "title": metadata["title"],
        "description": metadata["description"],
        "tags": metadata["tags"],
        "categoryId": metadata["category_id"],
        "recordingDate": metadata["recording_date"],
        "thumbnailPath": metadata.get("thumbnail_path"),
        "defaultLanguage": metadata["default_language"],
    }

    with open(UPLOAD_PREVIEW_FILE, "w", encoding="utf-8") as file_handle:
        json.dump(preview_payload, file_handle, ensure_ascii=False, indent=2)

    print(f"Upload metadata preview saved to {UPLOAD_PREVIEW_FILE}")


def get_authenticated_service():
    """Authenticate and return YouTube service."""
    creds = None

    token_env = os.environ.get("YOUTUBE_TOKEN")
    if token_env:
        try:
            token_data = json.loads(token_env)
            creds = Credentials.from_authorized_user_info(token_data, SCOPES)
            print("Using credentials from environment variable.")
        except Exception as exc:
            print(f"Error loading token from environment: {exc}")

    if not creds and os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists("client_secret.json"):
                print("client_secret.json not found for YouTube upload.")
                return None

            flow = InstalledAppFlow.from_client_secrets_file("client_secret.json", SCOPES)
            creds = flow.run_local_server(port=0)
        with open("token.json", "w", encoding="utf-8") as token_file:
            token_file.write(creds.to_json())

    return build("youtube", "v3", credentials=creds)


def upload_thumbnail(youtube, video_id: str, thumbnail_path: Optional[str]) -> None:
    if not thumbnail_path or not os.path.exists(thumbnail_path):
        return

    try:
        media = MediaFileUpload(thumbnail_path, mimetype="image/jpeg")
        youtube.thumbnails().set(videoId=video_id, media_body=media).execute()
        print(f"Custom thumbnail uploaded from {thumbnail_path}")
    except Exception as exc:
        print(f"Thumbnail upload failed: {exc}")


def upload_to_youtube(video_file: str, metadata: Dict[str, Any]) -> None:
    """Upload video to YouTube."""
    youtube = get_authenticated_service()
    if not youtube:
        return

    body = {
        "snippet": {
            "title": metadata["title"],
            "description": metadata["description"],
            "tags": metadata["tags"],
            "categoryId": metadata["category_id"],
            "defaultLanguage": metadata["default_language"],
        },
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False,
            "embeddable": True,
            "license": "youtube",
            "publicStatsViewable": True,
        },
        "recordingDetails": {
            "recordingDate": metadata["recording_date"],
        },
        "localizations": {
            "en": {
                "title": metadata["title"],
                "description": metadata["description"],
            }
        },
    }

    media = MediaFileUpload(video_file, chunksize=-1, resumable=True)

    try:
        print(f"Uploading {video_file}...")
        request = youtube.videos().insert(
            part="snippet,status,recordingDetails,localizations",
            body=body,
            media_body=media,
        )
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                print(f"Uploaded {int(status.progress() * 100)}%")
        print(f"Upload complete! Video ID: {response['id']}")
        upload_thumbnail(youtube, response["id"], metadata.get("thumbnail_path"))
    except Exception as exc:
        print(f"Upload failed: {exc}")


def generate_local_html(data: Dict[str, Any]) -> Optional[str]:
    """Read custom template, inject API data, and save local HTML."""
    try:
        template_path = "custom_game_template.html"
        output_path = os.path.abspath("local_game.html")

        if not os.path.exists(template_path):
            print("Custom template not found!")
            return None

        with open(template_path, "r", encoding="utf-8") as file_handle:
            content = file_handle.read()

        puzzle = data["puzzle"]
        game_data = {
            "displayDate": puzzle["date"],
            "centerLetter": puzzle["letters"].lower(),
            "outerLetters": [letter.lower() for letter in puzzle["all_letters"] if letter != puzzle["letters"]],
            "validLetters": [letter.lower() for letter in puzzle["all_letters"]],
            "answers": [word_item["word"] for word_item in data["words"]],
        }

        json_str = json.dumps(game_data)
        init_call = f"<script>window.startLocalGame({json_str});</script>"
        content = content.replace("</body>", f"{init_call}</body>")

        with open(output_path, "w", encoding="utf-8") as file_handle:
            file_handle.write(content)

        return output_path
    except Exception as exc:
        print(f"Error generating local HTML: {exc}")
        return None


def main() -> None:
    print("Starting Spelling Bee Automation...")

    data = fetch_daily_words()
    if not data:
        return
    warn_if_puzzle_is_stale(data)

    words = get_human_like_word_order(data)
    if not words:
        print("No words found.")
        return
    print(f"Found {len(words)} words.")

    game_file = generate_local_html(data)
    if not game_file:
        print("Failed to generate local game file.")
        return

    global GAME_URL
    GAME_URL = f"file:///{game_file.replace(os.sep, '/')}"
    print(f"Using local game file: {GAME_URL}")

    run_browser_automation(words)

    video_details = process_video()
    if not video_details:
        return

    metadata = build_upload_metadata(data, video_details)
    write_upload_preview(metadata)

    if os.environ.get("YOUTUBE_TOKEN") or os.path.exists("token.json"):
        upload_to_youtube(OUTPUT_VIDEO, metadata)
    else:
        print("No YouTube credentials found. Skipping upload.")


if __name__ == "__main__":
    main()
