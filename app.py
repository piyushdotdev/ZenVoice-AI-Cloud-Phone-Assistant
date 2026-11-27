from flask import Flask, request, Response, url_for
from werkzeug.middleware.proxy_fix import ProxyFix
from openai import OpenAI
import firebase_admin
from firebase_admin import credentials, firestore
import os
import re
import random
import uuid
from datetime import datetime, timezone
import requests
from emotion_detection import detect_emotion_from_text, detect_emotion_from_audio
from twilio.twiml.voice_response import VoiceResponse, Start, Pause, Gather, Hangup
import tempfile
import wave
import audioop
from twilio.rest import Client
from config import TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, OPENAI_API_KEY, FIREBASE_CREDENTIALS_PATH, OPENAI_MODEL
from config import ENABLE_WHATSAPP, WHATSAPP_FROM, ENABLE_VOICE_CASCADE
from functools import lru_cache
import hashlib
import json
import base64
import threading
from collections import deque
import time as _time

app = Flask(__name__)
# Trust proxy headers from ngrok so url_for() generates the correct public HTTPS URL
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# ── Public URL helper: auto-detects ngrok URL dynamically ──
_ngrok_url_cache = {'url': os.getenv('PUBLIC_URL', '').rstrip('/'), 'ts': 0}

def _get_base_url():
    """Dynamically fetch ngrok public URL; cache for 60s. Falls back to PUBLIC_URL env var."""
    global _ngrok_url_cache
    now = _time.time()
    # Refresh cache every 60 seconds
    if now - _ngrok_url_cache['ts'] > 60:
        try:
            r = requests.get('http://127.0.0.1:4040/api/tunnels', timeout=2)
            tunnels = r.json().get('tunnels', [])
            https = [t for t in tunnels if t.get('public_url','').startswith('https')]
            url = (https or tunnels)[0]['public_url'].rstrip('/')
            if url != _ngrok_url_cache['url']:
                print(f"[ngrok] Updated public URL: {url}")
            _ngrok_url_cache = {'url': url, 'ts': now}
        except Exception:
            _ngrok_url_cache['ts'] = now
    return _ngrok_url_cache['url']

def public_url_for(endpoint):
    """Return the full public HTTPS URL for a Flask endpoint."""
    path_map = {
        'process_speech': '/process_speech',
        'voice':          '/voice',
        'cascade_status': '/cascade_status',
        'cascade_ack':    '/cascade_ack',
        'media':          '/media',
    }
    base = _get_base_url()
    path = path_map.get(endpoint, f'/{endpoint}')
    return base + path

print(f"[config] Initial public URL: {_get_base_url() or 'NOT SET — update PUBLIC_URL in .env'}")

# WebSocket support for Twilio Media Streams
from flask_sock import Sock
sock = Sock(app)

# Initialize Twilio client
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN else None

# Initialize OpenAI client with keep-alive
from config import OPENAI_BASE_URL
if OPENAI_BASE_URL:
    client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL, timeout=15, max_retries=2)
else:
    client = OpenAI(api_key=OPENAI_API_KEY, timeout=15, max_retries=2)

# Initialize Firebase
credentials_path = FIREBASE_CREDENTIALS_PATH or 'firebase_credentials.json'
db = None
if os.path.exists(credentials_path):
    try:
        cred = credentials.Certificate(credentials_path)
        firebase_admin.initialize_app(cred)
        db = firestore.client()
        print("[Firebase] Initialized successfully")
    except Exception as e:
        print(f"[Firebase] Init error: {e}")

# In-memory storage
user_memory = {}
conversation_memory = {}
call_sessions = {}
call_logs = {}
_user_cache = {}
_USER_CACHE_TTL = 300
cascade_sessions = {}
_stream_state = {}

# ── Live Dashboard SSE ─────────────────────────────────────────────────────
# Stores SSE subscriber queues: each connected browser gets its own Queue.
_dashboard_subscribers: list = []   # list of queue.Queue instances
_dashboard_lock = threading.Lock()
_active_calls: set = set()  # call SIDs currently active

def _emit_dashboard_event(event_type: str, payload: dict):
    """Broadcast a server-sent event to all connected dashboard clients."""
    data = json.dumps(payload)
    msg = f"event: {event_type}\ndata: {data}\n\n"
    with _dashboard_lock:
        dead = []
        for q in _dashboard_subscribers:
            try:
                q.put_nowait(msg)
            except Exception:
                dead.append(q)
        for q in dead:
            _dashboard_subscribers.remove(q)

# ── Language and Voice Helpers ──────────────────────────────────────────────
LANG_MAP = {
    'english': 'en-IN',
    'en': 'en-IN',
    'hindi': 'hi-IN',
    'hi': 'hi-IN',
}

BACKCHANNELS_EN = ["Okay.", "Hmm.", "Alright.", "Got it.", "I see."]
BACKCHANNELS_HI = ["ठीक है।", "हूँ।", "अच्छा।", "हाँ।", "जी।"]

HINGLISH_MARKERS = {
    'kya', 'kyu', 'kyun', 'kaise', 'kab', 'kaha', 'kaun', 'kisko', 'kis',
    'hai', 'ho', 'hoon', 'hain', 'tha', 'thi', 'the', 'hoga', 'hogi',
    'nahi', 'nahin', 'mat', 'haan', 'ha', 'ji', 'jee',
    'aap', 'aapka', 'aapko', 'tum', 'tumhara', 'mera', 'meri', 'mere', 'uska', 'yeh', 'woh',
    'naam', 'batao', 'bataiye', 'sunao', 'karo', 'kijiye', 'dijiye',
    'thoda', 'bahut', 'bohot', 'jyada', 'kam', 'theek', 'thik', 'sahi', 'galat',
    'kal', 'aaj', 'abhi', 'pehle', 'baad', 'phir',
    'bhai', 'dost', 'yaar', 'beta', 'beti',
    'namaste', 'namaskar', 'pranam',
    'kripya', 'dhanyavaad', 'shukriya', 'maaf',
    'accha', 'acha', 'achha', 'bas', 'chalo', 'arre', 'kuch',
    'samajh', 'pata', 'malum', 'milega', 'chahiye', 'chahie',
    'bolo', 'bola', 'boli', 'bole', 'kaha', 'kahi',
    'help', 'madad', 'zarurat', 'jarurat'
}

def detect_lang_from_text(text: str) -> str:
    """Detect language from text - returns hi-IN or en-IN."""
    t = (text or "").strip()
    if not t:
        return 'en-IN'
    
    if re.search(r"[\u0900-\u097F]", t):
        return 'hi-IN'
    
    tl = t.lower()
    words = re.findall(r"\b[a-z]+\b", tl)
    if not words:
        return 'en-IN'
    
    hinglish_count = sum(1 for w in words if w in HINGLISH_MARKERS)
    
    if len(words) > 0 and (hinglish_count / len(words)) >= 0.3:
        return 'hi-IN'
    
    if len(words) <= 5 and hinglish_count >= 2:
        return 'hi-IN'
    
    return 'en-IN'

def get_dynamic_language(user_data):
    """Get language code for current turn."""
    if user_data and user_data.get('last_user_text'):
        detected = detect_lang_from_text(user_data.get('last_user_text'))
        if detected == 'hi-IN':
            return detected
    return 'en-IN'

def preferred_lang_code(user_data):
    lang = (user_data or {}).get('language', 'English').lower()
    for k, v in LANG_MAP.items():
        if k in lang:
            return v
    return 'en-IN'

def split_for_voice(text: str, max_len: int = 140):
    if not text:
        return [""]
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    chunks = []
    for p in parts:
        if not p:
            continue
        if len(p) <= max_len:
            chunks.append(p)
        else:
            for i in range(0, len(p), max_len):
                chunks.append(p[i:i+max_len])
    return chunks or [text[:max_len]]

def speak(target, text, user_data=None, add_backchannel=False):
    preferred = preferred_lang_code(user_data)
    
    if user_data and user_data.get('last_user_text'):
        turn_lang = detect_lang_from_text(user_data.get('last_user_text')) or preferred
    else:
        turn_lang = preferred

    # Twilio's 'alice' voice does NOT support Hindi — it will refuse and say
    # "I can only speak English".  Use Polly.Aditi (Amazon Polly via Twilio)
    # for Hindi, and keep alice for English.
    if turn_lang.startswith('hi'):
        voice = 'Polly.Aditi'
    else:
        voice = 'alice'

    if add_backchannel:
        bc = random.choice(BACKCHANNELS_HI if turn_lang.startswith('hi') else BACKCHANNELS_EN)
        target.say(bc, language=turn_lang, voice=voice)

    if text:
        target.say(text.strip(), language=turn_lang, voice=voice)

# ── Cache for GPT responses ─────────────────────────────────────────────────
# Real dict-based cache (lru_cache on a function that always returns None is a no-op).
_gpt_response_cache: dict = {}      # {(prompt_hash, context_hash): text}
_GPT_CACHE_MAX = 50                  # evict oldest when full

def get_cached_response(prompt_hash: str, context_hash: str):
    """Return cached GPT response or None."""
    return _gpt_response_cache.get((prompt_hash, context_hash))

def store_cached_response(prompt_hash: str, context_hash: str, text: str):
    """Store a GPT response. Evict oldest entry when cache is full."""
    key = (prompt_hash, context_hash)
    if len(_gpt_response_cache) >= _GPT_CACHE_MAX:
        _gpt_response_cache.pop(next(iter(_gpt_response_cache)))  # FIFO eviction
    _gpt_response_cache[key] = text

# ── User Data Management ───────────────────────────────────────────────────
def get_user_data(phone_number):
    """Get user data with caching."""
    cached = _user_cache.get(phone_number)
    if cached:
        data, ts = cached
        if _time.time() - ts < _USER_CACHE_TTL:
            return data
    
    if db:
        try:
            doc = db.collection('users').document(phone_number).get()
            if doc.exists:
                data = doc.to_dict()
                _user_cache[phone_number] = (data, _time.time())
                return data
        except Exception as e:
            print(f"[Firebase] Read error: {e}")
    
    _user_cache[phone_number] = (None, _time.time())
    return user_memory.get(phone_number)

def save_user_data(phone_number, data):
    """Save user data and update cache."""
    _user_cache[phone_number] = (data, _time.time())
    if db:
        try:
            db.collection('users').document(phone_number).set(data, merge=True)
        except Exception as e:
            print(f"[Firebase] Write error: {e}")
    else:
        user_memory[phone_number] = data

def get_or_init_user(phone_number):
    data = get_user_data(phone_number)
    if not data:
        data = {'last_emotion': 'neutral', 'language': 'English', 'call_count': 0}
        save_user_data(phone_number, data)
    return data

def log_mood(phone_number: str, emotion: str):
    entry = {
        't': datetime.now(timezone.utc).isoformat(),
        'emotion': emotion
    }
    if db:
        try:
            db.collection('users').document(phone_number).update({
                'moods': firestore.ArrayUnion([entry])
            })
        except Exception:
            doc = db.collection('users').document(phone_number)
            cur = doc.get().to_dict() or {}
            moods = (cur.get('moods') or []) + [entry]
            doc.set({**cur, 'moods': moods})
    else:
        rec = user_memory.setdefault(phone_number, {})
        moods = rec.setdefault('moods', [])
        moods.append(entry)
        if len(moods) > 50:
            del moods[:-50]

# ── Phone Number Helpers ───────────────────────────────────────────────────
def normalize_for_twilio(num: str) -> str:
    if not num:
        return num
    if num.startswith('+') or num.startswith('whatsapp:+'):
        return num
    digits = re.sub(r"[^\d]", "", num)
    if digits.startswith('91') and len(digits) >= 12:
        return '+' + digits
    if len(digits) == 10:
        return '+91' + digits
    if len(digits) == 12 and digits.startswith('91'):
        return '+' + digits
    return '+' + digits if digits else num

def validate_phone_number(number: str) -> bool:
    """Validate if a string is a proper phone number."""
    digits = re.sub(r"[^\d]", "", number)
    # Indian numbers: 10 digits, or 12 digits starting with 91
    return (len(digits) == 10) or (len(digits) == 12 and digits.startswith('91'))

# ── Emergency Contact Parsing (FIXED) ──────────────────────────────────────
def parse_contacts_from_text(text: str):
    """Robust extractor: returns list of (label, number) from spoken or typed text."""
    pairs = []

    # Word to digit mapping (English + Hindi)
    word_to_num = {
        'zero': '0', 'one': '1', 'two': '2', 'three': '3', 'four': '4',
        'five': '5', 'six': '6', 'seven': '7', 'eight': '8', 'nine': '9',
        'shunya': '0', 'ek': '1', 'do': '2', 'teen': '3', 'chaar': '4',
        'paanch': '5', 'chhah': '6', 'saat': '7', 'aath': '8', 'nau': '9',
        'oh': '0',
    }

    t = text.lower().strip()
    print(f'[parse_contacts] raw input: {repr(t)}')

    # --- Step 1: Replace every spoken digit word with its digit ---
    # Use regex word-boundary replacement so we don't mangle real words
    for word, digit in word_to_num.items():
        t = re.sub(r'\b' + re.escape(word) + r'\b', digit, t)

    print(f'[parse_contacts] after word substitution: {repr(t)}')

    # --- Step 2: Collapse spaces/hyphens between adjacent digits ---
    for _ in range(15):  # enough passes for a full 10-digit number
        t = re.sub(r'(?<=\d)[\s\-]+(?=\d)', '', t)

    print(f'[parse_contacts] after collapsing: {repr(t)}')

    # --- Step 3: Find all digit runs of 7+ digits ---
    nums = re.findall(r'\+?\d{7,13}', t)
    print(f'[parse_contacts] candidate numbers: {nums}')

    if not nums:
        print('[parse_contacts] no valid phone number found')
        return pairs

    FILLER = {'save','add','my','the','a','is','are','was','this','that',
              'number','contact','emergency','please','call','with','and',
              'to','for','hi','hey','just','it','on','in','at','by','of','give'}

    for n in nums:
        clean = re.sub(r'[^\d]', '', n)
        if len(clean) < 7:
            continue
        # Take the first 10 digits (drop country code if 12-digit with 91 prefix)
        if len(clean) == 12 and clean.startswith('91'):
            clean = clean[2:]
        phone_number = clean[:10]

        if not validate_phone_number(phone_number):
            continue

        # --- Label extraction: look at text BEFORE the digit run ---
        pos = t.find(n)
        prefix = t[:pos].strip() if pos > 0 else ''
        label_words = re.findall(r'\b[a-z]{2,}\b', prefix)
        label = next((w for w in reversed(label_words) if w not in FILLER), None)
        name = label.title() if label else 'Contact'

        pairs.append((name, phone_number))
        print(f'[parse_contacts] extracted: label={name!r}, number={phone_number!r}')

    return pairs

def _normalize_contact_number(number: str) -> str:
    """Ensure contact number has +91 prefix."""
    if not number:
        return number
    digits = re.sub(r'[^\d]', '', number)
    if len(digits) == 10:
        return f'+91{digits}'
    elif len(digits) == 12 and digits.startswith('91'):
        return f'+{digits}'
    elif number.startswith('+'):
        return number  # already normalized international number
    return f'+{digits}' if digits else number

def save_emergency_contacts(owner_phone: str, pairs):
    if not pairs:
        return
    
    user = get_or_init_user(owner_phone)
    # Normalize ALL existing contacts (fix old entries stored without prefix)
    lst = []
    for c in (user.get('emergency_contacts') or []):
        normalized = _normalize_contact_number(c.get('number', ''))
        lst.append({**c, 'number': normalized})
    
    for name, number in pairs:
        formatted_number = _normalize_contact_number(number)
        lst.append({'name': name, 'number': formatted_number, 'saved_at': datetime.now(timezone.utc).isoformat()})
    
    # Keep last 5 contacts
    user['emergency_contacts'] = lst[-5:]
    user['emergency_contact_saved'] = True
    save_user_data(owner_phone, user)
    print(f"[Contacts] Saved {len(pairs)} contact(s) for {owner_phone}")


# ── Emergency Alert Functions (IMPROVED) ───────────────────────────────────
def send_whatsapp(to_number: str, body: str) -> bool:
    try:
        if not (ENABLE_WHATSAPP and WHATSAPP_FROM):
            return False
        to = to_number
        if not to.startswith('whatsapp:'):
            to = 'whatsapp:' + normalize_for_twilio(to)
        twilio_client.messages.create(
            body=body,
            from_=WHATSAPP_FROM,
            to=to
        )
        return True
    except Exception as e:
        print(f'[WhatsApp] Send error: {e}')
        return False

def place_contact_call(to_number: str, msg: str, caller: str):
    """Call emergency contact with a clear spoken alert message."""
    try:
        from config import TWILIO_PHONE_NUMBER

        vr = VoiceResponse()
        vr.say('This is an automated emergency alert from ZenVoice.', language='en-IN', voice='alice')
        vr.pause(length=1)
        vr.say(msg, language='en-IN', voice='alice')
        vr.pause(length=2)
        # Repeat the message once for clarity
        vr.say(msg, language='en-IN', voice='alice')
        vr.pause(length=1)
        vr.say('Please check on them immediately. Thank you.', language='en-IN', voice='alice')

        to = normalize_for_twilio(to_number)
        frm = normalize_for_twilio(TWILIO_PHONE_NUMBER)
        print(f'[Emergency] Calling {to} from {frm}')

        call = twilio_client.calls.create(to=to, from_=frm, twiml=str(vr))
        print(f'[Emergency] Contact call placed: {call.sid} to {to}')
        return call.sid

    except Exception as e:
        print(f'[Emergency] Contact call error: {e}')
        return None

def broadcast_emergency_to_contacts(from_number, summary="User requested help", location_url=None):
    """Send emergency alerts to all saved contacts."""
    from config import TWILIO_PHONE_NUMBER
    
    try:
        user = get_user_data(from_number) or {}
        contacts = user.get('emergency_contacts') or []
        
        if not contacts:
            print('[Emergency] No emergency contacts found')
            return {'success': False, 'error': 'No contacts saved', 'sent': 0}
        
        user_name = user.get('name', 'Unknown')
        now_str = datetime.now(timezone.utc).strftime('%I:%M %p UTC')
        
        # Create alert message
        body = f"🚨 EMERGENCY ALERT from ZenVoice!\n\n"
        body += f"Name: {user_name}\n"
        body += f"Phone: {from_number}\n"
        body += f"Message: {summary}\n"
        body += f"Time: {now_str}\n"
        if location_url:
            body += f"\nLocation: {location_url}\n"
        
        # Clean voice message (no emojis)
        voice_msg = (
            f"This is an emergency alert for {user_name}. "
            f"Their message: {summary}. "
            f"Please contact them immediately at {from_number}."
        )
        
        sent_count = 0
        call_count = 0

        for contact in contacts:
            try:
                number = contact.get('number')
                if not number:
                    continue

                # Try WhatsApp first if enabled
                if ENABLE_WHATSAPP and WHATSAPP_FROM:
                    if send_whatsapp(number, body):
                        sent_count += 1
                        print(f'[Emergency] WhatsApp sent to {number}')

                # Always place voice call (most reliable channel)
                call_sid = place_contact_call(number, voice_msg, from_number)
                if call_sid:
                    call_count += 1

            except Exception as e:
                print(f'[Emergency] Contact alert error for {number}: {e}')

        total_alerted = sent_count + call_count
        return {
            'success': total_alerted > 0,
            'sent': total_alerted,
            'whatsapp_sent': sent_count,
            'calls_placed': call_count,
            'total_contacts': len(contacts)
        }
        
    except Exception as e:
        print(f'[Emergency] Broadcast error: {e}')
        return {'success': False, 'error': str(e), 'sent': 0}

def trigger_emergency_alert(phone: str, user_data: dict = None):
    """Fire emergency alert via all channels."""
    name = (user_data or {}).get('name', 'Unknown')
    distress_phrase = (user_data or {}).get('last_user_text', 'distress detected')
    emotion = (user_data or {}).get('last_emotion', 'unknown')

    # Build a meaningful alert message using what the user actually said
    summary = (
        f"{name} said: '{distress_phrase}'. "
        f"Emotion detected: {emotion}. Please check on them immediately."
    )

    result = broadcast_emergency_to_contacts(phone, summary)
    print(f'[Emergency] Alert triggered for {name} ({phone}): {result}')
    return result

# ── GPT Response Generation (with caching) ─────────────────────────────────
def generate_gpt_response(user_input, user_data=None, caller_id: str = None):
    """Generate a concise, emotion-adaptive GPT response."""
    
    # Check cache first
    prompt_key = hashlib.md5(user_input.encode()).hexdigest()
    context_key = hashlib.md5(str(user_data).encode()).hexdigest()
    
    cached = get_cached_response(prompt_key, context_key)
    if cached:
        return cached
    
    # Language detection
    if re.search(r'[\u0900-\u097F]', user_input):
        turn_lang = 'Hindi'
    else:
        turn_lang = 'Hindi' if detect_lang_from_text(user_input) == 'hi-IN' else 'English'
    
    if user_data:
        user_data['last_detected_language'] = turn_lang
    
    # Emotion-adaptive tone
    emotion = (user_data or {}).get('last_emotion', 'neutral')
    emotion_tone = {
        'sad':     'Be gently supportive. Acknowledge their pain in 1 sentence.',
        'angry':   'Be calm and grounding. Validate briefly, then redirect.',
        'happy':   'Be warm and encouraging. Match their energy briefly.',
        'scared':  'Be reassuring and steady. One calming sentence.',
        'neutral': 'Be friendly and direct.',
    }.get(emotion, 'Be friendly and direct.')
    
    name = (user_data or {}).get('name', '')
    name_clause = f" The user's name is {name}." if name else ''
    
    # Check if factual question
    factual_triggers = [
        'what happened', 'what is', 'who is', 'who was', 'tell me about',
        'explain', 'history of', 'when did', 'why did', 'how did',
        'what was', 'describe', 'kya hua', 'kya hai', 'batao', 'bataiye'
    ]
    input_lower = (user_input or '').lower()
    is_factual = any(t in input_lower for t in factual_triggers)
    
    if is_factual:
        topic_instruction = (
            "Answer the question accurately and factually in 2-3 short sentences. "
            "Do NOT change the topic. Only add a brief empathetic line at the end."
        )
        max_tok = 120
        temp = 0.3
    else:
        topic_instruction = "Be conversational and emotionally aware. Keep it brief."
        max_tok = 60
        temp = 0.6
    
    system_prompt = (
        f"You are ZenVoice, a warm, empathetic voice assistant on a phone call.{name_clause}"
        f" Respond ONLY in {turn_lang}. Keep replies concise and natural for phone (1-2 sentences max)."
        f" Tone: {emotion_tone}"
        f" {topic_instruction}"
        " Never use robotic phrases. Never make up facts."
    )
    
    # Conversation history
    key = caller_id or ''
    history = conversation_memory.get(key, [])
    trimmed = [m for m in history[-4:] if m.get('content')]  # Keep last 2 exchanges
    
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(trimmed)
    messages.append({"role": "user", "content": user_input or ''})
    
    # Model selection
    model_name = OPENAI_MODEL or "gpt-3.5-turbo"
    
    _t0 = _time.time()
    try:
        response = client.chat.completions.create(
            model=model_name,
            messages=messages,
            max_tokens=max_tok,
            temperature=temp,
            timeout=10
        )

        assistant_text = response.choices[0].message.content.strip()
        print(f'[GPT] {_time.time()-_t0:.2f}s | model={model_name} | tokens={max_tok} | lang={turn_lang}')

        # Update conversation memory
        history.append({"role": "user", "content": user_input or ''})
        history.append({"role": "assistant", "content": assistant_text})
        conversation_memory[key] = history[-4:]  # Keep last 2 exchanges

        # Store in cache (do NOT clear the whole cache here)
        store_cached_response(prompt_key, context_key, assistant_text)

        return assistant_text

    except Exception as e:
        print(f'[GPT] Error after {_time.time()-_t0:.2f}s: {e}')
        fallbacks = [
            "I'm here. Can you say that again?",
            "Sorry, I didn't catch that. Please repeat.",
            "Tell me more about that."
        ]
        return random.choice(fallbacks)

# ── Voice Call Handlers ────────────────────────────────────────────────────
@app.route('/voice', methods=['GET', 'POST'])
def voice():
    """Handle incoming voice calls."""
    resp = VoiceResponse()
    
    # Get caller info
    from_number = request.values.get('From', '')
    call_sid = request.values.get('CallSid', '')
    
    # Log call start
    try:
        call_logs[call_sid] = {
            'phone': from_number,
            'started': datetime.now(timezone.utc).isoformat(),
            'turns': []
        }
        user = get_or_init_user(from_number)
        user['call_count'] = int(user.get('call_count') or 0) + 1
        save_user_data(from_number, user)
    except Exception as e:
        print(f'[Call] Log error: {e}')
    
    # Get user data
    user_data = get_user_data(from_number)
    
    # Load conversation history
    try:
        prev = (user_data or {}).get('last_conversation')
        if prev:
            conversation_memory[from_number] = prev
    except Exception as e:
        print(f'[Call] History error: {e}')
    
    # Initialize session
    name_known = bool(user_data and user_data.get('name'))
    call_sessions[call_sid] = {
        'name_captured': name_known,
        'comfort_mode': False,
        'pending_emergency': False,
        'ask_emergency_contact': False
    }
    
    # Create gather for speech input (reduced timeout for lower latency)
    gather = resp.gather(
        input='speech dtmf',
        action=public_url_for('process_speech'),
        method='POST',
        timeout=10,  # Reduced from 60
        speechTimeout='auto',
        speechModel='phone_call',
        language='en-IN',
        enhanced=True,
        bargeIn=True,
        actionOnEmptyResult=True
    )
    
    # Personalized greeting
    if name_known:
        name = user_data.get('name')
        call_count = int((user_data or {}).get('call_count', 1))
        if call_count > 1:
            greeting_text = f"Hey {name}! Good to hear from you again. How are you doing?"
        else:
            greeting_text = f"Hey {name}! I'm ZenVoice. How can I help you today?"
    else:
        greeting_text = "Hi! I'm ZenVoice. What's your name?"
    
    speak(gather, greeting_text, user_data)
    return Response(str(resp), mimetype='text/xml')

@app.route('/process_speech', methods=['GET', 'POST'])
def process_speech():
    """Process speech input and generate response."""
    resp = VoiceResponse()
    
    speech_result = request.values.get('SpeechResult', '')
    digits = request.values.get('Digits', '')
    from_number = request.values.get('From', '')
    call_sid = request.values.get('CallSid', '')
    
    user_data = get_or_init_user(from_number)
    session = call_sessions.setdefault(call_sid, {
        'name_captured': bool(user_data.get('name')),
        'comfort_mode': False,
        'pending_emergency': False,
        'ask_emergency_contact': False
    })
    
    # Handle empty input
    if not speech_result and not digits:
        gather = resp.gather(
            input='speech dtmf',
            action=public_url_for('process_speech'),
            method='POST',
            timeout=10,
            speechTimeout='auto',
            speechModel='phone_call',
            language=get_dynamic_language(user_data),
            bargeIn=True,
            actionOnEmptyResult=True
        )
        speak(gather, "I'm here. Take your time.", user_data)
        return Response(str(resp), mimetype='text/xml')
    
    # Normalize input
    user_input = speech_result if speech_result else digits
    ui_lower = user_input.lower().strip()

    # ── Explicit language-switch detection ──────────────────────────────────
    # If the user says "speak in Hindi" / "Hindi mein bolo" etc., persist the
    # preference so every subsequent reply is spoken in that language.
    lang_switch_hi = [
        'speak in hindi', 'hindi mein bolo', 'hindi me bolo', 'hindi boliye',
        'talk in hindi', 'baat karo hindi mein', 'switch to hindi',
        'hindi mai bolo', 'hindi bol', 'hindi me'
    ]
    lang_switch_en = [
        'speak in english', 'english mein bolo', 'talk in english',
        'switch to english', 'english me bolo', 'english boliye'
    ]
    if any(phrase in ui_lower for phrase in lang_switch_hi):
        user_data['language'] = 'Hindi'
        save_user_data(from_number, user_data)
        gather = resp.gather(
            input='speech dtmf',
            action=public_url_for('process_speech'),
            method='POST',
            timeout=10,
            speechTimeout='auto',
            speechModel='phone_call',
            language='en-IN',
            bargeIn=True,
            actionOnEmptyResult=True
        )
        gather.say('Bilkul! Main ab Hindi mein baat karunga.', language='hi-IN', voice='Polly.Aditi')
        return Response(str(resp), mimetype='text/xml')

    if any(phrase in ui_lower for phrase in lang_switch_en):
        user_data['language'] = 'English'
        save_user_data(from_number, user_data)
        gather = resp.gather(
            input='speech dtmf',
            action=public_url_for('process_speech'),
            method='POST',
            timeout=10,
            speechTimeout='auto',
            speechModel='phone_call',
            language='en-IN',
            bargeIn=True,
            actionOnEmptyResult=True
        )
        gather.say('Sure! I will speak in English now.', language='en-IN', voice='alice')
        return Response(str(resp), mimetype='text/xml')
    
    # Log user turn
    try:
        log = call_logs.get(call_sid)
        if log:
            log['turns'].append({'role': 'user', 'text': user_input, 't': datetime.now(timezone.utc).isoformat()})
    except Exception as e:
        print(f'[Log] Error: {e}')
    
    # Check for end/stop commands
    end_phrases = ['bye', 'goodbye', 'bye bye', 'alright bye']
    stop_phrases = ['stop', 'pause', 'enough', 'that\'s all']
    
    if any(k in ui_lower for k in end_phrases):
        speak(resp, "Okay, take care! I'm here if you need me.", user_data)
        resp.hangup()
        return Response(str(resp), mimetype='text/xml')
    
    if any(k in ui_lower for k in stop_phrases):
        gather = resp.gather(
            input='speech dtmf',
            action=public_url_for('process_speech'),
            method='POST',
            timeout=30,
            speechTimeout='auto',
            speechModel='phone_call',
            language=get_dynamic_language(user_data),
            bargeIn=True,
            actionOnEmptyResult=True
        )
        speak(gather, "Okay, I'll wait. Just say something when you're ready.", user_data)
        return Response(str(resp), mimetype='text/xml')
    
    # Emergency detection — skip if we are mid-contact-capture (user is providing a number)
    EMERGENCY_KEYWORDS = [
        'emergency', 'danger', 'help me', 'save me', 'urgent',
        'suicide', 'kill myself', 'want to die', 'end my life',
        'police', 'ambulance', 'sos', 'need help',
        'madad', 'bachao', 'khatra', 'jaan de dunga', 'mar jaana'
    ]

    is_emergency = any(k in ui_lower for k in EMERGENCY_KEYWORDS)

    if is_emergency and not session.get('pending_emergency') and not session.get('ask_emergency_contact'):
        session['pending_emergency'] = True
        user_data['last_user_text'] = user_input
        save_user_data(from_number, user_data)

        name = user_data.get('name', 'friend')

        # Check if emergency contacts exist BEFORE claiming we alerted anyone
        saved_contacts = user_data.get('emergency_contacts') or []

        gather = resp.gather(
            input='speech dtmf',
            action=public_url_for('process_speech'),
            method='POST',
            timeout=20,
            speechTimeout='auto',
            speechModel='phone_call',
            language=get_dynamic_language(user_data),
            bargeIn=True,
            actionOnEmptyResult=True
        )

        if saved_contacts:
            # Contacts exist — trigger the call now in background
            threading.Thread(
                target=trigger_emergency_alert,
                args=(from_number, dict(user_data))  # pass copy so thread has stable snapshot
            ).start()
            speak(gather, f"I am calling your emergency contact right now, {name}. Please stay on the line. You are not alone.", user_data)
        else:
            # No contacts saved — be honest and ask if they want to add one
            speak(gather, f"I want to help, {name}. But I don't have an emergency contact saved for you yet. Please tell me a phone number I can call, or say 'skip' to just talk.", user_data)
            session['ask_emergency_contact'] = True  # re-enter contact capture
            session['pending_emergency'] = False      # allow re-triggering after contact is saved

        return Response(str(resp), mimetype='text/xml')
    
    # Name capture
    if not session.get('name_captured'):
        name_match = None
        
        # Patterns for name extraction
        patterns = [
            r'(?:my name is|i am|i\'m|this is|mera naam|naam hai)\s+([A-Za-z\u0900-\u097F]+(?:\s+[A-Za-z\u0900-\u097F]+)?)',
            r'(?:call me)\s+([A-Za-z]+)',
            r'^([A-Za-z]+)$'  # Single word response
        ]
        
        for pattern in patterns:
            m = re.search(pattern, ui_lower, re.IGNORECASE)
            if m:
                name_match = m.group(1).strip().title()
                break
        
        if name_match and len(name_match) > 1:
            user_data['name'] = name_match
            save_user_data(from_number, user_data)
            session['name_captured'] = True
            
            gather = resp.gather(
                input='speech dtmf',
                action=public_url_for('process_speech'),
                method='POST',
                timeout=10,
                speechTimeout='auto',
                speechModel='phone_call',
                language=detect_lang_from_text(user_input),
                enhanced=True,
                bargeIn=True,
                actionOnEmptyResult=True
            )
            
            # Check if emergency contact already exists
            has_contact = bool(user_data.get('emergency_contacts'))
            
            if not has_contact:
                session['ask_emergency_contact'] = True
                speak(gather, f"Nice to meet you, {name_match}. Just to be safe, can you share an emergency contact number? You can say something like 'mom nine eight seven six five four three two one zero' or say 'skip' to continue.", user_data)
            else:
                speak(gather, f"Nice to meet you, {name_match}! How are you feeling today?", user_data)
            
            return Response(str(resp), mimetype='text/xml')
        else:
            gather = resp.gather(
                input='speech dtmf',
                action=public_url_for('process_speech'),
                method='POST',
                timeout=10,
                speechTimeout='auto',
                speechModel='phone_call',
                language=detect_lang_from_text(user_input),
                enhanced=True,
                bargeIn=True,
                actionOnEmptyResult=True
            )
            speak(gather, "Sorry, I didn't catch your name. Could you please tell me again?", user_data)
            return Response(str(resp), mimetype='text/xml')
    
    # Emergency contact capture
    if session.get('ask_emergency_contact'):
        session.pop('ask_emergency_contact', None)
        
        skip_words = ['skip', 'no', 'nahi', 'nope', 'later', 'baad mein']
        if any(w in ui_lower for w in skip_words):
            user_data['last_user_text'] = user_input
            gather = resp.gather(
                input='speech dtmf',
                action=public_url_for('process_speech'),
                method='POST',
                timeout=10,
                speechTimeout='auto',
                speechModel='phone_call',
                language=detect_lang_from_text(user_input),
                enhanced=True,
                bargeIn=True,
                actionOnEmptyResult=True
            )
            speak(gather, "No problem! How are you feeling today?", user_data)
            return Response(str(resp), mimetype='text/xml')
        
        # Parse contact from speech
        pairs = parse_contacts_from_text(user_input)
        
        if pairs:
            save_emergency_contacts(from_number, pairs)
            contact_names = [p[0] for p in pairs]
            
            user_data['last_user_text'] = user_input
            gather = resp.gather(
                input='speech dtmf',
                action=public_url_for('process_speech'),
                method='POST',
                timeout=10,
                speechTimeout='auto',
                speechModel='phone_call',
                language=detect_lang_from_text(user_input),
                enhanced=True,
                bargeIn=True,
                actionOnEmptyResult=True
            )
            
            if len(pairs) == 1:
                speak(gather, f"Got it! Saved {pairs[0][0]} as your emergency contact. How are you feeling today?", user_data)
            else:
                speak(gather, f"Got it! Saved your contacts. How are you feeling today?", user_data)
            
            return Response(str(resp), mimetype='text/xml')
        else:
            user_data['last_user_text'] = user_input
            gather = resp.gather(
                input='speech dtmf',
                action=public_url_for('process_speech'),
                method='POST',
                timeout=10,
                speechTimeout='auto',
                speechModel='phone_call',
                language=detect_lang_from_text(user_input),
                enhanced=True,
                bargeIn=True,
                actionOnEmptyResult=True
            )
            speak(gather, "I didn't catch a phone number. Say it digit by digit, for example: nine eight seven six five four three two one zero. Or say 'skip' to continue.", user_data)
            session['ask_emergency_contact'] = True
            return Response(str(resp), mimetype='text/xml')
    
    _turn_start = _time.time()

    # Detect emotion
    emotion = detect_emotion_from_text(user_input)
    user_data['last_emotion'] = emotion
    user_data['last_user_text'] = user_input

    # ── Push event to live dashboard ────────────────────────────────────────
    _active_calls.add(call_sid)
    _emit_dashboard_event('emotion', {
        'emotion':      emotion,
        'phone':        from_number,
        'name':         user_data.get('name', ''),
        'text':         user_input,
        'ts':           _time.time(),
        'is_emergency': is_emergency,
        'call_sid':     call_sid,
    })

    # ── Non-blocking Firebase writes ────────────────────────────────────────
    # Moving save_user_data + log_mood to background threads removes ~200-400 ms
    # of Firebase round-trip latency from the hot response path.
    def _bg_save():
        try:
            save_user_data(from_number, user_data.copy())
            log_mood(from_number, emotion)
        except Exception as e:
            print(f'[BG] Save error: {e}')
    threading.Thread(target=_bg_save, daemon=True).start()
    
    # Quick responses for common inputs — bilingual (EN + HI/Hinglish)
    name = user_data.get('name', '')
    turn_is_hindi = (detect_lang_from_text(user_input) == 'hi-IN')
    quick_responses_en = {
        'hi': f"Hey {name}! How are you?",
        'hello': f"Hello {name}! What's on your mind?",
        'hey': f"Hey {name}! I'm listening.",
        'thank you': f"You're welcome {name}! Always here for you.",
        'thanks': f"Anytime {name}!",
        'ok': f"Okay {name}, I'm here.",
        'okay': f"Okay {name}, go ahead."
    }
    quick_responses_hi = {
        'hi': f"Hey {name}! Kya haal hai?",
        'hello': f"Hello {name}! Kya baat hai?",
        'hey': f"Hey {name}! Main sun raha hoon.",
        'haan': f"Achha {name}, batao.",
        'ha': f"Ji {name}, main yahan hoon.",
        'theek hai': f"Theek hai {name}, aage batao.",
        'thank you': f"Koi baat nahi {name}! Hamesha yahan hoon.",
        'thanks': f"Anytime {name}!",
        'shukriya': f"Koi baat nahi {name}!",
        'ok': f"Okay {name}, main yahan hoon.",
        'okay': f"Okay {name}, aage batao."
    }
    quick_responses = quick_responses_hi if turn_is_hindi else quick_responses_en
    
    if ui_lower.strip() in quick_responses:
        gather = resp.gather(
            input='speech dtmf',
            action=public_url_for('process_speech'),
            method='POST',
            timeout=10,
            speechTimeout='auto',
            speechModel='phone_call',
            language=get_dynamic_language(user_data),
            bargeIn=True,
            actionOnEmptyResult=True
        )
        speak(gather, quick_responses[ui_lower.strip()], user_data)
        return Response(str(resp), mimetype='text/xml')
    
    # Generate GPT response
    _gpt_start = _time.time()
    response_text = generate_gpt_response(user_input, user_data, from_number)
    print(f'[Turn] total={_time.time()-_turn_start:.2f}s | gpt={_time.time()-_gpt_start:.2f}s')
    
    # Gather for next input
    gather = resp.gather(
        input='speech dtmf',
        action=public_url_for('process_speech'),
        method='POST',
        timeout=10,
        speechTimeout='auto',
        speechModel='phone_call',
        language='en-IN',
        bargeIn=True,
        actionOnEmptyResult=True
    )
    
    speak(gather, response_text, user_data)
    
    # Log assistant turn
    try:
        log = call_logs.get(call_sid)
        if log:
            log['turns'].append({'role': 'assistant', 'text': response_text, 't': datetime.now(timezone.utc).isoformat()})
    except Exception as e:
        print(f'[Log] Assistant error: {e}')
    
    return Response(str(resp), mimetype='text/xml')

# ── Media Streams WebSocket (optional) ─────────────────────────────────────
from realtime_bridge import RealtimeBridge

@sock.route('/media')
def media(ws):
    out_queue = deque()
    try:
        while True:
            msg = ws.receive()
            while out_queue:
                ws.send(out_queue.popleft())
            
            if msg is None:
                break
            
            try:
                data = json.loads(msg)
                event = data.get('event')
                
                if event == 'start':
                    start = data.get('start', {})
                    stream_sid = start.get('streamSid')
                    print(f'[Media] Stream started: {stream_sid}')
                    
                    def emit_to_twilio(ulaw_bytes: bytes):
                        payload = base64.b64encode(ulaw_bytes).decode('ascii')
                        out_msg = json.dumps({
                            'event': 'media',
                            'streamSid': stream_sid,
                            'media': {'payload': payload}
                        })
                        out_queue.append(out_msg)
                    
                    bridge = RealtimeBridge(
                        api_key=OPENAI_API_KEY,
                        model=os.getenv('OPENAI_REALTIME_MODEL', 'gpt-4o-realtime-preview'),
                        voice=os.getenv('OPENAI_REALTIME_VOICE', 'alloy'),
                        on_output_ulaw=emit_to_twilio
                    )
                    
                    _stream_state[stream_sid] = {'bridge': bridge}
                    
                elif event == 'media':
                    media_data = data.get('media', {})
                    payload_b64 = media_data.get('payload')
                    if not payload_b64:
                        continue
                    
                    stream_sid = data.get('streamSid')
                    state = _stream_state.get(stream_sid)
                    if not state:
                        continue
                    
                    bridge = state['bridge']
                    bridge.send_ulaw_frame_b64(payload_b64)
                    
                elif event == 'stop':
                    print('[Media] Stream stopped')
                    stream_sid = data.get('streamSid')
                    state = _stream_state.pop(stream_sid, None)
                    if state:
                        try:
                            state['bridge'].close()
                        except:
                            pass
                    break
                    
            except Exception as e:
                print(f'[Media] Parse error: {e}')
                
    except Exception as e:
        print(f'[Media] WS error: {e}')
    finally:
        for sid, state in list(_stream_state.items()):
            try:
                state['bridge'].close()
            except:
                pass
            _stream_state.pop(sid, None)

# ── API Endpoints ──────────────────────────────────────────────────────────
@app.route('/api/user/<phone>', methods=['GET'])
def get_user_profile(phone):
    """Get user profile with emotional insights."""
    try:
        user_data = get_user_data(phone)
        if not user_data:
            return {'success': False, 'error': 'User not found'}, 404
        
        # Calculate mood stats
        moods = user_data.get('moods', [])
        mood_stats = {'happy': 0, 'sad': 0, 'angry': 0, 'scared': 0, 'neutral': 0}
        for m in moods[-20:]:  # Last 20 entries
            emotion = m.get('emotion', 'neutral')
            mood_stats[emotion] = mood_stats.get(emotion, 0) + 1
        
        return {
            'success': True,
            'user': {
                'phone': phone,
                'name': user_data.get('name', 'Unknown'),
                'language': user_data.get('language', 'English'),
                'call_count': user_data.get('call_count', 0),
                'current_emotion': user_data.get('last_emotion', 'neutral'),
                'emergency_contacts': len(user_data.get('emergency_contacts', [])),
            },
            'emotional_insights': {
                'mood_distribution': mood_stats,
                'total_mood_logs': len(moods)
            }
        }, 200
    except Exception as e:
        print(f'[API] Profile error: {e}')
        return {'success': False, 'error': str(e)}, 500

@app.route('/api/sos', methods=['POST'])
def trigger_sos():
    """REST API endpoint for immediate SOS alert."""
    try:
        data = request.get_json() or {}
        phone = data.get('phone', '')
        message = data.get('message', 'SOS - I need help!')
        lat = data.get('latitude')
        lon = data.get('longitude')
        
        if not phone:
            return {'success': False, 'error': 'Phone number required'}, 400
        
        location_url = None
        if lat and lon:
            location_url = f"https://www.google.com/maps?q={lat},{lon}"
        
        user_data = get_or_init_user(phone)
        user_name = user_data.get('name', 'Unknown')
        
        if location_url:
            user_data['last_location'] = location_url
            save_user_data(phone, user_data)
        
        result = broadcast_emergency_to_contacts(phone, message, location_url)
        
        return {
            'success': result.get('success', False),
            'message': 'Emergency alert processed',
            'sent_to': result.get('sent', 0),
            'contacts': result.get('total_contacts', 0),
            'user_name': user_name,
            'location_shared': bool(location_url),
            'error': result.get('error', '')
        }, 200 if result.get('success') else 500
        
    except Exception as e:
        print(f'[API] SOS error: {e}')
        return {'success': False, 'error': str(e)}, 500

@app.route('/sos')
def sos_page():
    """Serve the SOS emergency alert demo page."""
    try:
        with open('sos_demo.html', 'r', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        return f"SOS page not found: {e}", 404

@app.route('/admin')
def admin():
    """Serve admin panel."""
    try:
        with open('admin.html', 'r') as f:
            return f.read()
    except:
        return "Admin panel not found", 404

@app.route('/dashboard')
def dashboard():
    """Serve the real-time emotion dashboard."""
    try:
        with open('dashboard.html', 'r', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        return f"Dashboard not found: {e}", 404

# ── SSE endpoint ───────────────────────────────────────────────────────────
import queue as _queue

@app.route('/api/dashboard/stream')
def dashboard_stream():
    """Server-Sent Events endpoint — streams live emotion events to the dashboard."""
    import queue as _q
    client_q = _q.Queue(maxsize=200)
    with _dashboard_lock:
        _dashboard_subscribers.append(client_q)

    def generate():
        # Send initial heartbeat so the browser marks connection as open
        yield f"event: heartbeat\ndata: {json.dumps({'active_calls': len(_active_calls)})}\n\n"
        try:
            while True:
                try:
                    msg = client_q.get(timeout=20)
                    yield msg
                except _q.Empty:
                    # Send keepalive heartbeat every 20 s
                    yield f"event: heartbeat\ndata: {json.dumps({'active_calls': len(_active_calls)})}\n\n"
        except GeneratorExit:
            pass
        finally:
            with _dashboard_lock:
                try:
                    _dashboard_subscribers.remove(client_q)
                except ValueError:
                    pass

    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Access-Control-Allow-Origin': '*',
        }
    )

@app.route('/admin/data')
def admin_data():
    """Return user data for admin panel."""
    rows = []
    try:
        if db:
            users = db.collection('users').limit(100).stream()
            for doc in users:
                d = doc.to_dict() or {}
                rows.append({
                    'phone': doc.id,
                    'name': d.get('name') or '',
                    'emotion': d.get('last_emotion') or 'neutral',
                    'call_count': int(d.get('call_count') or 0),
                    'contacts': len(d.get('emergency_contacts') or [])
                })
        else:
            for phone, d in list(user_memory.items())[:100]:
                rows.append({
                    'phone': phone,
                    'name': d.get('name') or '',
                    'emotion': d.get('last_emotion') or 'neutral',
                    'call_count': int(d.get('call_count') or 0),
                    'contacts': len(d.get('emergency_contacts') or [])
                })
    except Exception as e:
        print(f'[Admin] Data error: {e}')
    
    return json.dumps({'users': rows}), 200, {'Content-Type': 'application/json'}

# ── TwiML Endpoints ────────────────────────────────────────────────────────
@app.route('/alert_twiml')
def alert_twiml():
    service = request.args.get('service', '')
    caller = request.args.get('from', '')
    vr = VoiceResponse()
    vr.say(f"Emergency alert from ZenVoice. Caller {caller} needs {service} assistance.", 
           language='en-IN', voice='alice')
    return Response(str(vr), mimetype='text/xml')

@app.route('/cascade_ack', methods=['POST'])
def cascade_ack():
    cid = request.args.get('cid', '')
    idx = int(request.args.get('idx', '0') or 0)
    digits = request.values.get('Digits', '')
    sess = cascade_sessions.get(cid)
    vr = VoiceResponse()
    
    if not sess:
        vr.say('Thank you.', language='en-IN', voice='alice')
        return Response(str(vr), mimetype='text/xml')
    
    if digits == '1':
        sess['active'] = False
        sess['acknowledged'] = True
        vr.say('Acknowledged. Help is on the way.', language='en-IN', voice='alice')
        return Response(str(vr), mimetype='text/xml')
    
    vr.say('Thank you.', language='en-IN', voice='alice')
    return Response(str(vr), mimetype='text/xml')

@app.route('/cascade_status', methods=['POST'])
def cascade_status():
    cid = request.args.get('cid', '')
    idx = int(request.args.get('idx', '0') or 0)
    call_status = request.values.get('CallStatus', '')
    sess = cascade_sessions.get(cid)
    
    if not sess or not sess.get('active'):
        return ('', 204)
    
    # If call failed and wasn't acknowledged, move to next contact
    if call_status in ['failed', 'busy', 'no-answer', 'canceled'] and not sess.get('acknowledged'):
        # Implement next contact logic here
        pass
    
    return ('', 204)

# ── Call-end hook: remove call from active set ──────────────────────────────
@app.route('/voice_status', methods=['POST'])
def voice_status():
    """Twilio StatusCallback — removes ended calls from active set."""
    call_sid = request.values.get('CallSid', '')
    status = request.values.get('CallStatus', '')
    if call_sid and status in ('completed', 'failed', 'busy', 'no-answer', 'canceled'):
        _active_calls.discard(call_sid)
    return ('', 204)

@app.route('/api/test_emit', methods=['POST'])
def test_emit():
    """Fire a fake emotion event into the SSE stream to verify dashboard shows real numbers."""
    data = request.get_json(force=True, silent=True) or {}
    emotion   = data.get('emotion', 'neutral')
    phone     = data.get('phone', '+91 99999 00000')
    name      = data.get('name', 'Test User')
    text      = data.get('text', 'test message')
    emergency = bool(data.get('is_emergency', False))
    payload = {
        'emotion':      emotion,
        'phone':        phone,
        'name':         name,
        'text':         text,
        'ts':           _time.time(),
        'is_emergency': emergency,
        'call_sid':     'TEST-' + str(int(_time.time())),
    }
    _emit_dashboard_event('emotion', payload)
    return json.dumps({'ok': True, 'emitted': payload}), 200, {'Content-Type': 'application/json'}

if __name__ == '__main__':
    app.run(debug=False, host='127.0.0.1', port=5000, threaded=True)