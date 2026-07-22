"""Configuration and paths for SubWatch."""
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "bin")
DATA = os.path.join(ROOT, "data")
DB_DIR = os.path.join(ROOT, "db")
NOTES_DIR = os.path.join(ROOT, "notes")
LOGS_DIR = os.path.join(ROOT, "logs")

OCR_HELPER = os.path.join(BIN, "ocr_helper")
DB_PATH = os.path.join(DB_DIR, "subwatch.db")
COMMON_WORDS = os.path.join(DATA, "common_words.txt")
CONFIG_PATH = os.path.join(DATA, "config.json")

# How hard a word must be before it is captured. A word is "hard" when its rank
# in the common-word frequency list is worse than this threshold (or absent).
DEFAULT_CONFIG = {
    # subtitle display mode: "show_both", "hide_chinese", "hide_english", "hide_both"
    "display_mode": "hide_chinese",
    # how often to OCR the screen, in seconds
    "capture_interval": 0.4,
    # a word ranked worse than this in the 10k common list is considered "hard".
    # lower = stricter (capture only rarer words). 3000 is a good intermediate level.
    "rarity_threshold": 3000,
    # minimum word length to ever consider capturing
    "min_word_length": 4,
    # OCR confidence floor (0..1) below which a line is ignored
    "min_confidence": 0.3,
    # the screen capture region as [x, y, w, h] in pixels of the chosen display
    # (origin top-left). null = whole display. Typically the bottom subtitle strip.
    "capture_region": None,
    # which display to capture (1-based, as macOS `screencapture -D` numbers them).
    # null = main display. Set this when the video plays on a secondary monitor.
    "display": None,
    # SMART capture: use Codex to judge which words/phrases are genuinely
    # hard for a learner, instead of a raw frequency cutoff (which misses words like
    # "sudsy"/"MTV"). On by default; falls back to frequency mode if Codex is down.
    "smart_capture": True,
    # Learner difficulty profile used by Codex. Intermediate + breadth 1 captures
    # useful upper-intermediate movie vocabulary without flooding the deck with basics.
    "smart_level": "intermediate",
    "capture_breadth": 1,
    # also capture idiom/slang PHRASES (e.g. "help yourself") from the curated list,
    # storing the whole sentence as the study item. Single-word capture still runs too.
    "capture_phrases": True,
    # additionally use Codex to flag hard expressions the fixed list
    # can't anticipate (e.g. "devoid of humor"). Off by default — costs an API call
    # per new subtitle line. Turn on with `subwatch llm-phrases on`.
    "use_llm_phrases": False,
    # LOCAL-ONLY mode: never call Codex. Audio mode still uses on-device Whisper;
    # word selection falls back to the local frequency+dictionary path.
    "local_only": False,
    # Every language-model feature runs through the locally installed Codex CLI.
    "ai_provider": "codex",
    "codex_model": "gpt-5.6-terra",
    # MEETING MODE (Otter-style live Zoom transcript + AI notes + Q&A chat). Captures
    # Zoom's live caption window (primary: Accessibility API; fallback: OCR; last
    # resort: audio→Whisper), assembles a clean timestamped transcript, and runs notes/
    # chat/summary on Codex.
    "meeting": {
        "source": "auto",            # auto | ax | ocr | audio
        "poll_hz": 3,                # caption poll rate
        "stable_frames": 4,          # frames of no-change before a line finalizes
        "grow_similarity": 0.85,     # match threshold for "same line, grown/jittered"
        "dup_similarity": 0.92,      # cross-utterance duplicate threshold
        "dup_window_seconds": 90.0,  # how far back to dedup against (Zoom's panel dwells ~60s+)
        "speaker_reset_seconds": 8.0,
        "notes_interval_seconds": 45,  # how often to refresh running notes (debounced)
        "notes_model": "gpt-5.6-terra",
        "effort": "high",           # Codex reasoning effort for the summary
        "chat_effort": "medium",    # lower effort for live chat so replies are fast (~3-5s)
        "caption_region": None,      # manual OCR-fallback region [x,y,w,h] if AX fails
        "capture_shared_screen": True,  # OCR shared-screen content as summary context
        "auto_capture": True,        # meeting_daemon auto-starts capture when Zoom captions
                                     # open (no need to open the portal). Idle = ~few MB.
        # Personalization: bias notes/summary toward YOUR action items, and respect
        # correct names/pronouns. Set `name` to how you appear in Zoom captions.
        "me": {
            "name": "Canjian Chen",
            # include common OCR-garbled spellings of the name so the AI knows these all
            # refer to the user (captions frequently mangle 'Canjian').
            "aliases": ["Canjian", "Ken", "Kenjian", "canjianc", "Kanjan", "Kanjin",
                        "Kenjan", "Kenjin", "Canjan", "Kangjian"],
            "pronouns": "he/him",
            "role": "Software Dev Engineer",
        },
        # Optional roster of known people → pronouns, so the summary uses them correctly
        # (the AiMS 'pronoun correction' request). e.g. {"Alex Kim": "they/them"}.
        "pronouns": {},
    },
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
                saved = json.load(handle)
            for key, value in saved.items():
                # DEEP-merge dict-valued sections (e.g. "meeting") so a saved block that
                # predates a newly-added default key doesn't silently drop it. Without
                # this, an old saved "meeting" block would shadow new defaults like
                # me / effort / capture_shared_screen.
                if isinstance(value, dict) and isinstance(cfg.get(key), dict):
                    merged = dict(cfg[key])
                    merged.update(value)
                    cfg[key] = merged
                else:
                    cfg[key] = value
        except (json.JSONDecodeError, OSError):
            pass
    return cfg


def save_config(cfg):
    os.makedirs(DATA, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as handle:
        json.dump(cfg, handle, indent=2, ensure_ascii=False)


def codex_available(refresh=False):
    """Return whether the local Codex CLI is installed and authenticated."""
    try:
        import codex_ai
        return codex_ai.available(refresh=refresh)
    except Exception:  # noqa: BLE001
        return False


def effective_local_only():
    """True when AI is disabled explicitly or Codex is not authenticated."""
    cfg = load_config()
    return bool(cfg.get("local_only")) or not codex_available()


def _user_set_keys():
    """Keys the user has explicitly written to config.json (vs. defaults)."""
    if not os.path.exists(CONFIG_PATH):
        return set()
    try:
        with open(CONFIG_PATH, encoding="utf-8") as handle:
            return set(json.load(handle).keys())
    except (json.JSONDecodeError, OSError):
        return set()
