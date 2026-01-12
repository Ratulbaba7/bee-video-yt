import json
import random
import time
import os
import datetime
from typing import List, Dict, Any

import requests
from playwright.sync_api import sync_playwright, Page

# Patch for Pillow 10+ where ANTIALIAS was removed
import PIL.Image
if not hasattr(PIL.Image, 'ANTIALIAS'):
    PIL.Image.ANTIALIAS = PIL.Image.LANCZOS

from moviepy.editor import ImageClip, VideoFileClip, concatenate_videoclips
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# Configuration
API_URL = "https://spelling-bee-api.sbsolver.workers.dev/today"
GAME_URL = "https://www.nytimes.com/puzzles/spelling-bee"
INTRO_IMAGE = "intro.png"
OUTPUT_VIDEO = "spelling_bee_daily.mp4"
VIDEO_DIR = "recordings"

# YouTube API Scopes
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

def fetch_daily_words() -> Dict[str, Any]:
    """Fetch daily answers from the API."""
    try:
        response = requests.get(API_URL)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"Error fetching API: {e}")
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

def get_prioritized_words(data: Dict[str, Any]) -> List[str]:
    """
    Extract words, sort by score (descending).
    Ensure the first word is NOT a pangram.
    """
    if not data or "words" not in data:
        return []
    
    # Calculate score for each word
    scored_words = []
    for item in data["words"]:
        word = item["word"]
        score = score_valid_word(word)
        # API provides is_pangram, but we can also check set length
        is_pangram = item.get("is_pangram", 0) == 1 or len(set(word)) == 7
        scored_words.append({"word": word, "score": score, "is_pangram": is_pangram})
    
    # Sort by score descending
    scored_words.sort(key=lambda x: x["score"], reverse=True)
    
    # Check if first word is a pangram
    if scored_words and scored_words[0]["is_pangram"]:
        # Find the first non-pangram
        for i, item in enumerate(scored_words):
            if not item["is_pangram"]:
                # Move this non-pangram to the front
                non_pangram = scored_words.pop(i)
                scored_words.insert(0, non_pangram)
                break
    
    return [item["word"] for item in scored_words]

def run_browser_automation(words: List[str]):
    """Run Playwright automation to play the game and record."""
    if not os.path.exists(VIDEO_DIR):
        os.makedirs(VIDEO_DIR)

    with sync_playwright() as p:
        # Detect if running in GitHub Actions for Headless mode
        is_headless = os.environ.get("GITHUB_ACTIONS") == "true"
        browser = p.chromium.launch(headless=is_headless)
        context = browser.new_context(
            record_video_dir=VIDEO_DIR,
            record_video_size={"width": 1280, "height": 720},
            viewport={"width": 1280, "height": 720},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        
        # Stealth scripts
        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
        """)

        page = context.new_page()
        
        try:
            print("Navigating to game...")
            # Use 'domcontentloaded' to avoid waiting for stalled external scripts
            page.goto(GAME_URL, wait_until="domcontentloaded")
            
            # Handle "Play" button if present
            try:
                page.wait_for_selector("button:has-text('Play')", timeout=5000)
                page.click("button:has-text('Play')")
            except:
                pass # Play button might be handled by injected script or already passed

            time.sleep(2)

            for word in words:
                print(f"Typing word: {word}")
                # Simulate human-like typing (slower)
                for char in word:
                    page.keyboard.type(char)
                    # Slower typing: 0.2 to 0.5 seconds
                    time.sleep(random.uniform(0.2, 0.5)) 
                
                time.sleep(random.uniform(0.3, 0.6))
                page.keyboard.press("Enter")
                time.sleep(1.5) # Wait for UI to update (score/messages)
                
                # Check for stop condition: "Genius" rank
                try:
                    # Look for the rank label in our new custom UI (id="rank-name") or standard text
                    rank_label = page.query_selector("#rank-name")
                    if rank_label:
                        current_rank = rank_label.inner_text().strip()
                        if current_rank == "Genius":
                            print("Target rank 'Genius' reached. Stopping automation.")
                            # Give it a moment to show the success toast
                            time.sleep(3)
                            break
                    
                    # Fallback or additional checks requested by user
                    if page.get_by_text("Queen Bee").is_visible():
                        print("Queen Bee reached. Stopping.")
                        break
                except:
                    pass

                # Wait 6-7 seconds between words as requested
                time.sleep(random.uniform(6.0, 7.0))

            # Wait a bit after finishing
            time.sleep(5)
            
        except Exception as e:
            print(f"Browser automation error: {e}")
        finally:
            page.close()
            context.close()
            browser.close()

def process_video():
    """Merge intro image and recorded gameplay."""
    # Find the latest recording
    recordings = [os.path.join(VIDEO_DIR, f) for f in os.listdir(VIDEO_DIR) if f.endswith(".webm")]
    if not recordings:
        print("No recordings found.")
        return

    latest_recording = max(recordings, key=os.path.getctime)
    print(f"Processing recording: {latest_recording}")

    try:
        gameplay_clip = VideoFileClip(latest_recording)
        
        # Create intro clip
        if os.path.exists(INTRO_IMAGE):
            # Enforce 30fps and resizing to match gameplay exactly
            intro_clip = ImageClip(INTRO_IMAGE).set_duration(5).set_fps(30)
            intro_clip = intro_clip.resize(newsize=gameplay_clip.size)
        else:
            print(f"Warning: {INTRO_IMAGE} not found. Skipping intro.")
            intro_clip = None

        if intro_clip:
            # method='compose' is slower but prevents glitches when mixing formats
            final_clip = concatenate_videoclips([intro_clip, gameplay_clip], method="compose") 
        else:
            final_clip = gameplay_clip

        # Write result with constant FPS to ensure compatibility
        final_clip.write_videofile(OUTPUT_VIDEO, codec="libx264", audio_codec="aac", fps=30)
        print(f"Video saved to {OUTPUT_VIDEO}")
        
    except Exception as e:
        print(f"Video processing error: {e}")

def get_authenticated_service():
    """Authenticate and return YouTube service."""
    creds = None
    
    # Check for token in environment variable first (for GitHub Actions)
    token_env = os.environ.get("YOUTUBE_TOKEN")
    if token_env:
        try:
            token_data = json.loads(token_env)
            creds = Credentials.from_authorized_user_info(token_data, SCOPES)
            print("Using credentials from environment variable.")
        except Exception as e:
            print(f"Error loading token from environment: {e}")

    # Fallback to token.json file
    if not creds and os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists("client_secret.json"):
                print("client_secret.json not found for YouTube upload.")
                return None
                
            flow = InstalledAppFlow.from_client_secrets_file(
                "client_secret.json", SCOPES
            )
            creds = flow.run_local_server(port=0)
        with open("token.json", "w") as token:
            token.write(creds.to_json())

    return build("youtube", "v3", credentials=creds)

def upload_to_youtube(video_file, data):
    """Upload video to YouTube."""
    youtube = get_authenticated_service()
    if not youtube:
        return

    date_str = data["puzzle"].get("date", datetime.date.today().strftime("%B %d, %Y"))
    title = f"NYT Spelling Bee {date_str} Answer | Today's Solution"
    description = f"Here are the answers for the NYT Spelling Bee on {date_str}.\n\nPlay the game: {GAME_URL}"
    tags = ["NYT Spelling Bee", "Spelling Bee Answers", "Spelling Bee Solver", "NYT Games"]

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags,
            "categoryId": "20", # Gaming
        },
        "status": {
            "privacyStatus": "private", # Default to private for safety
            "selfDeclaredMadeForKids": False,
        }
    }

    media = MediaFileUpload(video_file, chunksize=-1, resumable=True)
    
    try:
        print(f"Uploading {video_file}...")
        request = youtube.videos().insert(
            part="snippet,status",
            body=body,
            media_body=media
        )
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                print(f"Uploaded {int(status.progress() * 100)}%")
        print(f"Upload complete! Video ID: {response['id']}")
    except Exception as e:
        print(f"Upload failed: {e}")

def main():
    print("Starting Spelling Bee Automation...")
    
    # 1. Fetch Data
    data = fetch_daily_words()
    if not data:
        return

    # 2. Get Prioritized Words
    words = get_prioritized_words(data)
    if not words:
        print("No words found.")
        return
    print(f"Found {len(words)} words.")

    # 3. Generate Local HTML
    game_file = generate_local_html(data)
    if not game_file:
        print("Failed to generate local game file.")
        return
        
    # Update global GAME_URL to point to the local file
    global GAME_URL
    GAME_URL = f"file:///{game_file.replace(os.sep, '/')}"
    print(f"Using local game file: {GAME_URL}")

    # 4. Run Browser Automation
    run_browser_automation(words)

    # 5. Process Video
    process_video()

    # 6. Upload to YouTube
    # Only upload if the video was created successfully
    if os.path.exists(OUTPUT_VIDEO):
       # Upload if YOUTUBE_TOKEN is provided OR if we are running locally with local_game.html
       if os.environ.get("YOUTUBE_TOKEN") or os.path.exists("token.json"):
           upload_to_youtube(OUTPUT_VIDEO, data)
       else:
           print("No YouTube credentials found. Skipping upload.")

def generate_local_html(data: Dict[str, Any]) -> str:
    """Read custom template, inject API data, and save local HTML."""
    try:
        template_path = "custom_game_template.html"
        output_path = os.path.abspath("local_game.html")
        
        if not os.path.exists(template_path):
            print("Custom template not found!")
            return None

        with open(template_path, "r", encoding="utf-8") as f:
            content = f.read()
            
        # Prepare Data
        puzzle = data["puzzle"]
        game_data = {
            "displayDate": puzzle["date"],
            "centerLetter": puzzle["letters"].lower(),
            "outerLetters": [l.lower() for l in puzzle["all_letters"] if l != puzzle["letters"]],
            "validLetters": [l.lower() for l in puzzle["all_letters"]],
            "answers": [w["word"] for w in data["words"]],
        }
        
        # Inject Data Call
        json_str = json.dumps(game_data)
        init_call = f"<script>window.startLocalGame({json_str});</script>"
        
        content = content.replace("</body>", f"{init_call}</body>")

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(content)
            
        return output_path
    except Exception as e:
        print(f"Error generating local HTML: {e}")
        return None

if __name__ == "__main__":
    main()
