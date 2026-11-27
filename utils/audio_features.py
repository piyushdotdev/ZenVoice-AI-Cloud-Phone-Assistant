
import librosa
import numpy as np
import os
import soundfile as sf

def extract_features(file_path):
    """
    Extracts features from an audio file.
    Features: MFCC, Chroma, Mel Spectrogram.
    Returns: A 1D numpy array of the mean of these features.
    """
    try:
        # Load audio file
        # sr=None preserves the native sampling rate, but we can fix it to 22050 for consistency
        audio, sample_rate = librosa.load(file_path, sr=22050) # 22kHz is standard for speech

        result = np.array([])

        # 1. MFCC (Mel-frequency cepstral coefficients) - Good for speech/timbre
        mfccs = np.mean(librosa.feature.mfcc(y=audio, sr=sample_rate, n_mfcc=40).T, axis=0)
        result = np.hstack((result, mfccs))

        # 2. Chroma (Pitch class profile) - Good for music/tone, sometimes useful for emotion
        stft = np.abs(librosa.stft(audio))
        chroma = np.mean(librosa.feature.chroma_stft(S=stft, sr=sample_rate).T, axis=0)
        result = np.hstack((result, chroma))

        # 3. Mel Spectrogram - Approximates human hearing
        mel = np.mean(librosa.feature.melspectrogram(y=audio, sr=sample_rate).T, axis=0)
        result = np.hstack((result, mel))

        return result
    except Exception as e:
        print(f"Error extracting features from {file_path}: {e}")
        return None

def create_synthetic_audio(filename, duration=2.0):
    """
    Creates a synthetic audio file for testing purposes if no real data exists.
    """
    sr = 22050
    t = np.linspace(0, duration, int(sr * duration))
    # Generate a random frequency sine wave with some noise
    freq = np.random.uniform(200, 800)
    audio = 0.5 * np.sin(2 * np.pi * freq * t)
    audio += 0.05 * np.random.normal(0, 1, len(t)) # Add white noise
    
    sf.write(filename, audio, sr)
    return filename
