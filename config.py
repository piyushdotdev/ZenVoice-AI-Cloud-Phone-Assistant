import os
from dotenv import load_dotenv

# Load environment variables from .env file (optional)
try:
    load_dotenv()
except:
    pass  # Continue without .env file

# Twilio configuration
TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID', '')
TWILIO_AUTH_TOKEN = os.getenv('TWILIO_AUTH_TOKEN', '')
TWILIO_PHONE_NUMBER = os.getenv('TWILIO_PHONE_NUMBER', '')

# LLM configuration
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', '')
OPENAI_MODEL = os.getenv('OPENAI_MODEL', 'gpt-4o-mini')
OPENAI_BASE_URL = os.getenv('OPENAI_BASE_URL', '')

# Realtime/Media Streams toggle (disable for reliable PSTN speech recognition)
ENABLE_MEDIA_STREAMS = os.getenv('ENABLE_MEDIA_STREAMS', 'true').lower() in ['1','true','yes','on']

# Auto-detect OpenRouter key (sk-or-*) and set base URL if not provided
if not OPENAI_BASE_URL and OPENAI_API_KEY.startswith('sk-or-'):
    OPENAI_BASE_URL = 'https://openrouter.ai/api/v1'

GROQ_API_KEY = ''
LLM_PROVIDER = os.getenv('LLM_PROVIDER', 'openai')

# Firebase configuration
FIREBASE_CREDENTIALS_PATH = os.getenv('FIREBASE_CREDENTIALS_PATH', 'firebase_credentials.json')

# SMS provider: 'twilio' or 'msg91'
SMS_PROVIDER = os.getenv('SMS_PROVIDER', 'twilio').lower()
# Voice cascade (sequential calls with acknowledge)
ENABLE_VOICE_CASCADE = os.getenv('ENABLE_VOICE_CASCADE', 'true').lower() in ['1','true','yes','on']
# MSG91 configuration (DLT compliant)
MSG91_AUTHKEY = os.getenv('MSG91_AUTHKEY', '')
MSG91_SENDER_ID = os.getenv('MSG91_SENDER_ID', '')  # 6-letter DLT sender id
MSG91_FLOW_ID_EMERGENCY = os.getenv('MSG91_FLOW_ID_EMERGENCY', '')  # Flow ID for emergency template
MSG91_ENTITY_ID = os.getenv('MSG91_ENTITY_ID', '')  # Optional PE/Entity ID if required

# WhatsApp (Twilio)
ENABLE_WHATSAPP = os.getenv('ENABLE_WHATSAPP', 'false').lower() in ['1','true','yes','on']
WHATSAPP_FROM = os.getenv('WHATSAPP_FROM', '')  # e.g., whatsapp:+14155238886 (sandbox) or your WABA number

# Emergency contact number (for SMS alerts)
EMERGENCY_CONTACT_NUMBER = os.getenv('EMERGENCY_CONTACT_NUMBER', '')
EMERGENCY_POLICE_NUMBER = os.getenv('EMERGENCY_POLICE_NUMBER', '')  # e.g., +91100 for India police gateway
EMERGENCY_AMBULANCE_NUMBER = os.getenv('EMERGENCY_AMBULANCE_NUMBER', '')  # e.g., +91102 for India ambulance gateway
