import os
import sqlite3
import base64
import uuid
import json
import mimetypes
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

import requests
from flask import (
    Flask, render_template, request, redirect, url_for,
    abort, jsonify, flash, g, session
)
from werkzeug.security import generate_password_hash, check_password_hash

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

try:
    import httpx
except ImportError:
    httpx = None


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "revision.db"
STATIC_DIR = BASE_DIR / "static"
IMAGES_DIR = STATIC_DIR / "images"
AUDIO_DIR = STATIC_DIR / "audio"
IMAGES_DIR.mkdir(parents=True, exist_ok=True)
AUDIO_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_IMAGE_EXT = {"png", "jpg", "jpeg", "webp", "gif"}
MAX_UPLOAD_MB = 8

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "").strip()
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM").strip()
# Each music API call is capped at 22s; we concatenate N segments for a longer track.
MUSIC_SEGMENTS = max(1, min(5, int(os.environ.get("MUSIC_SEGMENTS", "2"))))

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE NOT NULL,
    email         TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_at    TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS artworks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    title         TEXT NOT NULL,
    artist        TEXT NOT NULL,
    image_file    TEXT NOT NULL,
    audio_file    TEXT,
    music_file    TEXT,
    music_prompt  TEXT,
    alt_text      TEXT,
    description   TEXT,
    sonification  TEXT,
    mood          TEXT,
    palette       TEXT,
    music_params  TEXT,
    listen_count  INTEGER DEFAULT 0,
    favorite      INTEGER DEFAULT 0,
    created_by    INTEGER,
    created_at    TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (created_by) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS user_favorites (
    user_id     INTEGER NOT NULL,
    artwork_id  INTEGER NOT NULL,
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, artwork_id),
    FOREIGN KEY (user_id)    REFERENCES users(id),
    FOREIGN KEY (artwork_id) REFERENCES artworks(id)
);

CREATE TABLE IF NOT EXISTS plays (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    artwork_id  INTEGER NOT NULL,
    user_id     INTEGER,
    played_at   TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (artwork_id) REFERENCES artworks(id),
    FOREIGN KEY (user_id)    REFERENCES users(id)
);
"""

# Seeds. Images already present locally don't need an image_url; the rest are
# downloaded once from Wikimedia Commons on first init. `pregenerate_music`
# controls whether the music API runs at seed time (vs. on first user view).
SEED = [
    {
        "title": "The Starry Night",
        "artist": "Vincent van Gogh",
        "image_file": "images/img1.jpeg",
        "alt_text": "A swirling night sky over a quiet village, dominated by deep blues and glowing yellow stars.",
        "description": "A turbulent indigo sky whirls in great spirals above a hushed village. Yellow stars throb like distant bells, while a dark cypress flares upward like a flame.",
        "sonification": "Sustained low strings hold the night, sweeping violin arpeggios trace the spiral winds, and a soft bell rings for each glowing star.",
        "mood": "Turbulent, dreamlike, hopeful",
        "palette": "Cobalt, midnight blue, ochre, lemon",
        "music_params": {
            "tempo": 68, "key": "D minor",
            "mood_in_sound": "Slow, swelling, with arcing arpeggios and gentle bell-like highs.",
            "instruments": ["warm pad", "soft piano", "low cello"],
            "chord_progression": ["Dm", "Bb", "F", "Am"],
            "bass_notes": ["D2", "Bb1", "F2", "A1"],
            "melody_notes": ["D5","F5","A5","D6","A5","F5","E5","D5","C5","E5","G5","A5","F5","D5","C5","A4"],
            "color_to_sound": "Cobalt and midnight blue translate to held cello drones; lemon stars become bright piano sparks.",
            "shape_to_rhythm": "Spiral winds are arcing eighth-note arpeggios; the steady village is a slow whole-note pad."
        },
        "music_prompt": "Slow cinematic ambient piece, 68 BPM in D minor. Warm pad, soft piano, and low cello over a Dm to B-flat to F to A-minor chord progression. Sweeping arcing arpeggios with gentle bell-like piano accents. Dreamlike, turbulent yet hopeful, evoking a swirling starry night sky and a quiet village.",
        "pregenerate_music": True,
    },
    {
        "title": "The Persistence of Memory",
        "artist": "Salvador Dalí",
        "image_file": "images/starry_night.webp",
        "alt_text": "Melting pocket-watches drape a barren shore beneath glowing cliffs.",
        "description": "Soft clocks melt across an empty beach as cliffs glow in fading light. Time itself stretches like warm wax. A single insect and a strange face remind us this is a dream.",
        "sonification": "Stretched pads with slow detuning, bending clock-tick percussion, and one lingering piano note suspended in space.",
        "mood": "Surreal, suspended, melancholic",
        "palette": "Sienna, dusty gold, ivory, shadow blue",
        "music_params": {
            "tempo": 54, "key": "F# minor",
            "mood_in_sound": "Suspended, dreamlike, with bent pitches and long fading notes.",
            "instruments": ["dreamy pad", "felted piano", "soft bass"],
            "chord_progression": ["F#m", "D", "A", "E"],
            "bass_notes": ["F#1", "D2", "A1", "E2"],
            "melody_notes": ["A4","C#5","F#5","A5","F#5","E5","C#5","B4","A4","C#5","E5","F#5","E5","D5","C#5","B4"],
            "color_to_sound": "Sienna sand becomes a warm low pad; the gold cliffs glow as held piano notes.",
            "shape_to_rhythm": "Melting watches stretch each note long; the bare horizon keeps a slow, steady pulse."
        },
        "music_prompt": "Slow surreal ambient piece, 54 BPM in F-sharp minor. Dreamy pad, felted piano, and soft sub bass with stretched bent notes and long fading tails. Hypnotic chord progression F#m to D to A to E. Suspended, melancholic, dreamlike, evoking melting clocks on a barren shore at dusk.",
        "pregenerate_music": True,
    },
    {
        "title": "The Scream",
        "artist": "Edvard Munch",
        "image_url": "https://upload.wikimedia.org/wikipedia/commons/f/f4/The_Scream.jpg",
        "image_file": "images/seed_scream.jpg",
        "alt_text": "A skeletal figure with hands on its face screams on a bridge under a blood-red sky.",
        "description": "A pale, skeletal figure clasps its face in horror on a wooden bridge. The sky writhes with streaks of blood-red and burnt orange while a black fjord undulates below. Two indifferent figures walk away in the distance.",
        "sonification": "Detuned strings rise and fall in nauseous waves, a single dissonant cluster pulses at the heart, and a soft bowed cymbal hisses like distant wind.",
        "mood": "Anxious, distorted, haunting",
        "palette": "Blood orange, burnt sienna, indigo, ash",
        "music_params": {
            "tempo": 80, "key": "A minor",
            "mood_in_sound": "Dissonant and unsteady, with rising tremolo strings and a nervous pulse.",
            "instruments": ["tremolo strings", "low piano", "bowed cymbal"],
            "chord_progression": ["Am", "F", "Dm7", "E7"],
            "bass_notes": ["A1", "F1", "D2", "E2"],
            "melody_notes": ["A4","C5","E5","F5","E5","D5","C5","B4","A4","B4","C5","D5","E5","D5","C5","B4"],
            "color_to_sound": "Blood-red sky becomes rising tremolo strings; the dark fjord is a low piano drone.",
            "shape_to_rhythm": "The screaming face is a single throbbing dissonant cluster; the bridge keeps a nervous quarter-note pulse."
        },
        "music_prompt": "Anxious cinematic piece, 80 BPM in A minor. Tremolo strings rising and falling, low piano drones, and a bowed cymbal hiss. Dissonant chord progression Am to F to Dm7 to E7 with a nervous quarter-note pulse. Haunting, distorted, and uneasy, evoking a screaming figure under a blood-red sky.",
        "pregenerate_music": False,
    },
    {
        "title": "The Great Wave off Kanagawa",
        "artist": "Katsushika Hokusai",
        "image_url": "https://upload.wikimedia.org/wikipedia/commons/0/0a/The_Great_Wave_off_Kanagawa.jpg",
        "image_file": "images/seed_wave.jpg",
        "alt_text": "A towering blue wave with white claw-like foam curls over three boats, with Mount Fuji small in the distance.",
        "description": "A colossal cresting wave dominates the scene, its foam splayed like grasping fingers above three slim boats whose rowers crouch in resignation. Mount Fuji sits quietly in the background, dwarfed by the sea's fury.",
        "sonification": "Taiko drums build with the rising wave, koto plucks scatter as the foam breaks, and a low shakuhachi flute holds the steady horizon of Mount Fuji.",
        "mood": "Powerful, kinetic, awe-inspiring",
        "palette": "Prussian blue, ivory foam, sand, slate",
        "music_params": {
            "tempo": 96, "key": "E minor",
            "mood_in_sound": "Building tension with rolling percussion and bright koto cascades.",
            "instruments": ["taiko drums", "plucked koto", "shakuhachi flute"],
            "chord_progression": ["Em", "C", "G", "D"],
            "bass_notes": ["E1", "C2", "G2", "D2"],
            "melody_notes": ["E5","G5","B5","E6","B5","G5","A5","B5","D6","B5","A5","G5","E5","D5","G5","E5"],
            "color_to_sound": "Prussian blue becomes a deep taiko pulse; the foaming whites are bright koto cascades.",
            "shape_to_rhythm": "The cresting wave is a swelling crescendo; Mount Fuji holds a single sustained low tone."
        },
        "music_prompt": "Cinematic Japanese-inflected piece, 96 BPM in E minor. Taiko drums building with rolling thunder, plucked koto cascading like foam, and a shakuhachi flute holding a serene low tone. Chord progression Em to C to G to D. Powerful, kinetic, awe-inspiring, evoking a colossal wave curling over small boats with Mount Fuji in the distance.",
        "pregenerate_music": False,
    },
    {
        "title": "Composition VIII",
        "artist": "Wassily Kandinsky",
        "image_url": "https://upload.wikimedia.org/wikipedia/commons/4/47/Vassily_Kandinsky%2C_1923_-_Composition_8%2C_huile_sur_toile%2C_140_cm_x_201_cm%2C_Mus%C3%A9e_Guggenheim%2C_New_York.jpg",
        "image_file": "images/seed_composition8.jpg",
        "alt_text": "An abstract painting of overlapping circles, triangles, and sharp lines in red, blue, yellow, and black on cream.",
        "description": "Bold circles, sharp triangles, and confident lines collide across a pale field. Concentric rings of crimson, lemon, and royal blue jostle with checkerboards and razor diagonals — a visual jazz of pure geometry.",
        "sonification": "Staccato piano stabs catch each triangle, bright marimba chimes ring out the circles, and a brushed snare keeps a syncopated jazz pulse.",
        "mood": "Energetic, playful, geometric",
        "palette": "Crimson, lemon, royal blue, soft cream",
        "music_params": {
            "tempo": 116, "key": "C major",
            "mood_in_sound": "Bright, angular, and rhythmic with staccato stabs and chiming marimbas.",
            "instruments": ["staccato piano", "marimba", "brushed snare"],
            "chord_progression": ["C", "F", "G", "Am"],
            "bass_notes": ["C2", "F2", "G2", "A2"],
            "melody_notes": ["C5","E5","G5","C6","G5","E5","C5","D5","F5","A5","C6","A5","G5","F5","D5","C5"],
            "color_to_sound": "Crimson becomes a sharp piano stab; lemon yellow shines as a high marimba chime.",
            "shape_to_rhythm": "Circles ring out as bell-like sustains; triangles are syncopated quarter-note hits."
        },
        "music_prompt": "Bright modern jazz-tinged piece, 116 BPM in C major. Staccato piano stabs, chiming marimba bursts, and a brushed snare keeping a syncopated pulse. Major chord progression C to F to G to Am. Energetic, playful, geometric, evoking overlapping circles, triangles, and sharp diagonals in primary colors.",
        "pregenerate_music": False,
    },
    {
        "title": "Water-Lily Pond",
        "artist": "Claude Monet",
        "image_url": "https://upload.wikimedia.org/wikipedia/commons/1/10/Claude_Monet%2C_Water-Lily_Pond_and_Weeping_Willow.JPG",
        "image_file": "images/seed_waterlilies.jpg",
        "alt_text": "A serene pond covered with floating lily pads in soft greens, lavenders, and rose pinks.",
        "description": "A still pond reflects sky and willows in soft strokes of lavender and pale rose. Floating lily pads drift like calm thoughts; the water dissolves the boundary between earth and air. Light is everywhere and nowhere.",
        "sonification": "A felt-hammer piano traces slow arpeggios over a warm string pad, while soft mallets ring out single bell tones for each blossom on the water.",
        "mood": "Serene, contemplative, luminous",
        "palette": "Lavender, sage, rose, pale gold",
        "music_params": {
            "tempo": 60, "key": "F major",
            "mood_in_sound": "Spacious and luminous, with slow piano arpeggios over a glowing pad.",
            "instruments": ["felt piano", "warm string pad", "soft mallets"],
            "chord_progression": ["Fmaj7", "Bb", "Gm7", "C"],
            "bass_notes": ["F2", "Bb1", "G2", "C2"],
            "melody_notes": ["F5","A5","C6","F6","C6","A5","G5","F5","E5","G5","Bb5","D6","C6","A5","G5","F5"],
            "color_to_sound": "Lavender becomes a high felt-piano shimmer; sage greens hum as the warm pad.",
            "shape_to_rhythm": "Lily pads are gentle bell tones spread across the bar; the still water is one long sustained chord."
        },
        "music_prompt": "Slow impressionistic ambient piece, 60 BPM in F major. Felt-hammer piano arpeggios over a warm string pad, with soft mallets ringing single bell tones. Lush chord progression Fmaj7 to Bb to Gm7 to C. Serene, contemplative, luminous, evoking a still pond covered in floating lily pads at dusk.",
        "pregenerate_music": False,
    },
    {
        "title": "Girl with a Pearl Earring",
        "artist": "Johannes Vermeer",
        "image_url": "https://upload.wikimedia.org/wikipedia/commons/0/0f/1665_Girl_with_a_Pearl_Earring.jpg",
        "image_file": "images/seed_pearl.jpg",
        "alt_text": "A young woman in a blue and yellow turban glances over her shoulder, a single pearl earring catching the light.",
        "description": "A young woman turns her head over her shoulder, lips slightly parted as if about to speak. Her blue and gold turban glows softly against a deep black void, and a single pearl earring catches a single sphere of light.",
        "sonification": "A solo harpsichord plays an ornamental baroque figure over a soft viola da gamba, with a single muted bell for the gleam of the pearl.",
        "mood": "Intimate, baroque, hushed",
        "palette": "Ultramarine, lemon, ivory, deep black",
        "music_params": {
            "tempo": 72, "key": "D minor",
            "mood_in_sound": "Intimate baroque chamber music with ornamented melodies.",
            "instruments": ["harpsichord", "viola da gamba", "soft bell"],
            "chord_progression": ["Dm", "Gm", "A7", "Dm"],
            "bass_notes": ["D2", "G2", "A2", "D2"],
            "melody_notes": ["D5","F5","A5","D6","C6","Bb5","A5","G5","F5","E5","D5","C5","D5","E5","F5","A5"],
            "color_to_sound": "Ultramarine becomes the rich viola da gamba; the pearl is a single bell-like high note.",
            "shape_to_rhythm": "Her turning gesture is a slow ornamental run; the dark background is one sustained note."
        },
        "music_prompt": "Intimate baroque chamber piece, 72 BPM in D minor. Harpsichord with ornamental Baroque figures over a soft viola da gamba, with a single muted bell for highlight. Chord progression Dm to Gm to A7 to Dm. Hushed, candlelit, and deeply intimate, evoking a young woman glancing over her shoulder by candlelight.",
        "pregenerate_music": False,
    },
    {
        "title": "American Gothic",
        "artist": "Grant Wood",
        "image_url": "https://upload.wikimedia.org/wikipedia/commons/c/cc/Grant_Wood_-_American_Gothic_-_Google_Art_Project.jpg",
        "image_file": "images/seed_gothic.jpg",
        "alt_text": "A stern farmer with a pitchfork stands beside a woman in front of a small white wooden house.",
        "description": "A stern farmer grips a pitchfork while a woman in a colonial-print apron stands at his side, both facing forward with quiet resolve. Behind them, a modest white wooden house with a single gothic-arched window seems to mirror their straight backs.",
        "sonification": "An acoustic guitar fingerpicks a simple folk pattern, a fiddle holds a long low drone, and a harmonica breathes in and out like prairie wind.",
        "mood": "Stoic, austere, plainspoken",
        "palette": "Bone white, slate, ochre, cornflower",
        "music_params": {
            "tempo": 84, "key": "E minor",
            "mood_in_sound": "Plain and resolute, with fingerpicked folk patterns and a sustained drone.",
            "instruments": ["acoustic guitar", "fiddle", "harmonica"],
            "chord_progression": ["Em", "Am", "B7", "Em"],
            "bass_notes": ["E2", "A2", "B1", "E2"],
            "melody_notes": ["E4","G4","B4","E5","D5","B4","A4","G4","E4","F#4","G4","A4","B4","A4","G4","E4"],
            "color_to_sound": "Bone-white siding becomes the steady fingerpicked guitar; the slate sky is a low fiddle drone.",
            "shape_to_rhythm": "The vertical pitchfork is a single repeated low note; the gothic window arch is a slow ornament on the fiddle."
        },
        "music_prompt": "Plainspoken American folk piece, 84 BPM in E minor. Fingerpicked acoustic guitar, a sustained fiddle drone, and a harmonica breathing slowly. Simple chord progression Em to Am to B7 to Em. Stoic, austere, and resolute, evoking a midwestern farmer and his daughter standing in front of a modest white wooden house.",
        "pregenerate_music": False,
    },
    {
        "title": "Sunflowers",
        "artist": "Vincent van Gogh",
        "image_url": "https://upload.wikimedia.org/wikipedia/commons/9/9d/Vincent_van_Gogh_-_Sunflowers_-_VGM_F458.jpg",
        "image_file": "images/seed_sunflowers.jpg",
        "alt_text": "A vase of bright yellow and ochre sunflowers against a warm yellow background.",
        "description": "A bouquet of fifteen sunflowers in a clay vase fills the canvas with thick, vivid yellow brushstrokes. Some blooms turn toward us in full glory; others bend with seeds heavy and dark. The whole picture seems to vibrate with warmth.",
        "sonification": "A bright marimba shimmer hovers above a warm cello pad, with soft acoustic guitar plucks marking each sunflower head.",
        "mood": "Warm, radiant, joyful",
        "palette": "Lemon yellow, ochre, sienna, olive",
        "music_params": {
            "tempo": 92, "key": "C major",
            "mood_in_sound": "Warm and luminous with steady rhythmic plucks.",
            "instruments": ["marimba", "warm cello", "soft acoustic guitar"],
            "chord_progression": ["C", "F", "Am", "G"],
            "bass_notes": ["C2", "F2", "A1", "G2"],
            "melody_notes": ["C5","E5","G5","C6","B5","G5","E5","C5","D5","F5","A5","C6","B5","A5","G5","E5"],
            "color_to_sound": "Yellow petals shimmer as bright marimba notes; the deep seed centers are warm cello low notes.",
            "shape_to_rhythm": "Each round flower head is a single ringing chord; the bunched stems are a steady plucked rhythm."
        },
        "music_prompt": "Warm pastoral piece, 92 BPM in C major. Bright marimba shimmer over a warm cello pad and softly plucked acoustic guitar. Chord progression C to F to Am to G. Joyful, radiant, full of summer warmth, evoking a vase of yellow sunflowers in bloom.",
        "pregenerate_music": False,
    },
    {
        "title": "The Kiss",
        "artist": "Gustav Klimt",
        "image_url": "https://upload.wikimedia.org/wikipedia/commons/4/40/The_Kiss_-_Gustav_Klimt_-_Google_Cultural_Institute.jpg",
        "image_file": "images/seed_kiss.jpg",
        "alt_text": "Two lovers entwined in robes of glittering gold, the man cradling the woman's face as they kiss.",
        "description": "Two lovers wrap into one shimmering shape against an infinite gold ground. His dark rectangles meet her circles and flowers; her face turns up to receive his kiss. The whole world dissolves into pattern and gold.",
        "sonification": "A celesta tinkles in slow waves while a bowed cello holds a long warm note, and a soft choral pad swells beneath like inhaling breath.",
        "mood": "Tender, golden, ornate",
        "palette": "Gold leaf, ivory, jade, deep umber",
        "music_params": {
            "tempo": 64, "key": "G major",
            "mood_in_sound": "Slow and golden, with ornamental celesta and a glowing pad.",
            "instruments": ["celesta", "bowed cello", "choral pad"],
            "chord_progression": ["G", "Em7", "Cmaj7", "D"],
            "bass_notes": ["G1", "E2", "C2", "D2"],
            "melody_notes": ["G5","B5","D6","G6","F#6","D6","B5","G5","A5","C6","E6","G6","E6","D6","B5","G5"],
            "color_to_sound": "Gold leaf shimmers as high celesta sparkles; the dark umber robes hum as a low cello drone.",
            "shape_to_rhythm": "The kiss itself is one held suspended chord; the ornamental patterns are slow tinkling arpeggios."
        },
        "music_prompt": "Tender ornamental piece, 64 BPM in G major. Slow celesta arpeggios over a bowed cello drone and a warm choral pad. Lush chord progression G to Em7 to Cmaj7 to D. Glowing, romantic, golden, evoking two lovers entwined on a field of gold leaf.",
        "pregenerate_music": False,
    },
    {
        "title": "The Birth of Venus",
        "artist": "Sandro Botticelli",
        "image_url": "https://upload.wikimedia.org/wikipedia/commons/0/0b/Sandro_Botticelli_-_La_nascita_di_Venere_-_Google_Art_Project_-_edited.jpg",
        "image_file": "images/seed_venus.jpg",
        "alt_text": "Venus stands modestly on a giant scallop shell as winds blow her toward a shore where an attendant offers a robe.",
        "description": "A nude Venus arrives on the shore standing modestly on a vast scallop shell. To her left, two winds entwined among scattered flowers blow her gently inland; to her right, a robed attendant rushes to clothe her. The sea is calm and the air is full of falling roses.",
        "sonification": "A solo recorder traces a renaissance dance line over plucked lute strings and a soft choir, with whispered shaker for the rolling sea.",
        "mood": "Graceful, mythic, breezy",
        "palette": "Sea green, pale rose, ivory, lapis",
        "music_params": {
            "tempo": 76, "key": "F major",
            "mood_in_sound": "Light renaissance dance with floating recorder lines.",
            "instruments": ["recorder", "lute", "soft choir"],
            "chord_progression": ["F", "Dm", "Bb", "C"],
            "bass_notes": ["F2", "D2", "Bb1", "C2"],
            "melody_notes": ["F5","A5","C6","F6","E6","D6","C6","Bb5","A5","C6","D6","F6","D6","C6","Bb5","A5"],
            "color_to_sound": "The pale rose air is a high recorder line; the sea green water hums as a soft choral pad.",
            "shape_to_rhythm": "The scallop shell is a single repeating ornament; the falling roses are scattered staccato plucks."
        },
        "music_prompt": "Light renaissance dance piece, 76 BPM in F major. Solo recorder over plucked lute strings, soft choral pad, and a whispered shaker. Chord progression F to Dm to Bb to C. Graceful, mythic, breezy, evoking Venus arriving on a giant scallop shell as winds carry her to shore amid falling roses.",
        "pregenerate_music": False,
    },
]


def _download_image(url: str, dest: Path, max_dim: int = 1400) -> bool:
    """Fetch a public-domain image once, then resize to a reasonable max size."""
    if dest.exists():
        return True
    if not url:
        return False
    try:
        headers = {"User-Agent": "Revision-Gallery/1.0 (educational sonification demo)"}
        r = requests.get(url, headers=headers, timeout=30, stream=True)
        if r.status_code != 200:
            app.logger.warning("image download %s: HTTP %s", url, r.status_code)
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as fh:
            for chunk in r.iter_content(64 * 1024):
                if chunk:
                    fh.write(chunk)
    except Exception as e:
        app.logger.warning("image download %s failed: %s", url, e)
        return False

    # Resize to keep file sizes sane (some originals are 50+ MB).
    try:
        from PIL import Image
        with Image.open(dest) as im:
            im.load()
            if max(im.size) > max_dim:
                im.thumbnail((max_dim, max_dim), Image.LANCZOS)
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            im.save(dest, "JPEG", quality=85, optimize=True)
    except Exception as e:
        app.logger.info("image resize skipped for %s: %s", dest.name, e)
    return True


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)

    existing_titles = {row[0] for row in
                       conn.execute("SELECT title FROM artworks").fetchall()}

    inserted = []
    for art in SEED:
        if art["title"] in existing_titles:
            continue
        img_local = STATIC_DIR / art["image_file"]
        if not img_local.exists():
            img_url = art.get("image_url")
            if not img_url or not _download_image(img_url, img_local):
                print(f"[seed]   image missing for “{art['title']}” — skipped", flush=True)
                continue
            print(f"[seed]   downloaded {art['image_file']}", flush=True)
        c = conn.execute(
            """INSERT INTO artworks
               (title, artist, image_file, alt_text, description,
                sonification, mood, palette, music_params, music_prompt)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (art["title"], art["artist"], art["image_file"], art["alt_text"],
             art["description"], art["sonification"], art["mood"], art["palette"],
             json.dumps(art["music_params"]), art.get("music_prompt", "")),
        )
        inserted.append((c.lastrowid, art))
    conn.commit()

    if inserted and ELEVENLABS_API_KEY:
        for art_id, art in inserted:
            if not art.get("pregenerate_music") or not art.get("music_prompt"):
                continue
            filename = f"music_seed_{art_id}.mp3"
            out = AUDIO_DIR / filename
            print(f"[seed] composing music for “{art['title']}”…", flush=True)
            if generate_music_with_elevenlabs(art["music_prompt"], out, duration=22):
                conn.execute("UPDATE artworks SET music_file = ? WHERE id = ?",
                             (f"audio/{filename}", art_id))
                conn.commit()
                print(f"[seed]   saved {filename}", flush=True)
            else:
                print(f"[seed]   music generation failed", flush=True)
    conn.close()


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
ADMIN_USERNAME = "admin"
ADMIN_EMAIL = "admin@admin.com"


def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    if "current_user" not in g:
        g.current_user = get_db().execute(
            "SELECT id, username, email, created_at FROM users WHERE id = ?", (uid,)
        ).fetchone()
    return g.current_user


def is_admin(user=None) -> bool:
    u = user if user is not None else current_user()
    if not u:
        return False
    return (u["username"] or "").lower() == ADMIN_USERNAME and \
           (u["email"] or "").lower() == ADMIN_EMAIL


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user():
            if request.headers.get("X-Requested-With") == "fetch":
                return jsonify({"ok": False, "error": "login required"}), 401
            flash("Please sign in to continue.", "info")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user():
            return jsonify({"ok": False, "error": "login required"}), 401
        if not is_admin():
            return jsonify({"ok": False, "error": "admin only"}), 403
        return view(*args, **kwargs)
    return wrapped


def user_favorited(artwork_id) -> bool:
    u = current_user()
    if not u:
        return False
    row = get_db().execute(
        "SELECT 1 FROM user_favorites WHERE user_id = ? AND artwork_id = ?",
        (u["id"], artwork_id),
    ).fetchone()
    return row is not None


def annotate_favorites(rows):
    u = current_user()
    if not u:
        return [dict(r, favorited=False) for r in rows]
    fav_ids = {r["artwork_id"] for r in get_db().execute(
        "SELECT artwork_id FROM user_favorites WHERE user_id = ?", (u["id"],)
    ).fetchall()}
    return [dict(r, favorited=(r["id"] in fav_ids)) for r in rows]


# ---------------------------------------------------------------------------
# AI: vision → music
# ---------------------------------------------------------------------------
def _openai_client():
    if not OPENAI_API_KEY or OpenAI is None:
        return None
    # Pass our own httpx.Client so the OpenAI SDK doesn't try to build one
    # with the deprecated `proxies=` kwarg (broken on httpx >= 0.28).
    http_client = httpx.Client(timeout=60.0) if httpx is not None else None
    if http_client is not None:
        return OpenAI(api_key=OPENAI_API_KEY, http_client=http_client)
    return OpenAI(api_key=OPENAI_API_KEY)


FALLBACK_MUSIC = {
    "tempo": 72,
    "key": "C minor",
    "mood_in_sound": "Gentle ambient texture awaiting AI analysis.",
    "instruments": ["warm pad", "soft piano", "low cello"],
    "chord_progression": ["Cm", "Ab", "Eb", "Bb"],
    "bass_notes": ["C2", "Ab1", "Eb2", "Bb1"],
    "melody_notes": [
        "C5", "Eb5", "G5", "Bb5", "G5", "Eb5", "D5", "C5",
        "C5", "Eb5", "F5", "G5", "F5", "Eb5", "D5", "C5"
    ],
    "color_to_sound": "Placeholder mapping — set OPENAI_API_KEY for real analysis.",
    "shape_to_rhythm": "Placeholder rhythm — set OPENAI_API_KEY for real analysis."
}


def analyze_image(image_path: Path, title: str = "", artist: str = "", keywords: str = ""):
    """Return dict with sensory description, music_params, AND a rich music_prompt.

    If `keywords` is given (a comma-separated string of mood/style hints from the
    uploader), the AI is asked to bias the composition toward those keywords
    while still matching what is actually in the picture.
    """
    kw_tail = f" Mood/style keywords from the uploader: {keywords}." if keywords else ""
    fallback = {
        "alt_text": f"An artwork{(' titled ' + title) if title else ''}"
                    f"{(' by ' + artist) if artist else ''}.",
        "description": "A striking artwork awaiting AI interpretation. "
                       "Set OPENAI_API_KEY to generate a real composition.",
        "sonification": "Placeholder — set OPENAI_API_KEY to get a real interpretation.",
        "mood": (keywords or "Contemplative"),
        "palette": "Mixed tones",
        "music_params": FALLBACK_MUSIC,
        "music_prompt": "Slow ambient instrumental piece, soft piano with warm pad "
                        "and gentle low strings, contemplative and reflective." + kw_tail,
    }
    client = _openai_client()
    if client is None:
        return fallback

    mime = mimetypes.guess_type(str(image_path))[0] or "image/jpeg"
    with open(image_path, "rb") as fh:
        b64 = base64.b64encode(fh.read()).decode("ascii")
    data_url = f"data:{mime};base64,{b64}"

    system = (
        "You are a synesthetic composer. Given an artwork, analyze its colors, "
        "shapes, objects, style, and mood, then COMPOSE a short piece of music "
        "that represents it. Replies must be strict JSON. Use only standard "
        "Western notation. Never invent biographical facts about the artist."
    )
    user_text = (
        f"Title (may be empty): {title!r}. Artist (may be empty): {artist!r}.\n"
        + (f"USER MUSIC PREFERENCE (the uploader picked these keywords; bias the composition "
           f"— tempo, key, instruments, chord progression, mood, and music_prompt — toward them "
           f"while still matching what is actually in the image): {keywords!r}.\n"
           if keywords else "")
        + "\nReturn JSON with EXACTLY these keys:\n"
        "  alt_text: 1 sentence, <= 25 words, for screen readers.\n"
        "  description: 3 to 5 sensory sentences about what is in the image.\n"
        "  sonification: 1 to 2 sentences explaining how the visual translates to sound.\n"
        "  mood: 2 to 4 comma-separated adjectives.\n"
        "  palette: 3 to 5 comma-separated color names.\n"
        "  music_params: object with these EXACT keys:\n"
        "    tempo: integer between 50 and 140 (BPM).\n"
        "    key: musical key like \"C minor\" or \"F# major\".\n"
        "    mood_in_sound: 1 sentence on the musical feeling.\n"
        "    instruments: array of EXACTLY 3 short instrument names (lowercase),\n"
        "      e.g. [\"warm pad\", \"soft piano\", \"low cello\"].\n"
        "    chord_progression: array of 4 chord symbols (e.g. [\"Cm\",\"Ab\",\"Eb\",\"Bb\"]).\n"
        "      Use only these qualities: \"\", \"m\", \"7\", \"maj7\", \"m7\", \"sus2\", \"sus4\", \"dim\".\n"
        "    bass_notes: array of EXACTLY 4 note names with octave (e.g. \"C2\"). Same length as chord_progression.\n"
        "    melody_notes: array of EXACTLY 16 note names with octave (e.g. \"C5\"). Stay diatonic to the key.\n"
        "    color_to_sound: 1 to 2 sentences mapping specific colors to specific sounds.\n"
        "    shape_to_rhythm: 1 to 2 sentences mapping shapes / objects to rhythm.\n"
        "  music_prompt: 2 to 3 sentences. A rich, vivid prompt for an AI music generator\n"
        "    (ElevenLabs). Mention tempo, key, instruments, mood, and the imagery of the\n"
        "    artwork. Example: \"Slow cinematic piece, 64 BPM in D minor. Warm pad, soft\n"
        "    piano, low cello. Sweeping arpeggios evoking a swirling night sky.\"\n"
        "\nThe music must MATCH the artwork: dark/turbulent paintings → minor key, slow tempo, "
        "low instruments; bright/playful → major key, faster, brighter instruments; "
        "geometric/abstract → angular melodies; organic/flowing → smooth arpeggios."
    )

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": [
                    {"type": "text", "text": user_text},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ]},
            ],
            max_tokens=900,
            temperature=0.6,
        )
        data = json.loads(resp.choices[0].message.content)
        for k, v in fallback.items():
            data.setdefault(k, v)
        data["music_params"] = sanitize_music_params(data.get("music_params"))
        if not isinstance(data.get("music_prompt"), str) or len(data["music_prompt"]) < 20:
            data["music_prompt"] = build_music_prompt_from_params(
                data["music_params"], mood=data.get("mood", ""), description=data.get("description", "")
            )
        else:
            data["music_prompt"] = data["music_prompt"][:600]
        return data
    except Exception as e:
        app.logger.warning("OpenAI analyze failed: %s", e)
        return fallback


def build_music_prompt_from_params(mp, mood="", description=""):
    """Fallback: synthesize an ElevenLabs prompt from structured params."""
    instr = ", ".join(mp.get("instruments", []))
    chords = " to ".join(mp.get("chord_progression", []))
    bits = []
    bits.append(f"Slow instrumental piece, {mp.get('tempo', 72)} BPM in {mp.get('key', 'C minor')}.")
    if instr: bits.append(f"Played on {instr}.")
    if chords: bits.append(f"Chord progression {chords}.")
    if mood: bits.append(f"Mood: {mood}.")
    if description: bits.append(description[:160])
    return " ".join(bits)


def generate_music_with_elevenlabs(
    prompt: str, out_path: Path, duration: int = 22, segments: int = None
) -> bool:
    """Call the music API multiple times and concatenate into a longer track.

    Each API call is capped at 22 seconds. We make `segments` calls and
    concatenate the MP3 bytes — MP3 frames are independent so the join is safe
    for ambient material.
    """
    if not ELEVENLABS_API_KEY or not prompt:
        return False

    seg_count = segments if segments is not None else MUSIC_SEGMENTS
    each = max(1, min(22, int(duration)))
    url = "https://api.elevenlabs.io/v1/sound-generation"
    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "accept": "audio/mpeg",
        "content-type": "application/json",
    }

    parts = []
    for i in range(seg_count):
        # Slight phrasing variation keeps consecutive segments musically distinct.
        if i == 0 or seg_count == 1:
            phrased = prompt
        elif i == seg_count - 1:
            phrased = prompt + " Resolves and fades to silence."
        else:
            phrased = prompt + " A continuation that develops the same theme."

        body = {
            "text": phrased[:600],
            "duration_seconds": each,
            "prompt_influence": 0.6,
        }
        try:
            r = requests.post(url, headers=headers, json=body, timeout=120)
            if r.status_code != 200:
                app.logger.warning("music-gen %s: %s", r.status_code, r.text[:300])
                if not parts:
                    return False
                break
            parts.append(r.content)
        except Exception as e:
            app.logger.warning("music-gen failed on segment %d: %s", i, e)
            if not parts:
                return False
            break

    if not parts:
        return False
    out_path.write_bytes(b"".join(parts))
    return True


_NOTE_RE = __import__("re").compile(r"^[A-G][b#]?[1-7]$")
_CHORD_RE = __import__("re").compile(r"^[A-G][b#]?(m|maj7|m7|7|sus2|sus4|dim|aug)?$")


def sanitize_music_params(p):
    """Coerce/validate music params. On any issue, fall back to defaults."""
    if not isinstance(p, dict):
        return FALLBACK_MUSIC
    out = dict(FALLBACK_MUSIC)
    try:
        out["tempo"] = max(40, min(160, int(p.get("tempo", 72))))
    except Exception:
        pass
    if isinstance(p.get("key"), str):       out["key"] = p["key"][:30]
    if isinstance(p.get("mood_in_sound"), str): out["mood_in_sound"] = p["mood_in_sound"][:240]
    if isinstance(p.get("color_to_sound"), str): out["color_to_sound"] = p["color_to_sound"][:300]
    if isinstance(p.get("shape_to_rhythm"), str): out["shape_to_rhythm"] = p["shape_to_rhythm"][:300]
    if isinstance(p.get("instruments"), list) and len(p["instruments"]) >= 1:
        out["instruments"] = [str(s)[:30] for s in p["instruments"][:3]]
    if isinstance(p.get("chord_progression"), list):
        chords = [c for c in p["chord_progression"] if isinstance(c, str) and _CHORD_RE.match(c)]
        if chords: out["chord_progression"] = chords[:8]
    if isinstance(p.get("bass_notes"), list):
        bass = [n for n in p["bass_notes"] if isinstance(n, str) and _NOTE_RE.match(n)]
        if bass: out["bass_notes"] = bass[:8]
    if isinstance(p.get("melody_notes"), list):
        mel = [n for n in p["melody_notes"] if isinstance(n, str) and _NOTE_RE.match(n)]
        if len(mel) >= 4: out["melody_notes"] = mel[:32]
    # Equalize chord/bass length
    if len(out["bass_notes"]) != len(out["chord_progression"]):
        n = min(len(out["bass_notes"]), len(out["chord_progression"]))
        if n >= 2:
            out["bass_notes"] = out["bass_notes"][:n]
            out["chord_progression"] = out["chord_progression"][:n]
        else:
            out["bass_notes"] = FALLBACK_MUSIC["bass_notes"]
            out["chord_progression"] = FALLBACK_MUSIC["chord_progression"]
    return out


def synthesize_audio_with_elevenlabs(text: str, out_path: Path) -> bool:
    if not ELEVENLABS_API_KEY:
        return False
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"
    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "accept": "audio/mpeg",
        "content-type": "application/json",
    }
    body = {
        "text": text,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {"stability": 0.45, "similarity_boost": 0.75, "style": 0.35},
    }
    try:
        r = requests.post(url, headers=headers, json=body, timeout=60)
        if r.status_code != 200:
            app.logger.warning("ElevenLabs %s: %s", r.status_code, r.text[:200])
            return False
        out_path.write_bytes(r.content)
        return True
    except Exception as e:
        app.logger.warning("ElevenLabs request failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def allowed_image(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_IMAGE_EXT


def fetch_artwork(art_id: int):
    return get_db().execute("SELECT * FROM artworks WHERE id = ?", (art_id,)).fetchone()


def music_params_for(row):
    raw = row["music_params"] if "music_params" in row.keys() else None
    if not raw:
        return FALLBACK_MUSIC
    try:
        return sanitize_music_params(json.loads(raw))
    except Exception:
        return FALLBACK_MUSIC


@app.context_processor
def inject_globals():
    u = current_user()
    return {
        "now": datetime.now,
        "has_openai": bool(OPENAI_API_KEY),
        "has_elevenlabs": bool(ELEVENLABS_API_KEY),
        "current_user": u,
        "is_admin": is_admin(u),
    }


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------
@app.route("/signup", methods=["GET", "POST"])
def signup():
    if current_user():
        return redirect(url_for("index"))
    if request.method == "GET":
        return render_template("signup.html")

    username = (request.form.get("username") or "").strip()
    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""
    confirm = request.form.get("confirm") or ""

    errors = []
    if len(username) < 3:  errors.append("Username must be at least 3 characters.")
    if "@" not in email:   errors.append("Please enter a valid email.")
    if len(password) < 6:  errors.append("Password must be at least 6 characters.")
    if password != confirm: errors.append("Passwords do not match.")
    if errors:
        for e in errors: flash(e, "danger")
        return render_template("signup.html", username=username, email=email)

    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO users (username, email, password_hash) VALUES (?, ?, ?)",
            (username, email, generate_password_hash(password, method="pbkdf2:sha256")),
        )
        db.commit()
        session["user_id"] = cur.lastrowid
        flash(f"Welcome to Revision, {username}!", "success")
        return redirect(url_for("index"))
    except sqlite3.IntegrityError:
        flash("Username or email already taken.", "danger")
        return render_template("signup.html", username=username, email=email)


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user():
        return redirect(url_for("index"))
    if request.method == "GET":
        return render_template("login.html", next=request.args.get("next", ""))

    identifier = (request.form.get("identifier") or "").strip().lower()
    password = request.form.get("password") or ""
    nxt = request.form.get("next") or url_for("index")

    user = get_db().execute(
        "SELECT * FROM users WHERE LOWER(username) = ? OR LOWER(email) = ?",
        (identifier, identifier),
    ).fetchone()
    if not user or not check_password_hash(user["password_hash"], password):
        flash("Invalid username/email or password.", "danger")
        return render_template("login.html", identifier=identifier, next=nxt)

    session.clear()
    session["user_id"] = user["id"]
    flash(f"Welcome back, {user['username']}.", "success")
    return redirect(nxt)


@app.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    flash("Signed out.", "success")
    return redirect(url_for("index"))


@app.route("/profile")
@login_required
def profile():
    u = current_user()
    db = get_db()

    fav_count    = db.execute(
        "SELECT COUNT(*) c FROM user_favorites WHERE user_id = ?", (u["id"],)
    ).fetchone()["c"]
    upload_count = db.execute(
        "SELECT COUNT(*) c FROM artworks WHERE created_by = ?", (u["id"],)
    ).fetchone()["c"]
    play_count   = db.execute(
        "SELECT COUNT(*) c FROM plays WHERE user_id = ?", (u["id"],)
    ).fetchone()["c"]

    favorites = db.execute(
        """SELECT a.id, a.title, a.artist, a.image_file, a.alt_text,
                  a.listen_count, a.music_file
           FROM artworks a
           JOIN user_favorites f ON f.artwork_id = a.id
           WHERE f.user_id = ?
           ORDER BY f.created_at DESC""",
        (u["id"],),
    ).fetchall()

    uploads = db.execute(
        """SELECT id, title, artist, image_file, alt_text, listen_count,
                  music_file, created_at
           FROM artworks WHERE created_by = ?
           ORDER BY created_at DESC""",
        (u["id"],),
    ).fetchall()

    recent = db.execute(
        """SELECT a.id, a.title, a.artist, p.played_at
           FROM plays p JOIN artworks a ON a.id = p.artwork_id
           WHERE p.user_id = ?
           ORDER BY p.played_at DESC LIMIT 8""",
        (u["id"],),
    ).fetchall()

    return render_template(
        "profile.html",
        user=u,
        stats={"favorites": fav_count, "uploads": upload_count, "plays": play_count},
        favorites=favorites, uploads=uploads, recent=recent,
    )


# ---------------------------------------------------------------------------
# Main routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    db = get_db()
    featured_rows = db.execute(
        "SELECT * FROM artworks ORDER BY listen_count DESC, id ASC LIMIT 3"
    ).fetchall()
    featured = annotate_favorites(featured_rows)
    stats = {
        "total": db.execute("SELECT COUNT(*) c FROM artworks").fetchone()["c"],
        "listens": db.execute(
            "SELECT COALESCE(SUM(listen_count),0) c FROM artworks"
        ).fetchone()["c"],
    }
    return render_template("index.html", featured=featured, stats=stats)


GALLERY_PAGE_SIZE = 6
SORT_OPTIONS = {
    "newest": ("id", "DESC"),
    "oldest": ("id", "ASC"),
    "popular": ("listen_count", "DESC"),
    "title":   ("LOWER(title)", "ASC"),
}


@app.route("/gallery")
def gallery():
    query = request.args.get("q", "").strip()
    mood = request.args.get("mood", "").strip().lower()
    sort = request.args.get("sort", "newest")
    if sort not in SORT_OPTIONS:
        sort = "newest"
    try:
        page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        page = 1

    where = []
    params = []
    if query:
        like = f"%{query.lower()}%"
        where.append("(LOWER(title) LIKE ? OR LOWER(artist) LIKE ? OR LOWER(mood) LIKE ?)")
        params.extend([like, like, like])
    if mood:
        where.append("LOWER(mood) LIKE ?")
        params.append(f"%{mood}%")
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    order_col, order_dir = SORT_OPTIONS[sort]

    db = get_db()
    total = db.execute(f"SELECT COUNT(*) c FROM artworks {where_sql}", params).fetchone()["c"]
    total_pages = max(1, (total + GALLERY_PAGE_SIZE - 1) // GALLERY_PAGE_SIZE)
    page = min(page, total_pages)
    offset = (page - 1) * GALLERY_PAGE_SIZE

    rows = db.execute(
        f"SELECT * FROM artworks {where_sql} "
        f"ORDER BY {order_col} {order_dir}, id ASC "
        f"LIMIT ? OFFSET ?",
        params + [GALLERY_PAGE_SIZE, offset],
    ).fetchall()
    artworks = annotate_favorites(rows)

    # Top moods for filter chips
    mood_counts = {}
    for r in db.execute("SELECT mood FROM artworks WHERE mood IS NOT NULL").fetchall():
        for tok in (r["mood"] or "").split(","):
            t = tok.strip()
            if t:
                k = t.lower()
                mood_counts[k] = mood_counts.get(k, (t, 0))
                label, n = mood_counts[k]
                mood_counts[k] = (label, n + 1)
    moods = sorted(
        [(k, v[0], v[1]) for k, v in mood_counts.items()],
        key=lambda x: -x[2],
    )[:10]

    return render_template(
        "gallery.html",
        artworks=artworks, query=query, mood=mood, sort=sort,
        page=page, total_pages=total_pages, total=total,
        moods=moods,
    )


@app.route("/artwork/<int:art_id>")
def artwork_detail(art_id):
    art = fetch_artwork(art_id)
    if art is None:
        abort(404)
    db = get_db()
    art_dict = dict(art, favorited=user_favorited(art_id))
    music = music_params_for(art)

    prev_row = db.execute(
        "SELECT id FROM artworks WHERE id < ? ORDER BY id DESC LIMIT 1", (art_id,)
    ).fetchone()
    next_row = db.execute(
        "SELECT id FROM artworks WHERE id > ? ORDER BY id ASC LIMIT 1", (art_id,)
    ).fetchone()

    related = db.execute(
        "SELECT id, title, artist, image_file FROM artworks WHERE id != ? "
        "ORDER BY RANDOM() LIMIT 3",
        (art_id,),
    ).fetchall()
    music_url = (
        url_for("static", filename=art["music_file"])
        if art["music_file"] else None
    )
    return render_template(
        "artwork_detail.html",
        artwork=art_dict, music=music, related=related,
        music_url=music_url,
        music_prompt=art["music_prompt"],
        prev_id=prev_row["id"] if prev_row else None,
        next_id=next_row["id"] if next_row else None,
    )


@app.route("/api/play/<int:art_id>", methods=["POST"])
def record_play(art_id):
    art = fetch_artwork(art_id)
    if art is None:
        return jsonify({"ok": False}), 404
    u = current_user()
    db = get_db()
    db.execute("UPDATE artworks SET listen_count = listen_count + 1 WHERE id = ?", (art_id,))
    db.execute("INSERT INTO plays (artwork_id, user_id) VALUES (?, ?)",
               (art_id, u["id"] if u else None))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/favorite/<int:art_id>", methods=["POST"])
@login_required
def toggle_favorite(art_id):
    art = fetch_artwork(art_id)
    if art is None:
        return jsonify({"ok": False}), 404
    u = current_user()
    db = get_db()
    existing = db.execute(
        "SELECT 1 FROM user_favorites WHERE user_id = ? AND artwork_id = ?",
        (u["id"], art_id)
    ).fetchone()
    if existing:
        db.execute("DELETE FROM user_favorites WHERE user_id = ? AND artwork_id = ?",
                   (u["id"], art_id))
        db.commit()
        return jsonify({"ok": True, "favorite": False})
    db.execute("INSERT INTO user_favorites (user_id, artwork_id) VALUES (?, ?)",
               (u["id"], art_id))
    db.commit()
    return jsonify({"ok": True, "favorite": True})


@app.route("/api/artwork/<int:art_id>", methods=["DELETE"])
@login_required
def delete_artwork(art_id):
    art = fetch_artwork(art_id)
    if art is None:
        return jsonify({"ok": False, "error": "not found"}), 404
    u = current_user()
    if not (is_admin() or (art["created_by"] is not None and art["created_by"] == u["id"])):
        return jsonify({"ok": False, "error": "not allowed"}), 403
    db = get_db()
    db.execute("DELETE FROM user_favorites WHERE artwork_id = ?", (art_id,))
    db.execute("DELETE FROM plays WHERE artwork_id = ?", (art_id,))
    db.execute("DELETE FROM artworks WHERE id = ?", (art_id,))
    db.commit()

    for rel in (art["image_file"], art["audio_file"], art["music_file"]):
        if not rel:
            continue
        p = STATIC_DIR / rel
        try:
            if p.is_file() and STATIC_DIR in p.resolve().parents:
                p.unlink()
        except OSError as e:
            app.logger.info("could not remove %s: %s", p, e)
    return jsonify({"ok": True, "deleted": art_id})


@app.route("/api/regenerate-music/<int:art_id>", methods=["POST"])
@login_required
def regenerate_music(art_id):
    art = fetch_artwork(art_id)
    if art is None:
        return jsonify({"ok": False, "error": "not found"}), 404
    img_path = STATIC_DIR / art["image_file"]
    if not img_path.exists():
        return jsonify({"ok": False, "error": "image missing"}), 404

    result = analyze_image(img_path, title=art["title"], artist=art["artist"])
    db = get_db()
    db.execute(
        """UPDATE artworks SET
           description = ?, sonification = ?, mood = ?, palette = ?,
           alt_text = ?, music_params = ?, music_prompt = ?
           WHERE id = ?""",
        (result["description"], result["sonification"], result["mood"],
         result["palette"], result["alt_text"],
         json.dumps(result["music_params"]), result["music_prompt"], art_id),
    )
    db.commit()

    music_url = None
    if ELEVENLABS_API_KEY:
        filename = f"music_{art_id}_{uuid.uuid4().hex[:8]}.mp3"
        out_path = AUDIO_DIR / filename
        if generate_music_with_elevenlabs(result["music_prompt"], out_path, duration=22):
            rel = f"audio/{filename}"
            db.execute("UPDATE artworks SET music_file = ? WHERE id = ?", (rel, art_id))
            db.commit()
            music_url = url_for("static", filename=rel)

    return jsonify({
        "ok": True,
        "music_params": result["music_params"],
        "music_prompt": result["music_prompt"],
        "music_url": music_url,
        "el_used": bool(music_url),
    })


@app.route("/api/compose-music/<int:art_id>", methods=["POST"])
def compose_music_for_existing(art_id):
    """Generate ElevenLabs music for an artwork that already has a prompt
    (e.g. a seed). Open to all logged-in users; no re-analysis."""
    if not current_user():
        return jsonify({"ok": False, "error": "login required"}), 401
    art = fetch_artwork(art_id)
    if art is None:
        return jsonify({"ok": False, "error": "not found"}), 404
    if not ELEVENLABS_API_KEY:
        return jsonify({"ok": False, "error": "ElevenLabs not configured"}), 502

    prompt = art["music_prompt"] or ""
    if not prompt:
        try:
            mp = json.loads(art["music_params"] or "{}")
        except Exception:
            mp = {}
        prompt = build_music_prompt_from_params(
            mp, mood=art["mood"] or "", description=art["description"] or "",
        )

    filename = f"music_{art_id}_{uuid.uuid4().hex[:8]}.mp3"
    out_path = AUDIO_DIR / filename
    if not generate_music_with_elevenlabs(prompt, out_path, duration=22):
        return jsonify({"ok": False, "error": "music generation failed"}), 502

    rel = f"audio/{filename}"
    db = get_db()
    db.execute(
        "UPDATE artworks SET music_file = ?, music_prompt = ? WHERE id = ?",
        (rel, prompt, art_id),
    )
    db.commit()
    return jsonify({"ok": True, "music_url": url_for("static", filename=rel)})


@app.route("/api/generate-narration/<int:art_id>", methods=["POST"])
@login_required
def generate_narration(art_id):
    art = fetch_artwork(art_id)
    if art is None:
        return jsonify({"ok": False, "error": "not found"}), 404

    parts = [
        f"{art['title']}, by {art['artist']}.",
        art["description"] or "",
        "Sonification: " + (art["sonification"] or ""),
    ]
    narration = " ".join(p for p in parts if p.strip())

    filename = f"art_{art_id}_{uuid.uuid4().hex[:8]}.mp3"
    out_path = AUDIO_DIR / filename
    ok = synthesize_audio_with_elevenlabs(narration, out_path)
    if not ok:
        return jsonify({
            "ok": False,
            "error": "ElevenLabs not configured or request failed."
        }), 502

    rel = f"audio/{filename}"
    db = get_db()
    db.execute("UPDATE artworks SET audio_file = ? WHERE id = ?", (rel, art_id))
    db.commit()
    return jsonify({"ok": True, "audio_url": url_for("static", filename=rel)})


@app.route("/upload", methods=["GET", "POST"])
@login_required
def upload():
    if request.method == "GET":
        return render_template("upload.html")

    file = request.files.get("image")
    title = (request.form.get("title") or "Untitled").strip()
    artist = (request.form.get("artist") or "Unknown").strip()

    # Optional mood/style keywords picked or typed by the uploader. Merge the
    # checkbox selections with the freeform field, dedupe, and cap length so we
    # can't blow up the prompt sent to OpenAI.
    chip_keywords = request.form.getlist("keywords")
    custom_kw = (request.form.get("keywords_custom") or "").strip()
    custom_parts = [p.strip() for p in custom_kw.split(",") if p.strip()]
    seen, merged = set(), []
    for k in chip_keywords + custom_parts:
        kl = k.lower()
        if kl in seen:
            continue
        seen.add(kl)
        merged.append(k[:40])
    keywords_str = ", ".join(merged)[:240]

    if not file or file.filename == "":
        flash("Please choose an image file.", "danger")
        return redirect(url_for("upload"))
    if not allowed_image(file.filename):
        flash("Unsupported image type. Use PNG, JPG, JPEG, WEBP, or GIF.", "danger")
        return redirect(url_for("upload"))

    ext = file.filename.rsplit(".", 1)[1].lower()
    safe_name = f"upload_{uuid.uuid4().hex[:10]}.{ext}"
    img_path = IMAGES_DIR / safe_name
    file.save(img_path)
    rel_image = f"images/{safe_name}"

    ai = analyze_image(img_path, title=title, artist=artist, keywords=keywords_str)

    u = current_user()
    db = get_db()
    cur = db.execute(
        """INSERT INTO artworks
           (title, artist, image_file, alt_text, description,
            sonification, mood, palette, music_params, music_prompt, created_by)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (title, artist, rel_image, ai["alt_text"], ai["description"],
         ai["sonification"], ai["mood"], ai["palette"],
         json.dumps(ai["music_params"]), ai["music_prompt"],
         u["id"] if u else None),
    )
    new_id = cur.lastrowid
    db.commit()

    if ELEVENLABS_API_KEY and ai.get("music_prompt"):
        filename = f"music_{new_id}_{uuid.uuid4().hex[:8]}.mp3"
        out_path = AUDIO_DIR / filename
        if generate_music_with_elevenlabs(ai["music_prompt"], out_path, duration=22):
            db.execute("UPDATE artworks SET music_file = ? WHERE id = ?",
                       (f"audio/{filename}", new_id))
            db.commit()

    flash(f"“{title}” composed and added to the gallery.", "success")
    return redirect(url_for("artwork_detail", art_id=new_id))


@app.route("/dashboard")
def dashboard():
    db = get_db()
    u = current_user()
    my_favorites = []
    if u:
        my_favorites = db.execute(
            """SELECT a.id, a.title, a.artist, a.image_file, a.alt_text,
                      a.listen_count, a.music_file, f.created_at AS favorited_at
               FROM artworks a
               JOIN user_favorites f ON f.artwork_id = a.id
               WHERE f.user_id = ?
               ORDER BY f.created_at DESC""",
            (u["id"],),
        ).fetchall()
    total = db.execute("SELECT COUNT(*) c FROM artworks").fetchone()["c"]
    listens = db.execute("SELECT COALESCE(SUM(listen_count),0) c FROM artworks").fetchone()["c"]
    favs = db.execute("SELECT COUNT(*) c FROM user_favorites").fetchone()["c"]
    with_music = db.execute(
        "SELECT COUNT(*) c FROM artworks WHERE music_params IS NOT NULL"
    ).fetchone()["c"]
    users = db.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]

    top = db.execute(
        "SELECT id, title, artist, listen_count FROM artworks "
        "ORDER BY listen_count DESC, id ASC LIMIT 6"
    ).fetchall()

    recent_plays = db.execute(
        """SELECT a.title, a.artist, p.played_at
           FROM plays p JOIN artworks a ON a.id = p.artwork_id
           ORDER BY p.played_at DESC LIMIT 8"""
    ).fetchall()

    mood_rows = db.execute(
        "SELECT mood, COUNT(*) c FROM artworks WHERE mood IS NOT NULL GROUP BY mood"
    ).fetchall()
    mood_counter = {}
    for row in mood_rows:
        for tok in (row["mood"] or "").split(","):
            t = tok.strip().capitalize()
            if t:
                mood_counter[t] = mood_counter.get(t, 0) + row["c"]
    mood_labels = list(mood_counter.keys())[:6]
    mood_values = [mood_counter[k] for k in mood_labels]

    # Plays per day (last 14 days, including days with zero plays).
    days = 14
    today = datetime.utcnow().date()
    play_rows = db.execute(
        "SELECT substr(played_at, 1, 10) d, COUNT(*) c "
        "FROM plays WHERE played_at >= ? GROUP BY d",
        ((today - timedelta(days=days - 1)).isoformat(),),
    ).fetchall()
    play_by_day = {row["d"]: row["c"] for row in play_rows}
    plays_labels = []
    plays_values = []
    for i in range(days):
        d = today - timedelta(days=days - 1 - i)
        plays_labels.append(d.strftime("%b %d"))
        plays_values.append(play_by_day.get(d.isoformat(), 0))

    # Tempo buckets from music_params JSON.
    tempo_rows = db.execute(
        "SELECT music_params FROM artworks WHERE music_params IS NOT NULL"
    ).fetchall()
    tempo_buckets = {"Slow (<70 BPM)": 0, "Medium (70–100)": 0, "Fast (>100)": 0}
    for row in tempo_rows:
        try:
            tempo = (json.loads(row["music_params"]) or {}).get("tempo")
            tempo = int(tempo) if tempo is not None else None
        except (ValueError, TypeError, json.JSONDecodeError):
            tempo = None
        if tempo is None:
            continue
        if tempo < 70:
            tempo_buckets["Slow (<70 BPM)"] += 1
        elif tempo <= 100:
            tempo_buckets["Medium (70–100)"] += 1
        else:
            tempo_buckets["Fast (>100)"] += 1
    tempo_labels = list(tempo_buckets.keys())
    tempo_values = list(tempo_buckets.values())

    # Top artists by number of works in collection.
    artist_rows = db.execute(
        "SELECT artist, COUNT(*) c, COALESCE(SUM(listen_count),0) plays "
        "FROM artworks GROUP BY artist ORDER BY c DESC, plays DESC LIMIT 6"
    ).fetchall()
    artist_labels = [r["artist"] for r in artist_rows]
    artist_values = [r["c"] for r in artist_rows]

    stats = {"total": total, "listens": listens, "favorites": favs,
             "with_audio": with_music, "users": users}
    return render_template(
        "dashboard.html",
        stats=stats, top=top, recent_plays=recent_plays,
        mood_labels=mood_labels, mood_values=mood_values,
        plays_labels=plays_labels, plays_values=plays_values,
        tempo_labels=tempo_labels, tempo_values=tempo_values,
        artist_labels=artist_labels, artist_values=artist_values,
        my_favorites=my_favorites,
    )


@app.route("/slideshow")
def slideshow():
    db = get_db()
    rows = db.execute(
        "SELECT id, title, artist, image_file, music_file, alt_text "
        "FROM artworks WHERE image_file IS NOT NULL AND image_file != '' "
        "ORDER BY listen_count DESC, id ASC"
    ).fetchall()
    items = [{
        "id":     r["id"],
        "title":  r["title"],
        "artist": r["artist"],
        "image":  url_for("static", filename=r["image_file"]),
        "music":  url_for("static", filename=r["music_file"]) if r["music_file"] else None,
        "alt":    r["alt_text"] or r["title"],
    } for r in rows]
    return render_template("slideshow.html", items=items)


@app.errorhandler(404)
def not_found(_):
    return render_template("404.html"), 404


@app.errorhandler(413)
def too_large(_):
    flash(f"File is too large. Max {MAX_UPLOAD_MB} MB.", "danger")
    return redirect(url_for("upload"))


with app.app_context():
    init_db()


if __name__ == "__main__":
    app.run(debug=True, port=5001)
