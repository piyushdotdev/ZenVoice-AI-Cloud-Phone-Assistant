
import os
import glob
import numpy as np
import joblib
import argparse
from sklearn.model_selection import train_test_split
from sklearn.neural_network import MLPClassifier
from sklearn.metrics import accuracy_score, classification_report
from utils.audio_features import extract_features, create_synthetic_audio

# Define emotions to recognize
EMOTIONS = {
    'neutral': 0,
    'calm': 1,
    'happy': 2,
    'sad': 3,
    'angry': 4,
    'fearful': 5,
    'disgust': 6,
    'surprised': 7
}
# Reverse mapping for prediction
EMOTION_LABELS = {v: k for k, v in EMOTIONS.items()}

DATA_DIR = os.path.join("data", "raw")
MODEL_DIR = "models"
MODEL_PATH = os.path.join(MODEL_DIR, "emotion_model.pkl")

def load_data(data_dir=DATA_DIR):
    """
    Loads data from directories. 
    Expected structure: data/raw/happy/*.wav, data/raw/sad/*.wav, etc.
    """
    x, y = [], []
    
    # Check if data directory exists
    if not os.path.exists(data_dir):
        print(f"[!] Data directory {data_dir} not found.")
        return np.array(x), np.array(y)

    print(f"[*] Loading data from {data_dir}...")
    
    for emotion, label_id in EMOTIONS.items():
        # Look for a folder named after the emotion (e.g., data/raw/happy)
        emotion_dir = os.path.join(data_dir, emotion)
        if not os.path.exists(emotion_dir):
            continue
            
        files = glob.glob(os.path.join(emotion_dir, "*.wav"))
        print(f"    - Found {len(files)} files for emotion: '{emotion}'")
        
        for file in files:
            feature = extract_features(file)
            if feature is not None:
                x.append(feature)
                y.append(label_id)
                
    return np.array(x), np.array(y)

def train(demo_mode=False):
    # Ensure model directory exists
    if not os.path.exists(MODEL_DIR):
        os.makedirs(MODEL_DIR)

    # 1. Load Data
    if demo_mode:
        print("[*] DEMO MODE: Generating synthetic data...")
        x, y = generate_demo_data()
    else:
        x, y = load_data()

    if len(x) == 0:
        print("[!] No data found. Please add .wav files to 'data/raw/{emotion}/' or run with --demo")
        return

    # 2. Split Data
    print(f"[*] Data loaded: {len(x)} samples.")
    # If we have very few samples, don't split, just train (overfit) for demo purposes
    if len(x) < 10:
        print("[!] Warning: Very few samples. Skipping test split.")
        x_train, x_test, y_train, y_test = x, x, y, y
    else:
        x_train, x_test, y_train, y_test = train_test_split(x, y, test_size=0.2, random_state=42)

    # 3. Train Model
    # MLPClassifier is a good general purpose neural network for this tabular-like feature data
    print("[*] Training MLP Classifier...")
    model = MLPClassifier(alpha=0.01, batch_size=32, epsilon=1e-08, hidden_layer_sizes=(300,), learning_rate='adaptive', max_iter=500)
    model.fit(x_train, y_train)

    # 4. Evaluate
    y_pred = model.predict(x_test)
    accuracy = accuracy_score(y_test, y_pred)
    print(f"[*] Model Accuracy: {accuracy*100:.2f}%")
    
    # 5. Save Model
    joblib.dump(model, MODEL_PATH)
    print(f"[*] Model saved to {MODEL_PATH}")

def generate_demo_data():
    """Generates fake data for demonstration so the script runs out of the box."""
    x, y = [], []
    demo_dir = os.path.join("data", "demo_generated")
    if not os.path.exists(demo_dir):
        os.makedirs(demo_dir)
        
    print(f"    - Generating temporary wav files in {demo_dir}")
    
    # Generate 5 samples for each emotion
    for emotion, label_id in EMOTIONS.items():
        for i in range(3):
            fname = os.path.join(demo_dir, f"{emotion}_{i}.wav")
            # Create a file
            create_synthetic_audio(fname, duration=1.0)
            # Extract
            feat = extract_features(fname)
            x.append(feat)
            y.append(label_id)
            
    return np.array(x), np.array(y)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Train Emotion Detection Model")
    parser.add_argument("--demo", action="store_true", help="Run in demo mode with synthetic data")
    args = parser.parse_args()
    
    train(demo_mode=args.demo)
