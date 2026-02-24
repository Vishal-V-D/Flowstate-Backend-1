import pyttsx3
import wave

engine = pyttsx3.init()
engine.save_to_file('Hello test', 'test.wav')
engine.runAndWait()

with wave.open('test.wav', 'rb') as f:
    print(f.getparams())
