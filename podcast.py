#!/usr/bin/env python3
"""Turn podcast.json's spoken script into site/brief.mp3, the page's "Listen" recording.

podcast.json is written by the daily routine after research, like research.json:
    {"as_of": "YYYY-MM-DD", "script": "Good morning. ...\n\nParagraph two ..."}
Blank lines separate paragraphs; each gets a short pause. The script is spoken as written, so it
should already read like speech (see README).

Speech comes from Piper, a local neural TTS. The first run in a fresh container installs
piper-tts with pip and downloads the voice from Hugging Face (about 120 MB); ffmpeg encodes MP3.
On success it records the script's hash and the duration in podcast.json, which is how
refresh.py knows the recording matches today's script and shows the player.

    python3 podcast.py
"""
import array, datetime, hashlib, json, os, subprocess, sys, time, wave

os.environ['TZ'] = 'Europe/Copenhagen'
time.tzset()

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, 'podcast.json')
OUT = os.path.join(HERE, 'site', 'brief.mp3')
VOICE = 'en_US-ryan-high'
VOICES = os.path.expanduser('~/.cache/morningbrief/voices')
PAUSE = 0.7  # seconds between paragraphs
SPEED = 1.05  # Piper length_scale; above 1 is slower


def piper():
    try:
        import piper
    except ImportError:
        print('installing piper-tts')
        subprocess.run([sys.executable, '-m', 'pip', 'install', '--quiet', 'piper-tts'], check=True)
        import piper
    return piper


def spoken(text):
    """Small safety net for symbols that TTS reads badly; the script should avoid them anyway."""
    for a, b in (('¢', ' cents'), ('→', ' to '), ('·', ','), ('–', ' to '), ('°C', ' degrees'), ('%', ' percent')):
        text = text.replace(a, b)
    return text


with open(SCRIPT) as fh:
    pod = json.load(fh)
today = f'{datetime.date.today():%Y-%m-%d}'
if pod.get('as_of') != today:
    sys.exit(f"podcast.json is for {pod.get('as_of')}, not {today}; write today's script first")
paras = [p.strip() for p in pod['script'].split('\n\n') if p.strip()]
if not paras:
    sys.exit('podcast.json has an empty script')

pp = piper()
os.makedirs(VOICES, exist_ok=True)
model = os.path.join(VOICES, f'{VOICE}.onnx')
if not os.path.exists(model):
    print(f'downloading voice {VOICE}')
    subprocess.run([sys.executable, '-m', 'piper.download_voices', '--download-dir', VOICES, VOICE], check=True)
voice = pp.PiperVoice.load(model)
cfg = pp.SynthesisConfig(length_scale=SPEED)
rate = voice.config.sample_rate

pcm = array.array('h')
for i, p in enumerate(paras):
    if i:
        pcm.extend([0] * int(rate * PAUSE))
    for chunk in voice.synthesize(spoken(p), syn_config=cfg):
        pcm.frombytes(chunk.audio_int16_bytes)

os.makedirs(os.path.dirname(OUT), exist_ok=True)
wav = OUT[:-4] + '.wav'
with wave.open(wav, 'wb') as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
    w.writeframes(pcm.tobytes())
subprocess.run(['ffmpeg', '-loglevel', 'error', '-y', '-i', wav, '-ac', '1', '-b:a', '64k', OUT], check=True)
os.remove(wav)

pod['audio'] = {'sha1': hashlib.sha1(pod['script'].encode()).hexdigest(),
                'seconds': len(pcm) // rate, 'voice': VOICE}
with open(SCRIPT, 'w') as fh:
    json.dump(pod, fh, indent=2, ensure_ascii=False)
print(f"wrote {OUT}: {pod['audio']['seconds'] // 60}:{pod['audio']['seconds'] % 60:02d}, "
      f"{len(pod['script'].split())} words, {os.path.getsize(OUT) / 1e6:.1f} MB")
