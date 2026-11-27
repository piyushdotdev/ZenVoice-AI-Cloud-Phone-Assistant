import requests
import os
import numpy as np

# Audio processing imports
from utils.audio_features import extract_features
import joblib
import numpy as np

# Path to the trained model
MODEL_PATH = os.path.join("models", "emotion_model.pkl")
_LOADED_MODEL = None

def detect_emotion_from_audio(audio_url):
    """Download audio and detect emotion using trained model."""
    global _LOADED_MODEL
    try:
        # Download audio to a temp file
        import tempfile
        import urllib.request
        
        # Create temp file
        fd, temp_path = tempfile.mkstemp(suffix='.wav')
        os.close(fd)
        
        # Download
        # If it's a local path (starts with / or C:), just use it
        if os.path.exists(audio_url):
            temp_path = audio_url
        else:
            urllib.request.urlretrieve(audio_url, temp_path)
        
        # Load model if not loaded
        if _LOADED_MODEL is None:
            if os.path.exists(MODEL_PATH):
                print(f"[*] Loading emotion model from {MODEL_PATH}")
                _LOADED_MODEL = joblib.load(MODEL_PATH)
            else:
                print(f"[!] Model not found at {MODEL_PATH}. Using fallback.")
                return 'neutral'

        # Extract features
        features = extract_features(temp_path)
        if features is None:
            return 'neutral'
            
        # Predict
        # Reshape for single sample
        features = features.reshape(1, -1)
        
        # Get prediction
        # The model was trained with numeric labels 0-7. 
        # We need the reverse mapping.
        # Ideally this should be shared, but for now we hardcode the standard mapping used in train_model.py
        EMOTION_LABELS = {
            0: 'neutral', 1: 'calm', 2: 'happy', 3: 'sad', 
            4: 'angry', 5: 'fearful', 6: 'disgust', 7: 'surprised'
        }
        
        prediction_id = _LOADED_MODEL.predict(features)[0]
        emotion = EMOTION_LABELS.get(prediction_id, 'neutral')
        
        print(f"[*] Detected Emotion: {emotion}")
        
        # Cleanup if we downloaded
        if temp_path != audio_url and os.path.exists(temp_path):
            os.remove(temp_path)
            
        return emotion

    except Exception as e:
        print(f"Emotion detection error: {e}")
        return 'neutral'

def detect_emotion_from_text(text):
    """Hybrid emotion detection: quick keyword rules; fallback to 'neutral'.
    For better accuracy, set ENABLE_LLM_EMOTION=true to use the LLM.
    """
    text_lower = (text or '').lower()
    if any(k in text_lower for k in ['sad','depressed','down','unhappy','cry','lonely','upset']):
        return 'sad'
    if any(k in text_lower for k in ['angry','mad','furious','irritated','annoyed','rage']):
        return 'angry'
    if any(k in text_lower for k in ['happy','excited','joy','glad','good','great']):
        return 'happy'
    if any(k in text_lower for k in ['scared','afraid','anxious','nervous','worried','panic']):
        return 'scared'

    # Optional: LLM-based classification for nuance
    if os.getenv('ENABLE_LLM_EMOTION', 'false').lower() in ['1','true','yes','on']:
        try:
            from openai import OpenAI
            from config import OPENAI_API_KEY, OPENAI_BASE_URL
            client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL or None)
            prompt = (
                "Classify the dominant emotion in this text as one of: happy, sad, angry, scared, neutral. "
                "Answer with just the single word. Text: " + (text or '')
            )
            resp = client.chat.completions.create(
                model=os.getenv('OPENAI_MODEL','gpt-4o-mini'),
                messages=[{"role":"user","content":prompt}],
                max_tokens=1,
                temperature=0
            )
            label = (resp.choices[0].message.content or '').strip().lower()
            if label in ['happy','sad','angry','scared','neutral']:
                return label
        except Exception:
            pass
    return 'neutral'
