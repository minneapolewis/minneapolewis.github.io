import os
import json
import time
import requests
import tweepy
from dotenv import load_dotenv
import random
from bs4 import BeautifulSoup
import re
from datetime import datetime
from zoneinfo import ZoneInfo

load_dotenv()

# ─── Configuration ──────────────────────────────────────────────────────────────
ENABLE_TWITTER_POSTING = False  # Master kill switch. Set to True to reactivate tweeting.

PROFILES_TO_TRACK = [
    {
        "username": "alquis",
        "tweet_message": "Keira just got sent {amount} from {sender_name} at {est_time} EST"
    },
    {
        "username": "gnnx",
        "tweet_message": "Gianna was sent {amount} from {sender_name} at {est_time} EST"
    },
    {
        "username": "lili",
        "tweet_message": "Lili just got sent {amount} from {sender_name} at {est_time} EST"
    }
]

STATE_FILE = "last_sends.json"
API_URL = "https://us-east1-sent-wc254r.cloudfunctions.net/recentSends"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:129.0) Gecko/20100101 Firefox/129.0",
]

MAX_API_RETRIES = 3
RETRY_DELAY_SECONDS = 6

TWEET_RETRY_COUNT = 4
TWEET_BASE_DELAY = 8      # seconds
TWEET_JITTER_RANGE = (0, 7)

# ─── Helper Functions ───────────────────────────────────────────────────────────
def get_user_uid(username):
    profile_url = f"https://sent.bio/{username}"
    print(f"Scraping {profile_url} to find user UID...")
    for attempt in range(MAX_API_RETRIES):
        try:
            headers = {'User-Agent': random.choice(USER_AGENTS)}
            response = requests.get(profile_url, headers=headers, timeout=15)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')
            meta_tags = soup.find_all('meta', property='og:image')
            if not meta_tags:
                return None
            for tag in meta_tags:
                if not tag.has_attr('content'):
                    continue
                image_url = tag['content']
                if "public_users" in image_url:
                    match = re.search(r"public_users(?:/|%2F)([a-zA-Z0-9]+)(?:/|%2F)", image_url)
                    if match:
                        uid = match.group(1)
                        print(f"Successfully found UID for {username}: {uid}")
                        return uid
            print(f"No user-specific og:image found for {username}.")
            return None
        except requests.exceptions.RequestException as e:
            print(f"UID fetch attempt {attempt+1} failed: {e}")
            if attempt < MAX_API_RETRIES - 1:
                time.sleep(RETRY_DELAY_SECONDS)
    print(f"Failed to get UID for {username} after {MAX_API_RETRIES} attempts.")
    return None


def get_recent_sends(uid, username_for_logging):
    print(f"Fetching recent sends for '{username_for_logging}' (UID: {uid})...")
    for attempt in range(MAX_API_RETRIES):
        try:
            payload = {"data": {"receiverUid": uid}}
            headers = {"Content-Type": "application/json", "User-Agent": random.choice(USER_AGENTS)}
            response = requests.post(API_URL, headers=headers, json=payload, timeout=15)
            response.raise_for_status()
            api_data = response.json()
            sends_list = api_data.get('result', [])
            sends = []
            for item in sends_list:
                sender_name = item.get('sender_name', 'Unknown').strip()
                amount = item.get('amount', 0)
                currency_symbol = item.get('sender_currency_symbol', '$')
                formatted_amount = f"{currency_symbol}{amount}"
                sends.append({"sender": sender_name, "amount": formatted_amount})
            return sends
        except Exception as e:
            print(f"API attempt {attempt+1} failed: {e}")
            if attempt < MAX_API_RETRIES - 1:
                time.sleep(RETRY_DELAY_SECONDS)
    print(f"Failed to fetch sends for {username_for_logging}.")
    return []


def read_state():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    except json.JSONDecodeError:
        print("State file corrupted → starting fresh.")
        return {}


def write_state(data):
    with open(STATE_FILE, 'w') as f:
        json.dump(data, f, indent=2)
    print(f"State saved to {STATE_FILE}")


def post_to_twitter(message):
    for attempt in range(TWEET_RETRY_COUNT):
        try:
            client = tweepy.Client(
                consumer_key=os.environ['TWITTER_API_KEY'],
                consumer_secret=os.environ['TWITTER_API_SECRET'],
                access_token=os.environ['TWITTER_ACCESS_TOKEN'],
                access_token_secret=os.environ['TWITTER_ACCESS_TOKEN_SECRET']
            )
            response = client.create_tweet(text=message)
            print(f"Tweet posted successfully → ID: {response.data['id']}")
            return True
        except tweepy.TweepyException as e:
            if hasattr(e, 'response') and e.response is not None:
                status = e.response.status_code
                if status in (429, 500, 502, 503, 504):
                    wait = TWEET_BASE_DELAY * (1.6 ** attempt) + random.uniform(*TWEET_JITTER_RANGE)
                    print(f"Twitter transient error {status} (attempt {attempt+1}/{TWEET_RETRY_COUNT}) → retry in ~{wait:.1f}s")
                    time.sleep(wait)
                    continue
                elif status in (401, 403):
                    print(f"Auth/Permission error {status}: {e} (likely free tier restriction or app setup issue)")
                    return False
            print(f"Permanent Twitter error: {e}")
            return False
    print("Failed to post tweet after all retries.")
    return False


# ─── Core Profile Processing ────────────────────────────────────────────────────
def process_profile(profile, all_states, target_timezone):
    username = profile["username"]
    print(f"\n─── Checking profile: {username} ───")

    user_state = all_states.get(username, {"uid": None, "sends": []})
    uid = user_state.get("uid")

    if not uid:
        print(f"No cached UID → fetching from profile page...")
        uid = get_user_uid(username)
        if uid:
            user_state["uid"] = uid
            all_states[username] = user_state
        else:
            print("Could not obtain UID. Skipping.")
            return False

    recent_sends_api = get_recent_sends(uid, username)
    if not recent_sends_api:
        print("No sends returned from API (fetch failed or empty). No state update.")
        return False

    print(f"Found {len(recent_sends_api)} recent send(s) from API.")

    # 1. Retrieve full history
    stored_history = user_state.get("sends", [])

    # 2. Extract signatures (Sender, Amount) from API and History for comparison
    api_sigs = [(item['sender'], item['amount']) for item in recent_sends_api]
    hist_sigs = [(item['sender'], item['amount']) for item in stored_history]

    # 3. SEQUENCE PATTERN MATCHING
    new_sends_to_process = []
    
    for i in range(len(api_sigs) + 1):
        api_slice = api_sigs[i:]
        hist_slice = hist_sigs[:len(api_slice)]
        
        if api_slice == hist_slice:
            new_sends_to_process = recent_sends_api[:i]
            break

    if not new_sends_to_process:
        print("No new sends this run (API data exactly matches the top of our saved history).")
        return False

    print(f"→ {len(new_sends_to_process)} NEW send(s) detected! Adding timestamps and saving...")

    # 4. Add timestamps to the new sends
    now_est = datetime.now(target_timezone)
    timestamp_str = now_est.strftime("%Y-%m-%d %H:%M:%S %Z")
    time_str_short = now_est.strftime("%H:%M")

    for send in new_sends_to_process:
        send['detected_at'] = timestamp_str

    # 5. Prepend to history (Newest at TOP)
    updated_history = new_sends_to_process + stored_history
    
    user_state["sends"] = updated_history
    all_states[username] = user_state

    # 6. Tweet (Deactivated via toggle)
    if ENABLE_TWITTER_POSTING:
        tweet_list = list(reversed(new_sends_to_process))
        tweet_counts = {}
        
        tweets_succeeded = True
        for send in tweet_list:
            base_text = profile["tweet_message"].format(
                amount=send['amount'],
                sender_name=send['sender'],
                est_time=time_str_short
            )
            
            final_message = base_text
            
            # Jitter duplicate text protection
            tweet_counts[base_text] = tweet_counts.get(base_text, 0) + 1
            if tweet_counts[base_text] > 1:
                marker = ''.join(random.choices('abcdefghijklmnopqrstuvwxyz', k=2))
                final_message += f" [{marker}]"

            print(f"  → Attempting tweet: {final_message}")

            if not post_to_twitter(final_message):
                print("  → Tweet failed (continuing; state already saved)")
                tweets_succeeded = False

            time.sleep(random.uniform(10, 22))

        if tweets_succeeded:
            print(f"All new tweets succeeded for {username}")
    else:
        print(f"  → Twitter posting is disabled. Data saved locally.")

    return True


# ─── Main ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Starting sent.bio scraper (Sequence Matching Mode)...")
    all_states = read_state()
    global_state_changed = False
    tz_est = ZoneInfo("America/New_York")

    for profile in PROFILES_TO_TRACK:
        if process_profile(profile, all_states, tz_est):
            global_state_changed = True

    if global_state_changed:
        print("\nState changed (history updated) → saving file...")
        write_state(all_states)
    else:
        print("\nNo changes this run.")
