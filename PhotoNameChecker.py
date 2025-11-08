import streamlit as st
import os
import unicodedata
import re
import requests
from bs4 import BeautifulSoup
import pandas as pd
from urllib.parse import urlparse, parse_qs
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
import time

# --- Common HTTP Headers to avoid basic bot detection and connection issues ---
COMMON_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5',
    'Connection': 'keep-alive'
}

# --- Filename parsing (Unchanged) ---

def parse_filenames(folder_or_files):
    """
    Parse filenames in the format:
      school_last_first[_anything].png
    Ignores any extra suffixes after first name.
    """
    parsed = []
    for file in folder_or_files:
        name = os.path.basename(file)
        if not name.lower().endswith(".png"):
            continue

        base = name[:-4]  # strip .png
        parts = base.split("_")

        if len(parts) < 3:
            parsed.append({
                "filename": name,
                "school": None,
                "last": None,
                "first": None,
                "format_valid": False,
                "format_msg": "Too few parts: need school_last_first"
            })
            continue

        school, last, first = parts[0], parts[1], parts[2]

        parsed.append({
            "filename": name,
            "school": school.lower().strip(),
            "last": last.lower().strip(),
            "first": first.lower().strip(),
            "format_valid": True,
            "format_msg": None
        })

    return parsed

# --- Normalization / roster scraping ---

def normalize(name: str) -> str:
    """
    Normalize names to your conventions, including removing nicknames.
    Handles nicknames in single or double quotes.
    """
    name = name.lower()
    # Remove nicknames in single or double quotes: "nickname" or 'nickname'
    name = re.sub(r'(["“”‘’\']).*?\1', '', name)
    # Remove suffixes like Jr, Sr, II, III
    name = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", name)
    # Remove accents
    name = unicodedata.normalize("NFD", name)
    name = "".join(c for c in name if not unicodedata.combining(c))
    # Remove non-word characters except spaces/hyphens
    name = re.sub(r"[^\w\s-]", "", name)
    # Collapse multiple spaces and trim
    name = re.sub(r"\s+", " ", name).strip()
    return name

# --- Baylor scraping functions (use Selenium, no change needed) ---

def scrape_baylor_players(url: str):
    import requests
    from bs4 import BeautifulSoup

    players = {}
    nickname_names = {}

    r = requests.get(url, headers=COMMON_HEADERS, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    for a in soup.select("div[data-test-id='s-person-details__personal-single-line'] a"):
        href = a.get("href") or ""
        if "/coaches/" in href:
            continue
        h3 = a.find("h3")
        if h3:
            name = h3.get_text(strip=True)
            players[normalize(name)] = name
    return players, nickname_names
    
def scrape_baylor_staff(url: str):
    """
    Scrape Baylor staff/coaches using Requests + BeautifulSoup (no Selenium).
    Returns a dictionary: {normalized_name: {"name": original_name, "title": "Coach"}}
    """
    import requests
    from bs4 import BeautifulSoup

    staff_dict = {}

    try:
        r = requests.get(url, headers=COMMON_HEADERS, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        # Select all the 'personal single line' divs
        for div in soup.select("div[data-test-id='s-person-details__personal-single-line'] a"):
            href = div.get("href") or ""
            h3 = div.find("h3")
            if not h3:
                continue
            name = h3.get_text(strip=True)

            # Only pick coaches/staff (links with '/coaches/')
            if "/coaches/" in href:
                staff_dict[normalize(name)] = {"name": name, "title": "Coach"}

        return staff_dict

    except Exception as e:
        st.error(f"Error scraping Baylor staff (BS version): {e}")
        return {}


def contains_invalid_word(name: str, invalid_words: list[str]) -> bool:
    """
    Returns True if any invalid word matches as a whole word in the name.
    """
    name_lower = name.lower()
    for word in invalid_words:
        if re.search(rf"\b{re.escape(word)}\b", name_lower):
            return True
    return False

def clean_roster_name(raw_name: str) -> str:
    """
    Ensure roster names are consistently 'First Last'
    even if site lists them as 'Last, First'.
    """
    raw_name = raw_name.strip()
    if "," in raw_name:  # handles "Alexander, Kael"
        parts = [p.strip() for p in raw_name.split(",", 1)]
        if len(parts) == 2:
            last, first = parts
            return f"{first} {last}"
    return raw_name

def scrape_player_names(url: str):
    """
    Scrape player names from a roster page.
    Returns two dictionaries:
        primary_names: {normalized_name: original_name}
        nickname_names: {normalized_name: original_name} (if nicknames detected)
    """
    is_baylor = "baylorbears.com" in url.lower()
    is_stetson = "stetson.edu" in url.lower()  # Stetson-specific

    found_names = set()

    invalid_keywords = [
        "news", "schedule", "statistics", "videos",
        "links", "gameday", "staff", "coach", "bio", "media",
        "ireland", "tarheels2ireland", "central", "additional",
        "more", "results", "events", "©", "menu", "25fb", "2025",
        "photo", "headshot"
    ]

    try:
        # **FIX 1 & 2:** Use common headers and increased timeout
        resp = requests.get(url, timeout=30, headers=COMMON_HEADERS)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # --- George Mason specific scraping ---
        if "gomason.com" in url.lower():
            found_names.clear()  # reset any previously found names for safety
            for a_tag in soup.select('a[href*="/roster/"]'):
                # The 'title' attribute contains the full player name
                name = a_tag.get("title", "").strip()
                # Skip links without a name or that are obviously non-players
                if not name or any(kw in name.lower() for kw in ["bio", "staff", "coach", "view"]):
                    continue
                found_names.add(clean_roster_name(name))


        # --- Baylor logic ---
        if is_baylor:
            return scrape_baylor_players(url)
        
        # Stetson-specific scraping
        if is_stetson:
            for li in soup.select("li.sidearm-roster-player"):
                name_tag = li.select_one("h3 a")
                if name_tag:
                    raw_name = name_tag.get_text(" ", strip=True)
                    name = clean_roster_name(raw_name)
                    if name and not contains_invalid_word(name, ["news", "schedule", "staff", "coach", "video"]):
                        found_names.add(name)

            primary_names = {normalize(name): name for name in found_names}
            nickname_names = {}
            return primary_names, nickname_names


        # --- Common selectors for other schools ---
        common_player_selectors = [
            ".s-text-regular-bold",
            ".roster-list-item__title",
            ".player-name",
            "td.sidearm-table-player-name",
            ".roster-list__item-name",
            "a.table__roster-name",
            "td.sidearm-roster-table-data a[title]",
            "td > a[href*='/roster/season/']",
            "a.table__roster-name span",
            'div[data-test-id="s-person-details__personal-single-line"] h3',
            'a[href*="/player/"]',
            # Added robust Sidearm Sports selectors for players (common for USD)
            'li.sidearm-roster-player-name a',
            'div.sidearm-roster-list-item-name a',
            'th.name a[href*="/bios/"]',
            'div.flex-grow-1 a[aria-label]',
            'a[aria-label*="full bio"]',
            'a[aria-label*="View Full Bio"]',
            'a[href*="/roster/"][aria-label]',
            'div[class*="sidearm-roster-list-item-name"] a',
            'a[href*="/roster/"][href^="https://arkansasrazorbacks.com/"]',
            'a[href*="/roster/"]',
            'div.sidearm-roster-list-item-name.sidearm-roster-player-name a'
        ]

        # --- Step 1: scrape common selectors ---
        for element in soup.select(", ".join(common_player_selectors)):
            name = clean_roster_name(element.get_text(" ", strip=True))

            if contains_invalid_word(name, invalid_keywords):
                continue
            if name and not contains_invalid_word(name, invalid_keywords):
                found_names.add(name)


        # --- Step 2: ASU-specific fix: scrape alt from player images ---
        for img_tag in soup.select('a.roster-card__image-wrapper img'):
            name = img_tag.get('alt', '').strip()
            if name:
                found_names.add(name)

        # --- Step 3: build primary and nickname dictionaries ---
        primary_names = {}
        nickname_names = {}
        for name in found_names:
            # Look for nicknames in quotes
            match = re.search(r'(\S+)\s+["“”‘’](.+?)["“”‘’]\s+(.+)', name)
            if match:
                first_name, nickname, last_name = match.groups()
                primary_names[normalize(f"{first_name} {last_name}")] = name
                nickname_names[normalize(f"{nickname} {last_name}")] = name
            else:
                primary_names[normalize(name)] = name

        return primary_names, nickname_names

    except Exception as e:
        # Improved error logging for debugging
        st.error(f"Error scraping player names from URL: {e}")
        return {}, {}


def scrape_staff_names(url: str):
    """
    Scrape staff names and titles from a roster page.
    Returns a dictionary: {normalized_name: {"name": original_name, "title": title}}
    """
    staff_dict = {}
    if "baylorbears.com" in url.lower():
        return scrape_baylor_staff(url)
    
    try:
        # **FIX 1 & 2:** Use common headers and increased timeout
        resp = requests.get(url, timeout=30, headers=COMMON_HEADERS)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        if "thesundevils.com" in url.lower():
            for a_tag in soup.select('h3.roster-card__title a.roster-card__title-link'):
                href = a_tag.get('href', '')
                if '/staff/' in href.lower():
                    name = a_tag.get_text(strip=True)
                    # Get title if available
                    parent_div = a_tag.find_parent('div', class_='roster-card__heading')
                    title = "Staff"
                    if parent_div:
                        info_div = parent_div.find_next_sibling('div', class_='roster-card__info')
                        if info_div:
                            span = info_div.select_one('span.roster-card__position')
                            if span:
                                title = span.get_text(strip=True)
                    staff_dict[normalize(name)] = {"name": name, "title": title}

        # Selectors for various staff list formats
        staff_items = soup.select('li.sidearm-roster-coach, .roster-list-item.staff, tr[data-v-7436a2c8]')
        
        # **FIX 3:** Added robust Sidearm Sports staff selector (for USD/generic sidearm sites)
        sidearm_staff_items = soup.select('li.sidearm-roster-staff-item')

        # New selector for h3 name format
        h3_staff_names = soup.select('a[href*="/roster/staff/"] h3')

        # Added selector for Clemson's staff table format
        staff_rows = soup.select('tr.person__item')

        # Added selector for Georgia Tech's staff table format
        gt_staff_rows = soup.select('tr:has(td > a[href*="/coaches/"])')

        # New selector for Stanford's staff format
        stanford_staff_links = soup.select('a.table__roster-name[href*="/staff/"]')
        
        # New selector for Virginia Tech staff (using the same class as players, but filtering by URL)
        vt_staff_links = soup.select('a.roster-list-item__title[href*="/staff/"]')

        # New selector for Virginia coaches
        uva_coach_links = soup.select('a[href*="/coach/"]')

        # Process standard staff list formats
        for item in staff_items:
            # Check for the UNC-specific format within the table row
            if 'tr' in item.name and item.has_attr('data-v-7436a2c8'):
                name_tag = item.select_one('td:first-of-type .s-text-regular-bold')
                title_tag = item.select_one('td:last-of-type span')
            else:
                name_tag = item.select_one('.sidearm-roster-coach-name p, .roster-list-item__title')
                title_tag = item.select_one('.sidearm-roster-coach-title span, .roster-list-item__profile-field--position')

            if not name_tag:
                continue

            name = name_tag.get_text(" ", strip=True)
            title = title_tag.get_text(" ", strip=True) if title_tag else "Staff"

            if "bio" in name.lower() or "view" in name.lower():
                continue

            staff_dict[normalize(name)] = {"name": name, "title": title}

        # Process the newly added Sidearm Sports staff selector
        for item in sidearm_staff_items:
            # Attempt to find name in common Sidearm staff elements
            name_tag = item.select_one('.sidearm-roster-staff-name p, .sidearm-roster-staff-name')
            title_tag = item.select_one('.sidearm-roster-staff-title span, .sidearm-roster-staff-title')
            
            if not name_tag:
                continue
            
            name = name_tag.get_text(" ", strip=True)
            title = title_tag.get_text(" ", strip=True) if title_tag else "Staff"
            
            staff_dict[normalize(name)] = {"name": name, "title": title}
        
        # Process new h3 name format
        for name_h3 in h3_staff_names:
            name = name_h3.get_text(" ", strip=True)
            staff_dict[normalize(name)] = {"name": name, "title": "Staff"}

        # Process the new Clemson staff table format
        for row in staff_rows:
            name_tag = row.select_one('td:first-of-type a')
            title_tag = row.select_one('td:nth-of-type(2)')
            if name_tag and title_tag:
                name = name_tag.get_text(" ", strip=True)
                title = title_tag.get_text(" ", strip=True)
                staff_dict[normalize(name)] = {"name": name, "title": title}
        
        # Process the new Georgia Tech staff table format
        for row in gt_staff_rows:
            name_tag = row.select_one('td > a[href*="/coaches/"]')
            title_tag = name_tag.parent.find_next_sibling('td')
            if name_tag and title_tag:
                name = name_tag.get_text(" ", strip=True)
                title = title_tag.get_text(" ", strip=True)
                staff_dict[normalize(name)] = {"name": name, "title": title}

        # Process the new Stanford staff format
        for link in stanford_staff_links:
            name_span = link.select_one('span')
            if name_span:
                name = name_span.get_text(" ", strip=True)
                staff_dict[normalize(name)] = {"name": name, "title": "Staff"}

        # New selector for Syracuse staff
        syracuse_staff_links = soup.select('div[data-test-id="s-person-details__personal-single-line"] a[href*="/roster/staff/"]')
        for link in syracuse_staff_links:
            name_tag = link.select_one('h3')
            if name_tag:
                name = name_tag.get_text(" ", strip=True)
                staff_dict[normalize(name)] = {"name": name, "title": "Staff"}
        
        # Process new Virginia Tech staff format
        for link in vt_staff_links:
            name = link.get_text(" ", strip=True)
            staff_dict[normalize(name)] = {"name": name, "title": "Staff"}

        # New selector for Virginia coaches
        for link in uva_coach_links:
            name = link.get_text(" ", strip=True)
            staff_dict[normalize(name)] = {"name": name, "title": "Coach"}

        # Additional check for UNC format where coaches are listed in a separate table
        coach_names = soup.select('a[href*="/coaches/"] span.s-text-regular-bold')
        for name_span in coach_names:
            name = name_span.get_text(" ", strip=True)
            staff_dict[normalize(name)] = {"name": name, "title": "Coach"}

        return staff_dict

    except Exception as e:
        # Improved error logging for debugging
        st.error(f"Error scraping staff names: {e}")
        return {}


def generate_expected_filenames(player_keys, school_prefix):
    """
    Only generate expected filenames for players (ignore staff).
    """
    expected_files = []
    for normalized_name, original_name in player_keys.items():
        parts = original_name.split(" ")
        if len(parts) >= 2:
            first = parts[0].lower()
            last = parts[-1].lower()
            expected_filename = f"{school_prefix}_{last}_{first}.png"
            expected_files.append(expected_filename)
    return expected_files


def find_missing_players(parsed_files, player_keys, staff_dict, school_prefix):
    existing_player_files = set()
    for entry in parsed_files:
        if not entry.get("format_valid", False):
            continue
        normalized_name = normalize(f"{entry.get('first','')} {entry.get('last','')}")
        if normalized_name not in staff_dict:
            existing_player_files.add(normalized_name)

    missing_players = []

    for normalized_name, roster_name in player_keys.items():
        if normalized_name in staff_dict:
            continue

        if roster_name.lower() in ["full bio", "view full bio"]:
            continue

        if normalized_name not in existing_player_files:
            
            # --- START CORRECTED NAME SPLIT LOGIC ---
            
            # Use the normalized_name (e.g., "kalvyn crummie") for splitting
            parts = [p.strip() for p in normalized_name.split() if p.strip()]
            
            first = ""
            last = ""
            
            if len(parts) >= 2:
                # Two or more names: Last is the final word, First is everything before it.
                # Example: ['kalvyn', 'crummie'] -> first='kalvyn', last='crummie'
                # Example: ['d'arious', 't', 'reed'] -> first='d'arious t', last='reed'
                last_raw = parts[-1]
                first_raw = ' '.join(parts[:-1])
                
            elif len(parts) == 1:
                # Single-name case: Use it as the 'first' name.
                # Example: ['tymoss'] -> first='tymoss', last=''
                first_raw = parts[0]
                last_raw = ""
            else:
                continue # Skip empty names

            # Clean up parts for the FILENAME (remove ' and - from the suggested filename components)
            first = re.sub(r"['\-]", "", first_raw)
            last = re.sub(r"['\-]", "", last_raw)
            
            # --- END CORRECTED NAME SPLIT LOGIC ---

            missing_players.append({
                "filename": None,
                "first": first, 
                "last": last,
                "status": "⚠️ Missing"
            })

    return missing_players

# --- Google Drive folder helpers (no API) ---

def _extract_drive_folder_id(url: str) -> str | None:
    """
    Extracts the folder ID from a Google Drive folder URL.
    """
    try:
        parsed = urlparse(url)
        # path-based
        m = re.search(r"/folders/([a-zA-Z0-9_-]+)", parsed.path)
        if m:
            return m.group(1)
        # query-based (?id=)
        qs = parse_qs(parsed.query or "")
        if "id" in qs and len(qs["id"]) > 0:
            return qs["id"][0]
    except:
        pass
    return None

def get_drive_folder_png_filenames(folder_url: str) -> list[str]:
    """
    Fetch PNG filenames from a PUBLIC Google Drive folder without Google API.
    Uses the 'embeddedfolderview' endpoint, which returns parseable HTML.
    """
    folder_id = _extract_drive_folder_id(folder_url)
    if not folder_id:
        st.warning("Could not recognize a Google Drive folder ID from the URL.")
        return []

    # Try embedded folder view first (most reliable for scraping)
    candidates = [
        f"https://drive.google.com/embeddedfolderview?id={folder_id}#list",
        f"https://drive.google.com/embeddedfolderview?id={folder_id}#grid",
        # Fallbacks (may not contain names, but try anyway)
        f"https://drive.google.com/drive/folders/{folder_id}",
        f"https://drive.google.com/drive/u/0/folders/{folder_id}",
    ]

    for url in candidates:
        try:
            # **FIX:** Use headers here too, just in case Drive is sensitive
            r = requests.get(url, timeout=30, headers=COMMON_HEADERS)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")

            # Strategy: collect all visible text nodes and filter for *.png
            texts = [t.strip() for t in soup.stripped_strings if t.strip()]
            pngs = [t for t in texts if t.lower().endswith(".png")]

            # Deduplicate while preserving order
            seen = set()
            out = []
            for x in pngs:
                if x not in seen:
                    seen.add(x)
                    out.append(x)

            if out:
                return out
        except Exception:
            continue

    # If nothing found, give a hint
    st.info(
        "No .png names found. Make sure the folder is set to 'Anyone with the link' (Viewer). "
        "Then reload and try again."
    )
    return []

# --- Comparison logic (Unchanged) ---

def check_mismatches_and_missing(parsed_files, player_keys, nickname_keys, staff_dict, school_prefix):
    data = []
    # Inside check_mismatches_and_missing
    parsed_files = [f for f in parsed_files if f.get("format_valid", False)]

    # --- Step 1: existing files ---
    for entry in parsed_files:
        filename = entry.get("filename")
        fmt_ok = entry.get("format_valid", False)
        raw_last = entry.get("last") or ""
        raw_first = entry.get("first") or ""
        suggestion = None
        roster_name = None

        if not fmt_ok:
            status = entry.get("format_msg", "Invalid filename format")
        else:
            school = entry["school"]
            normalized_filename_name = normalize(f"{raw_first} {raw_last}")

            # Staff first
            if normalized_filename_name in staff_dict:
                staff_info = staff_dict[normalized_filename_name]
                status = f"❌ Not a Player ({staff_info.get('title','Staff')})"

            elif school != school_prefix.lower():
                status = "❌ School prefix mismatch"

            elif normalized_filename_name in player_keys:
                status = "✅"
                roster_name = player_keys[normalized_filename_name]

            elif normalized_filename_name in nickname_keys:
                original_roster_name = nickname_keys[normalized_filename_name]
                match = re.search(r'(\S+)\s+["“”‘’]', original_roster_name)
                if match:
                    first_name = normalize(match.group(1))
                    status = "❌ Nickname used instead of First Name"
                    suggestion = f"{school_prefix}_{raw_last}_{first_name}.png"
                    roster_name = original_roster_name
                else:
                    status = "❌ Nickname used instead of First Name"
                    roster_name = original_roster_name
            else:
                status = "❌ Name not in roster"

        data.append({
            "filename": filename,
            "first": raw_first,
            "last": raw_last,
            "status": status
        })

    # --- Step 2: missing players (ignore staff) ---
    missing_players = find_missing_players(parsed_files, player_keys, staff_dict, school_prefix)
    data.extend(missing_players)

    return pd.DataFrame(data)

# --- Streamlit UI (main script) ---

st.title("School Roster Photo Name Checker (Local or Google Drive Folder)")

source = st.radio("Where are the images stored?", ["Local folder", "Google Drive folder"])

image_files: list[str] = []

if source == "Local folder":
    folder_path = st.text_input("Paste the path to your image folder here:")
    if folder_path and os.path.exists(folder_path):
        image_files = [f for f in os.listdir(folder_path) if f.lower().endswith(".png")]
        st.caption(f"Found {len(image_files)} .png files locally.")
    elif folder_path:
        st.error("Folder path does not exist.")

elif source == "Google Drive folder":
    drive_folder_url = st.text_input("Paste the PUBLIC Google Drive folder URL here:")
    if drive_folder_url:
        image_files = get_drive_folder_png_filenames(drive_folder_url)
        if image_files:
            st.success(f"Found {len(image_files)} .png files in the Drive folder.")
            with st.expander("Show detected filenames"):
                st.write(image_files)

school_prefix = st.text_input("Enter the school prefix (e.g., cal, oregon):")
school_url = st.text_input("Paste the school roster URL here:")

if st.button("Check Files"):
    if not image_files:
        st.error("No image files detected yet.")
    elif not school_prefix or not school_url:
        st.error("Please fill in both the school prefix and the roster URL.")
    else:
        parsed_files = parse_filenames(image_files)

        # --- Scrape players and staff ---
        player_keys, nickname_keys = scrape_player_names(school_url)
        staff_dict = scrape_staff_names(school_url)

        # --- Remove staff accidentally scraped as players ---
        for staff_name in list(staff_dict.keys()):
            if staff_name in player_keys:
                del player_keys[staff_name]
            if staff_name in nickname_keys:
                del nickname_keys[staff_name]

        # --- Remove non-player entries using invalid keywords ---
        invalid_keywords = [
            "coach", "staff", "jersey", "number", "manager", "director",
            "head coach", "assistant", "trainer", "operations"
        ]
        for key in list(player_keys.keys()):
            name = player_keys[key]
            parts = name.split()
            first = parts[0] if len(parts) > 0 else ""
            last = parts[-1] if len(parts) > 1 else ""

            if any(re.search(rf"\b{re.escape(word)}\b", first.lower()) for word in invalid_keywords) or \
            any(re.search(rf"\b{re.escape(word)}\b", last.lower()) for word in invalid_keywords):
                del player_keys[key]
                if key in nickname_keys:
                    del nickname_keys[key]

        if not player_keys:
            st.warning("No players detected from the roster page.")
        else:
            df = check_mismatches_and_missing(
                parsed_files, player_keys, nickname_keys, staff_dict, school_prefix
            )
            st.subheader("Roster Photo Check")
            st.dataframe(df)

st.subheader("Debug: Staff Dictionary Contents")
if 'staff_dict' in locals():
    st.write(staff_dict)
