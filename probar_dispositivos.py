import sounddevice as sd, numpy as np
for i in [1, 22, 26]:
    try:
        print(f"Dispositivo {i}: habla 3 segundos...")
        a = sd.rec(3*16000, samplerate=16000, channels=1, dtype="float32", device=i)
        sd.wait()
        print("   nivel:", round(float(np.sqrt(np.mean(a**2))), 4))
    except Exception as e:
        print("   falla:", e)