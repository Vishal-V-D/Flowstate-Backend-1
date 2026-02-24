def generate_pcm_16k(text: str) -> bytes:
    """
    Generate 16kHz PCM audio bytes from text using pyttsx3.
    """
    import audioop, os, pyttsx3, uuid, wave
    
    engine = pyttsx3.init()
    engine.setProperty('rate', 175)
    
    filename = f"temp_{uuid.uuid4().hex}.wav"
    try:
        engine.save_to_file(text, filename)
        engine.runAndWait()
        
        with wave.open(filename, 'rb') as f:
            nchannels = f.getnchannels()
            sampwidth = f.getsampwidth()
            framerate = f.getframerate()
            raw_frames = f.readframes(f.getnframes())
            
        if framerate != 16000:
            raw_frames, _ = audioop.ratecv(raw_frames, sampwidth, nchannels, framerate, 16000, None)
            
        if nchannels == 2:
            raw_frames = audioop.tomono(raw_frames, sampwidth, 0.5, 0.5)
            
        return raw_frames
    finally:
        del engine
        if os.path.exists(filename):
            try:
                os.remove(filename)
            except:
                pass

if __name__ == "__main__":
    import time
    t0 = time.time()
    b = generate_pcm_16k("Hello world")
    t1 = time.time()
    print(f"Generated {len(b)} bytes of PCM data in {(t1-t0)*1000:.1f}ms")
