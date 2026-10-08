#!/usr/bin/env python3
"""Turn podcast.json's spoken script into site/brief.mp3, the page's "Listen" recording.

podcast.json is written by the daily routine after research, like research.json:
    {"as_of": "YYYY-MM-DD", "script": "Good morning. ...\n\nParagraph two ..."}
Blank lines separate paragraphs. The script is spoken as written, so it should already read like
speech (see README). It may carry ElevenLabs v3 audio tags such as [sighs]; Piper drops them.

Voice: ElevenLabs (model eleven_v3) with "Morning Brief Majordomo", an original voice designed from
a text description: a fussy, plummy English butler. The first run on an account designs and saves it;
later runs find it by name. Auth comes from ELEVENLABS_API_KEY, or from an environment credential
that injects the header for api.elevenlabs.io (as with Vercel). Without either, or if ElevenLabs
fails, it falls back to Piper, a local neural TTS that pip installs on first use (voice ~120 MB).
On success it records the script's hash and the duration in podcast.json, which is how refresh.py
knows the recording matches today's script and shows the player.

    python3 podcast.py            # ElevenLabs, falling back to Piper
    python3 podcast.py --piper    # Piper only
"""
import array, datetime, hashlib, json, os, re, subprocess, sys, tempfile, time, urllib.parse, wave

os.environ['TZ'] = 'Europe/Copenhagen'
time.tzset()

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, 'podcast.json')
OUT = os.path.join(HERE, 'site', 'brief.mp3')

EL_API = 'https://api.elevenlabs.io'
EL_MODEL = 'eleven_v3'
EL_LIMIT = 4500  # characters per request; eleven_v3 takes 5,000
EL_VOICE_NAME = 'Morning Brief Majordomo'
EL_VOICE_DESC = ('A fussy, fastidious English majordomo in his fifties with a crisp, plummy received-pronunciation '
                 'accent. Quick, clipped, precise delivery in a light, slightly reedy tenor; pompous and officious '
                 'but loyal, with dry wit and a hint of long-suffering exasperation. Clean studio recording, close mic.')
EL_VOICE_SAMPLE = ('Good morning, sir. I have the overnight figures here, and I am pleased to report that, once again, '
                   'almost nothing has happened. The markets twitched, the weather sulked, and the coalition in Berlin '
                   'is still, somehow, a coalition. Shall I go on? I shall go on.')

PIPER_VOICE = 'en_US-ryan-high'
PIPER_DIR = os.path.expanduser('~/.cache/morningbrief/voices')
PIPER_PAUSE = 0.7  # seconds between paragraphs
PIPER_SPEED = 1.05  # length_scale; above 1 is slower


# ---- ElevenLabs ----------------------------------------------------------
class ElevenError(Exception):
    pass


def el(method, path, data=None, raw=False):
    """Call ElevenLabs through curl, like deploy.py, so a proxy-injected credential applies."""
    cmd = ['curl', '-sS', '-X', method, f'{EL_API}{path}', '-H', 'Content-Type: application/json',
           '-w', '\n%{http_code}', '--max-time', '300']
    if os.environ.get('ELEVENLABS_API_KEY'):
        cmd += ['-H', f"xi-api-key: {os.environ['ELEVENLABS_API_KEY']}"]
    if data is not None:
        cmd += ['--data-binary', '@-']
    out = subprocess.run(cmd, input=json.dumps(data).encode() if data is not None else None, capture_output=True)
    if out.returncode:
        raise ElevenError(f'curl failed: {out.stderr.decode().strip()}')
    body, _, code = out.stdout.rpartition(b'\n')
    if code != b'200':
        try:
            detail = json.loads(body).get('detail', body.decode()[:300])
            detail = detail.get('message', detail) if isinstance(detail, dict) else detail
        except ValueError:
            detail = body.decode(errors='replace')[:300]
        raise ElevenError(f'HTTP {code.decode()} on {path.split("?")[0]}: {detail}')
    return body if raw else json.loads(body)


def el_voice():
    if os.environ.get('ELEVENLABS_VOICE_ID'):
        return os.environ['ELEVENLABS_VOICE_ID']
    found = el('GET', f'/v2/voices?page_size=100&search={urllib.parse.quote(EL_VOICE_NAME)}')
    for v in found.get('voices', []):
        if v.get('name') == EL_VOICE_NAME:
            return v['voice_id']
    print(f'designing ElevenLabs voice "{EL_VOICE_NAME}"')
    d = el('POST', '/v1/text-to-voice/design', {'voice_description': EL_VOICE_DESC, 'model_id': 'eleven_ttv_v3',
                                                'text': EL_VOICE_SAMPLE, 'seed': 1894})
    v = el('POST', '/v1/text-to-voice', {'voice_name': EL_VOICE_NAME, 'voice_description': EL_VOICE_DESC,
                                         'generated_voice_id': d['previews'][0]['generated_voice_id']})
    return v['voice_id']


def chunks(paras, limit):
    out, cur = [], ''
    for p in paras:
        if cur and len(cur) + 2 + len(p) > limit:
            out.append(cur); cur = p
        else:
            cur = f'{cur}\n\n{p}' if cur else p
    return out + [cur]


def eleven(paras, tmp):
    voice = el_voice()
    parts = []
    for i, text in enumerate(chunks(paras, EL_LIMIT)):
        mp3 = el('POST', f'/v1/text-to-speech/{voice}?output_format=mp3_44100_96',
                 {'text': text, 'model_id': EL_MODEL, 'voice_settings': {'stability': 0.5}}, raw=True)
        parts.append(os.path.join(tmp, f'part{i}.mp3'))
        with open(parts[-1], 'wb') as fh:
            fh.write(mp3)
    with open(os.path.join(tmp, 'parts.txt'), 'w') as fh:
        fh.writelines(f"file '{p}'\n" for p in parts)
    subprocess.run(['ffmpeg', '-loglevel', 'error', '-y', '-f', 'concat', '-safe', '0',
                    '-i', os.path.join(tmp, 'parts.txt'), '-c', 'copy', OUT], check=True)
    return f'elevenlabs {EL_MODEL}'


# ---- Piper ---------------------------------------------------------------
def spoken(text):
    """Drop audio tags and symbols that Piper reads badly; the script should avoid symbols anyway."""
    text = re.sub(r'\[[^\]]*\]\s*', '', text)
    for a, b in (('¢', ' cents'), ('→', ' to '), ('·', ','), ('–', ' to '), ('°C', ' degrees'), ('%', ' percent')):
        text = text.replace(a, b)
    return text


def piper(paras, tmp):
    try:
        import piper as pp
    except ImportError:
        print('installing piper-tts')
        subprocess.run([sys.executable, '-m', 'pip', 'install', '--quiet', 'piper-tts'], check=True)
        import piper as pp
    os.makedirs(PIPER_DIR, exist_ok=True)
    model = os.path.join(PIPER_DIR, f'{PIPER_VOICE}.onnx')
    if not os.path.exists(model):
        print(f'downloading voice {PIPER_VOICE}')
        subprocess.run([sys.executable, '-m', 'piper.download_voices', '--download-dir', PIPER_DIR, PIPER_VOICE],
                       check=True)
    voice = pp.PiperVoice.load(model)
    cfg = pp.SynthesisConfig(length_scale=PIPER_SPEED)
    rate = voice.config.sample_rate
    pcm = array.array('h')
    for i, p in enumerate(paras):
        if i:
            pcm.extend([0] * int(rate * PIPER_PAUSE))
        for chunk in voice.synthesize(spoken(p), syn_config=cfg):
            pcm.frombytes(chunk.audio_int16_bytes)
    wav = os.path.join(tmp, 'brief.wav')
    with wave.open(wav, 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    subprocess.run(['ffmpeg', '-loglevel', 'error', '-y', '-i', wav, '-ac', '1', '-b:a', '64k', OUT], check=True)
    return f'piper {PIPER_VOICE}'


# ---- main ----------------------------------------------------------------
with open(SCRIPT) as fh:
    pod = json.load(fh)
today = f'{datetime.date.today():%Y-%m-%d}'
if pod.get('as_of') != today:
    sys.exit(f"podcast.json is for {pod.get('as_of')}, not {today}; write today's script first")
paras = [p.strip() for p in pod['script'].split('\n\n') if p.strip()]
if not paras:
    sys.exit('podcast.json has an empty script')

os.makedirs(os.path.dirname(OUT), exist_ok=True)
with tempfile.TemporaryDirectory() as tmp:
    engine = None
    if '--piper' not in sys.argv:
        try:
            engine = eleven(paras, tmp)
        except (ElevenError, KeyError, subprocess.CalledProcessError) as e:
            print(f'elevenlabs failed, falling back to Piper: {e}')
    if engine is None:
        engine = piper(paras, tmp)

secs = int(float(subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', OUT],
                                capture_output=True, text=True, check=True).stdout))
pod['audio'] = {'sha1': hashlib.sha1(pod['script'].encode()).hexdigest(), 'seconds': secs, 'voice': engine}
with open(SCRIPT, 'w') as fh:
    json.dump(pod, fh, indent=2, ensure_ascii=False)
print(f"wrote {OUT} with {engine}: {secs // 60}:{secs % 60:02d}, "
      f"{len(pod['script'].split())} words, {os.path.getsize(OUT) / 1e6:.1f} MB")
