#!/usr/bin/env python3
import sys, subprocess, json, threading, time, queue, os, math, struct, wave

def need(pkg):
    try: __import__(pkg)
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", pkg])

for p in ("pyaudio", "simpleaudio", "flask", "pyttsx3"):
    need(p)

import pyaudio
import simpleaudio as sa
from flask import Flask, render_template_string, request, jsonify
from werkzeug.serving import make_server
import socket, pyttsx3
try:
    import winsound
    HAVE_WINSOUND = True
except ImportError:
    HAVE_WINSOUND = False

def linspace(start, stop, num):
    """Simple replacement for np.linspace"""
    if num <= 1:
        return [start]
    step = (stop - start) / (num - 1)
    return [start + step * i for i in range(num)]

def clip(value, min_val, max_val):
    """Simple replacement for np.clip"""
    if hasattr(value, '__iter__'):
        return [max(min_val, min(max_val, v)) for v in value]
    return max(min_val, min(max_val, value))

def mean(values):
    """Simple replacement for np.mean"""
    return sum(values) / len(values)

def sign(x):
    """Simple replacement for np.sign"""
    if hasattr(x, '__iter__'):
        return [1 if v > 0 else -1 if v < 0 else 0 for v in x]
    return 1 if x > 0 else -1 if x < 0 else 0

def get_config_path():
    """Gets the path to the config file in a standard OS-specific location."""
    if sys.platform == "win32":
        app_data_dir = os.path.join(os.getenv("APPDATA"), "NoiseAlert")
    else: # macOS and Linux
        app_data_dir = os.path.join(os.path.expanduser("~"), ".config", "NoiseAlert")
    
    os.makedirs(app_data_dir, exist_ok=True)
    return os.path.join(app_data_dir, "config.json")

CFG_FILE = get_config_path()

def load_cfg():
    if os.path.exists(CFG_FILE):
        try:
            with open(CFG_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            print(f"Warning: Could not load {CFG_FILE}: {e}. Using default config.")
            return {}
    return {}

def save_cfg(cfg):
    try:
        with open(CFG_FILE, "w") as f:
            json.dump(cfg, f, indent=4)
    except IOError as e:
        print(f"Warning: Could not save config to {CFG_FILE}: {e}")

def list_audio_devices(kind):
    pa = pyaudio.PyAudio()
    audio_devices = []
    try:
        device_count = pa.get_device_count()
        for i in range(device_count):
            try:
                device_info = pa.get_device_info_by_index(i)
                if kind == "input" and device_info['maxInputChannels'] > 0:
                    host_api_info = pa.get_host_api_info_by_index(device_info['hostApi'])
                    device_name = f"{device_info['name']} ({host_api_info['name']})"
                    audio_devices.append((i, device_name))
                elif kind == "output" and device_info['maxOutputChannels'] > 0:
                    host_api_info = pa.get_host_api_info_by_index(device_info['hostApi'])
                    device_name = f"{device_info['name']} ({host_api_info['name']})"
                    audio_devices.append((i, device_name))
            except Exception:
                continue
    finally:
        pa.terminate()
    return audio_devices

def list_input_devices():
    return list_audio_devices("input")

def list_output_devices():
    return list_audio_devices("output")

def get_default_device(kind):
    """Get default input or output device index"""
    pa = pyaudio.PyAudio()
    try:
        if kind == "input":
            return pa.get_default_input_device_info()['index']
        else:
            return pa.get_default_output_device_info()['index']
    except Exception:
        return None
    finally:
        pa.terminate()

device_map_name_to_idx = {}
device_map_idx_to_name = {}

class SharedState:
    def __init__(self, cfg):
        self.threshold = cfg.get("thresh", -30.0)
        self.is_monitoring = False
        self.current_db = -100.0
        self.web_server_enabled = False
        self.web_server_port = cfg.get("web_port", 8080)
        self.web_start_requested = False
        self.web_stop_requested = False
        self.current_device = 0
        self.tts_queue = queue.Queue()
        self.lock = threading.Lock()
        
        try:
            self.tts_engine = pyttsx3.init()
            self.tts_engine.setProperty('rate', 150)
            self.tts_engine.setProperty('volume', 0.9)
        except Exception as e:
            print(f"TTS initialization failed: {e}")
            self.tts_engine = None
    
    def update_threshold(self, value):
        with self.lock:
            self.threshold = value
    
    def update_monitoring(self, status):
        with self.lock:
            self.is_monitoring = status
    
    def update_current_db(self, value):
        with self.lock:
            self.current_db = value
    
    def add_tts_message(self, message):
        """Add a message to the TTS queue"""
        if self.tts_engine and message.strip():
            self.tts_queue.put(message)
    
    def get_state(self):
        with self.lock:
            return {
                'threshold': self.threshold,
                'is_monitoring': self.is_monitoring,
                'current_db': self.current_db
            }
    
    def check_web_requests(self):
        with self.lock:
            start_req = self.web_start_requested
            stop_req = self.web_stop_requested
            self.web_start_requested = False
            self.web_stop_requested = False
            return start_req, stop_req

cfg = load_cfg()
shared_state = SharedState(cfg)

class TTSWorker(threading.Thread):
    def __init__(self, shared_state):
        super().__init__(daemon=True)
        self.shared_state = shared_state
        self.stop_event = threading.Event()
    
    def run(self):
        while not self.stop_event.is_set():
            try:
                message = self.shared_state.tts_queue.get(timeout=1.0)
                if self.shared_state.tts_engine:
                    print(f"Speaking: {message}")
                    self.shared_state.tts_engine.say(message)
                    self.shared_state.tts_engine.runAndWait()
                self.shared_state.tts_queue.task_done()
            except queue.Empty:
                continue
            except Exception as e:
                print(f"TTS error: {e}")
    
    def stop(self):
        self.stop_event.set()

tts_worker = TTSWorker(shared_state)
tts_worker.start()

def gen_wave(shape, freq, ms, volume):
    fs = 44100
    t = linspace(0, ms/1000, int(fs*ms/1000))
    if shape == "Sine":
        wave = [math.sin(2*math.pi*freq*ti) for ti in t]
    elif shape == "Square":
        wave = sign([math.sin(2*math.pi*freq*ti) for ti in t])
    else:
        wave = [2*(ti*freq - math.floor(0.5 + ti*freq)) for ti in t]
    audio = [int((w * (volume/100) * 32767)) for w in wave]
    # Convert to bytes for PyAudio
    audio_bytes = struct.pack('<' + 'h' * len(audio), *audio)
    return audio_bytes, fs

def play_tone(shape, freq, ms, vol, device=None):
    try:
        audio_bytes, fs = gen_wave(shape, freq, ms, vol)
        
        pa = pyaudio.PyAudio()
        try:
            stream = pa.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=fs,
                output=True,
                output_device_index=device
            )
            stream.write(audio_bytes)
            stream.stop_stream()
            stream.close()
        finally:
            pa.terminate()
    except Exception as e:
        print(f"Error playing tone: {e}")

class Monitor(threading.Thread):
    def __init__(self, q_cmd, q_ui):
        super().__init__(daemon=True)
        self.q_cmd = q_cmd
        self.q_ui  = q_ui
        self.stop_evt = threading.Event()

    @staticmethod
    def rms_to_db(rms): return 20*math.log10(rms+1e-10)

    def run(self):
        settings = {}
        while not self.stop_evt.is_set():
            try:
                while 1: settings = self.q_cmd.get_nowait()
            except queue.Empty: pass

            if not settings: time.sleep(0.1); continue

            frames = int(settings["chunk"]*settings["fs"])
            try:
                pa = pyaudio.PyAudio()
                try:
                    stream = pa.open(
                        format=pyaudio.paFloat32,
                        channels=1,
                        rate=settings["fs"],
                        input=True,
                        input_device_index=settings["device"],
                        frames_per_buffer=frames
                    )
                    audio_data = stream.read(frames)
                    stream.stop_stream()
                    stream.close()
                    
                    # Convert bytes to float values
                    rec_flat = list(struct.unpack('<' + 'f' * frames, audio_data))
                finally:
                    pa.terminate()
            except Exception as e:
                self.q_ui.put(("ERR", str(e)))
                self.stop_evt.set(); break

            rec_clipped = clip(rec_flat, -1.0, 1.0)
            rms = math.sqrt(mean([r*r for r in rec_clipped]))
            db  = self.rms_to_db(rms)
            self.q_ui.put(("DB", db))

            if db > settings["thresh"]:
                play_tone(settings["shape"], settings["freq"],
                          settings["dur"], settings["vol"],
                          settings["output_device"])
            time.sleep(settings["delay"])

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

app = Flask(__name__)

WEB_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>🔊 Noise Alert</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no">
    <meta name="theme-color" content="#1a1a1a">
    <style>
        :root {
            --primary: #2196F3;
            --primary-dark: #1565C0;
            --success: #4CAF50;
            --warning: #FF9800;
            --error: #f44336;
            --bg-primary: #1a1a1a;
            --bg-secondary: #2d2d2d;
            --bg-card: rgba(40, 40, 40, 0.95);
            --text-primary: #ffffff;
            --text-secondary: #e0e0e0;
            --text-muted: rgba(255, 255, 255, 0.7);
            --border: rgba(255, 255, 255, 0.1);
            --shadow-sm: 0 2px 8px rgba(0, 0, 0, 0.1);
            --shadow-md: 0 4px 16px rgba(0, 0, 0, 0.2);
            --shadow-lg: 0 8px 32px rgba(0, 0, 0, 0.3);
            --radius-sm: 8px;
            --radius-md: 12px;
            --radius-lg: 16px;
            --spacing-xs: 4px;
            --spacing-sm: 8px;
            --spacing-md: 16px;
            --spacing-lg: 24px;
            --spacing-xl: 32px;
        }
        
        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }
        
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
            background: linear-gradient(135deg, var(--bg-primary) 0%, var(--bg-secondary) 100%);
            margin: 0;
            padding: var(--spacing-md);
            min-height: 100vh;
            color: var(--text-primary);
            line-height: 1.6;
            font-size: 16px;
            -webkit-font-smoothing: antialiased;
            -moz-osx-font-smoothing: grayscale;
            overflow-x: hidden;
        }
        
        .container {
            max-width: 520px;
            margin: 0 auto;
            padding: 0;
            animation: fadeInUp 0.6s ease-out;
        }
        
        @keyframes fadeInUp {
            from {
                opacity: 0;
                transform: translateY(20px);
            }
            to {
                opacity: 1;
                transform: translateY(0);
            }
        }
        
        .card {
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: var(--radius-lg);
            padding: var(--spacing-lg);
            margin-bottom: var(--spacing-lg);
            backdrop-filter: blur(20px);
            box-shadow: var(--shadow-md);
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            position: relative;
            overflow: hidden;
        }
        
        .card::before {
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            height: 1px;
            background: linear-gradient(90deg, transparent, var(--border), transparent);
        }
        
        .card:hover {
            transform: translateY(-4px);
            box-shadow: var(--shadow-lg);
            border-color: rgba(255, 255, 255, 0.15);
        }
        
        .title {
            text-align: center;
            font-size: clamp(1.5rem, 4vw, 2.2rem);
            font-weight: 800;
            margin-bottom: var(--spacing-xl);
            color: var(--text-primary);
            text-shadow: 0 2px 4px rgba(0, 0, 0, 0.3);
            letter-spacing: -0.02em;
            background: linear-gradient(135deg, #ffffff 0%, #e0e0e0 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }
        
        .status-section {
            text-align: center;
            position: relative;
        }
        
        .status {
            display: inline-flex;
            align-items: center;
            gap: var(--spacing-sm);
            padding: var(--spacing-md) var(--spacing-lg);
            border-radius: var(--radius-md);
            font-size: 1.1rem;
            font-weight: 600;
            margin-bottom: var(--spacing-lg);
            background: rgba(255, 255, 255, 0.08);
            border: 1px solid var(--border);
            backdrop-filter: blur(10px);
            transition: all 0.3s ease;
            position: relative;
            overflow: hidden;
        }
        
        .status::before {
            content: '';
            position: absolute;
            top: 0;
            left: -100%;
            width: 100%;
            height: 100%;
            background: linear-gradient(90deg, transparent, rgba(255,255,255,0.1), transparent);
            transition: left 0.5s ease;
        }
        
        .status.pulse::before {
            left: 100%;
        }
        
        .level-display {
            font-size: clamp(2rem, 8vw, 3.5rem);
            font-weight: 800;
            margin: var(--spacing-lg) 0;
            padding: var(--spacing-xl);
            border-radius: var(--radius-lg);
            background: linear-gradient(145deg, rgba(0,0,0,0.5), rgba(0,0,0,0.2));
            border: 1px solid var(--border);
            text-shadow: 0 2px 8px rgba(0, 0, 0, 0.5);
            letter-spacing: -0.02em;
            position: relative;
            overflow: hidden;
            font-variant-numeric: tabular-nums;
        }
        
        .level-display::before {
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: linear-gradient(45deg, 
                rgba(33, 150, 243, 0.05) 0%, 
                rgba(76, 175, 80, 0.05) 50%, 
                rgba(255, 152, 0, 0.05) 100%);
            opacity: 0;
            transition: opacity 0.3s ease;
        }
        
        .monitoring .level-display::before {
            opacity: 1;
        }
        
        .section-title {
            font-size: 1.25rem;
            font-weight: 700;
            margin-bottom: var(--spacing-lg);
            color: var(--text-secondary);
            display: flex;
            align-items: center;
            gap: var(--spacing-sm);
            position: relative;
        }
        
        .section-title::after {
            content: "";
            flex: 1;
            height: 1px;
            background: linear-gradient(90deg, var(--border), transparent);
            margin-left: var(--spacing-md);
        }
        
        .slider-container {
            margin: var(--spacing-xl) 0;
            padding: var(--spacing-md);
            background: rgba(255, 255, 255, 0.02);
            border-radius: var(--radius-md);
            border: 1px solid var(--border);
        }
        
        .slider-label {
            display: block;
            margin-bottom: var(--spacing-lg);
            font-weight: 600;
            color: var(--text-secondary);
            font-size: 1.1rem;
            text-align: center;
        }
        
        .slider {
            width: 100%;
            height: 10px;
            border-radius: 5px;
            background: linear-gradient(90deg, 
                var(--success) 0%, 
                var(--warning) 70%, 
                var(--error) 100%);
            outline: none;
            -webkit-appearance: none;
            margin: var(--spacing-lg) 0;
            position: relative;
            cursor: pointer;
        }
        
        .slider::-webkit-slider-thumb {
            -webkit-appearance: none;
            appearance: none;
            width: 28px;
            height: 28px;
            border-radius: 50%;
            background: var(--text-primary);
            cursor: pointer;
            box-shadow: 0 2px 8px rgba(0,0,0,0.3), 0 0 0 2px var(--primary);
            transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
            position: relative;
        }
        
        .slider::-webkit-slider-thumb:hover {
            transform: scale(1.15);
            box-shadow: 0 4px 16px rgba(0,0,0,0.4), 0 0 0 3px var(--primary);
        }
        
        .slider::-webkit-slider-thumb:active {
            transform: scale(1.2);
        }
        
        .slider::-moz-range-thumb {
            width: 28px;
            height: 28px;
            border-radius: 50%;
            background: var(--text-primary);
            cursor: pointer;
            border: 2px solid var(--primary);
            box-shadow: 0 2px 8px rgba(0,0,0,0.3);
            transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
        }
        
        .slider::-moz-range-thumb:hover {
            transform: scale(1.15);
            box-shadow: 0 4px 16px rgba(0,0,0,0.4);
        }
        
        .threshold-value {
            text-align: center;
            font-size: 1.5rem;
            font-weight: 700;
            margin-top: var(--spacing-lg);
            color: var(--primary);
            text-shadow: 0 2px 4px rgba(0,0,0,0.3);
            font-variant-numeric: tabular-nums;
        }
        
        .button {
            width: 100%;
            padding: var(--spacing-lg);
            margin: var(--spacing-md) 0;
            border: none;
            border-radius: var(--radius-md);
            font-size: 1.1rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            text-transform: uppercase;
            letter-spacing: 0.5px;
            position: relative;
            overflow: hidden;
            color: var(--text-primary);
            min-height: 56px;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: var(--spacing-sm);
        }
        
        .button::before {
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: linear-gradient(45deg, rgba(255,255,255,0.1), rgba(255,255,255,0.05));
            transform: translateX(-100%);
            transition: transform 0.3s ease;
        }
        
        .button:hover::before {
            transform: translateX(0);
        }
        
        .start-btn {
            background: linear-gradient(135deg, #43A047 0%, #2E7D32 100%);
            box-shadow: 0 4px 16px rgba(67, 160, 71, 0.3);
        }
        
        .stop-btn {
            background: linear-gradient(135deg, #e53935 0%, #c62828 100%);
            box-shadow: 0 4px 16px rgba(229, 57, 53, 0.3);
        }
        
        .calibrate-btn {
            background: linear-gradient(135deg, #1E88E5 0%, #1565C0 100%);
            box-shadow: 0 4px 16px rgba(30, 136, 229, 0.3);
        }
        
        .send-btn {
            background: linear-gradient(135deg, #FB8C00 0%, #EF6C00 100%);
            box-shadow: 0 4px 16px rgba(251, 140, 0, 0.3);
        }
        
        .button:disabled {
            opacity: 0.6;
            cursor: not-allowed;
            transform: none;
            box-shadow: none;
        }
        
        .button:not(:disabled):hover {
            transform: translateY(-2px);
            box-shadow: 0 8px 24px rgba(0,0,0,0.3);
        }
        
        .button:not(:disabled):active {
            transform: translateY(0);
        }
        
        .controls-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: var(--spacing-lg);
            margin: var(--spacing-lg) 0;
        }
        
        .message-section {
            margin-top: var(--spacing-lg);
        }
        
        .quick-messages {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
            gap: var(--spacing-md);
            margin-bottom: var(--spacing-xl);
        }
        
        .quick-btn {
            padding: var(--spacing-md);
            border: 1px solid var(--border);
            border-radius: var(--radius-md);
            background: rgba(255,255,255,0.05);
            color: var(--text-primary);
            font-size: 0.95rem;
            font-weight: 500;
            cursor: pointer;
            transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
            backdrop-filter: blur(10px);
            text-align: center;
            min-height: 50px;
            display: flex;
            align-items: center;
            justify-content: center;
            position: relative;
            overflow: hidden;
        }
        
        .quick-btn::before {
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: linear-gradient(45deg, rgba(255,255,255,0.1), transparent);
            transform: translateX(-100%);
            transition: transform 0.3s ease;
        }
        
        .quick-btn:hover::before {
            transform: translateX(0);
        }
        
        .quick-btn:hover {
            background: rgba(255,255,255,0.1);
            transform: translateY(-2px);
            box-shadow: var(--shadow-md);
            border-color: var(--primary);
        }
        
        .quick-btn:active {
            transform: translateY(0);
        }
        
        .message-input {
            width: 100%;
            padding: var(--spacing-lg);
            border: 1px solid var(--border);
            border-radius: var(--radius-md);
            background: rgba(255,255,255,0.05);
            color: var(--text-primary);
            font-size: 1.1rem;
            margin-bottom: var(--spacing-lg);
            resize: vertical;
            min-height: 120px;
            font-family: inherit;
            backdrop-filter: blur(10px);
            transition: all 0.3s ease;
            line-height: 1.5;
        }
        
        .message-input::placeholder {
            color: rgba(255,255,255,0.4);
        }
        
        .message-input:focus {
            outline: none;
            border-color: var(--primary);
            box-shadow: 0 0 0 3px rgba(33,150,243,0.2);
            background: rgba(255,255,255,0.08);
        }
        
        .info {
            text-align: center;
            margin-top: var(--spacing-lg);
            font-size: 0.95rem;
            color: var(--text-muted);
            line-height: 1.6;
            padding: var(--spacing-lg);
            background: rgba(255,255,255,0.02);
            border-radius: var(--radius-md);
            border: 1px solid var(--border);
        }
        
        @keyframes pulse {
            0% { transform: scale(1); }
            50% { transform: scale(1.02); }
            100% { transform: scale(1); }
        }
        
        @keyframes glow {
            0%, 100% { box-shadow: 0 0 5px rgba(33, 150, 243, 0.5); }
            50% { box-shadow: 0 0 20px rgba(33, 150, 243, 0.8); }
        }
        
        .monitoring .level-display {
            animation: pulse 2s infinite ease-in-out;
        }
        
        .monitoring .status {
            animation: glow 2s infinite ease-in-out;
        }
        
        .vu-meter-container {
            position: relative;
            width: 100%;
            height: 20px;
            background: rgba(0,0,0,0.3);
            border-radius: 10px;
            margin: var(--spacing-md) 0;
            overflow: hidden;
            border: 1px solid var(--border);
        }
        .vu-meter-bar {
            position: absolute;
            top: 0;
            left: 0;
            height: 100%;
            width: 0%;
            background: linear-gradient(90deg, 
                var(--success) 0%, 
                var(--warning) 70%, 
                var(--error) 100%);
            border-radius: 10px;
            transition: width 0.1s linear;
        }
        .vu-meter-threshold {
            position: absolute;
            top: 0;
            bottom: 0;
            width: 3px;
            background: var(--primary);
            transition: left 0.2s ease;
            box-shadow: 0 0 5px rgba(33, 150, 243, 0.7);
        }
        
        /* Toast notifications */
        .toast {
            position: fixed;
            top: var(--spacing-lg);
            right: var(--spacing-lg);
            background: var(--bg-card);
            color: var(--text-primary);
            padding: var(--spacing-lg);
            border-radius: var(--radius-md);
            border: 1px solid var(--border);
            backdrop-filter: blur(20px);
            box-shadow: var(--shadow-lg);
            transform: translateX(400px);
            transition: transform 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            z-index: 1000;
            max-width: 300px;
        }
        
        .toast.show {
            transform: translateX(0);
        }
        
        .toast.success {
            border-color: var(--success);
        }
        
        .toast.error {
            border-color: var(--error);
        }
        
        /* Loading spinner */
        .spinner {
            width: 20px;
            height: 20px;
            border: 2px solid rgba(255,255,255,0.3);
            border-top: 2px solid var(--text-primary);
            border-radius: 50%;
            animation: spin 1s linear infinite;
            margin-right: var(--spacing-sm);
        }
        
        @keyframes spin {
            0% { transform: rotate(0deg); }
            100% { transform: rotate(360deg); }
        }
        
        /* Responsive Design */
        @media (max-width: 768px) {
            body {
                padding: var(--spacing-sm);
                font-size: 14px;
            }
            
            .container {
                max-width: 100%;
            }
            
            .card {
                padding: var(--spacing-lg);
                margin-bottom: var(--spacing-md);
            }
            
            .title {
                margin-bottom: var(--spacing-lg);
            }
            
            .level-display {
                padding: var(--spacing-lg);
                margin: var(--spacing-md) 0;
            }
            
            .controls-grid {
                grid-template-columns: 1fr;
                gap: var(--spacing-md);
            }
            
            .button {
                padding: var(--spacing-md);
                font-size: 1rem;
                min-height: 48px;
            }
            
            .quick-messages {
                grid-template-columns: 1fr;
                gap: var(--spacing-sm);
            }
            
            .quick-btn {
                min-height: 44px;
                font-size: 0.9rem;
            }
            
            .message-input {
                min-height: 100px;
                font-size: 1rem;
            }
            
            .slider-container {
                margin: var(--spacing-lg) 0;
                padding: var(--spacing-sm);
            }
            
            .section-title {
                font-size: 1.1rem;
            }
            
            .toast {
                top: var(--spacing-sm);
                right: var(--spacing-sm);
                left: var(--spacing-sm);
                max-width: none;
            }
        }
        
        @media (max-width: 480px) {
            body {
                padding: var(--spacing-xs);
            }
            
            .card {
                padding: var(--spacing-md);
                border-radius: var(--radius-md);
            }
            
            .level-display {
                padding: var(--spacing-md);
            }
            
            .button {
                padding: var(--spacing-sm) var(--spacing-md);
                min-height: 44px;
            }
            
            .slider {
                height: 8px;
            }
            
            .slider::-webkit-slider-thumb {
                width: 24px;
                height: 24px;
            }
            
            .slider::-moz-range-thumb {
                width: 24px;
                height: 24px;
            }
        }
        
        @media (min-width: 769px) {
            .quick-messages {
                grid-template-columns: repeat(2, 1fr);
            }
        }
        
        /* High DPI displays */
        @media (-webkit-min-device-pixel-ratio: 2), (min-resolution: 192dpi) {
            .card {
                backdrop-filter: blur(30px);
            }
        }
        
        /* Dark mode specific improvements */
        @media (prefers-color-scheme: dark) {
            body {
                background: linear-gradient(135deg, #0f0f0f 0%, #1a1a1a 100%);
            }
        }
        
        /* Reduce motion for accessibility */
        @media (prefers-reduced-motion: reduce) {
            * {
                animation-duration: 0.01ms !important;
                animation-iteration-count: 1 !important;
                transition-duration: 0.01ms !important;
            }
        }
        
        /* Main layout grid for desktop */
        .main-grid {
            display: grid;
            grid-template-columns: 1fr;
            gap: var(--spacing-lg);
        }

        @media (min-width: 992px) {
            .main-grid {
                grid-template-columns: repeat(2, minmax(0, 1fr));
                align-items: start;
            }

            .container {
                max-width: 1100px;
            }

            .full-width-card {
                grid-column: 1 / -1;
            }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="card full-width-card">
            <h1 class="title">🔊 Noise Alert</h1>
            
            <div class="status-section">
                <div class="status" id="status">
                    <span id="monitoring-status">● LOADING...</span>
                </div>
                
                <div class="level-display" id="current-level">
                    --.-- dB
                </div>

                <div class="vu-meter-container">
                    <div class="vu-meter-bar" id="vu-meter-bar"></div>
                    <div class="vu-meter-threshold" id="vu-meter-threshold"></div>
                </div>
            </div>
        </div>
        
        <div class="main-grid">
            <div class="grid-col-1">
                <div class="card">
                    <div class="section-title">🎚️ Sensitivity Control</div>
                    <div class="slider-container">
                        <label class="slider-label">Noise Threshold</label>
                        <input type="range" min="-60" max="0" value="-30" class="slider" id="threshold-slider">
                        <div class="threshold-value" id="threshold-value">-30.0 dB</div>
                    </div>
                    
                    <div class="controls-grid">
                        <button class="button start-btn" id="start-btn" onclick="startMonitoring()">
                            ▶️ Start
                        </button>
                        
                        <button class="button stop-btn" id="stop-btn" onclick="stopMonitoring()">
                            ⏹️ Stop
                        </button>
                    </div>
                    

                </div>
                
                <div class="card">
                    <div class="info">
                        🎯 Remote control interface for noise monitoring<br>
                        🔊 Lower threshold = more sensitive to noise<br>
                        💬 Messages will be spoken through the computer speakers
                    </div>
                </div>
            </div>

            <div class="grid-col-2">
                <div class="card">
                    <div class="section-title">💬 Voice Messages</div>
                    <div class="message-section">
                        <div class="quick-messages">
                            <button class="quick-btn" onclick="sendQuickMessage('Please turn down the volume')">
                                🔉 Turn Down Volume
                            </button>
                            <button class="quick-btn" onclick="sendQuickMessage('You are being too loud')">
                                🔊 Too Loud
                            </button>
                            <button class="quick-btn" onclick="sendQuickMessage('Please be quieter')">
                                🤫 Be Quieter
                            </button>
                            <button class="quick-btn" onclick="sendQuickMessage('Thank you for being quieter')">
                                👍 Thank You
                            </button>
                        </div>
                        
                        <textarea class="message-input" id="message-input" 
                                 placeholder="Type a custom message to be spoken through the computer speakers..."></textarea>
                        
                        <button class="button send-btn" onclick="sendMessage()">
                            📢 Send Voice Message
                        </button>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <script>
        const slider = document.getElementById('threshold-slider');
        const thresholdValue = document.getElementById('threshold-value');
        const currentLevel = document.getElementById('current-level');
        const vuMeterBar = document.getElementById('vu-meter-bar');
        const vuMeterThreshold = document.getElementById('vu-meter-threshold');
        const status = document.getElementById('monitoring-status');
        const startBtn = document.getElementById('start-btn');
        const stopBtn = document.getElementById('stop-btn');
        const messageInput = document.getElementById('message-input');
        const statusSection = document.querySelector('.status-section');
        
        // Toast notification system
        function showToast(message, type = 'success') {
            const toast = document.createElement('div');
            toast.className = `toast ${type}`;
            toast.textContent = message;
            document.body.appendChild(toast);
            
            setTimeout(() => toast.classList.add('show'), 100);
            setTimeout(() => {
                toast.classList.remove('show');
                setTimeout(() => document.body.removeChild(toast), 300);
            }, 3000);
        }
        
        // Add haptic feedback for mobile devices
        function vibrate() {
            if ('vibrate' in navigator) {
                navigator.vibrate(50);
            }
        }
        
        // Update threshold display when slider moves
        slider.oninput = function() {
            const value = parseFloat(this.value);
            thresholdValue.textContent = value.toFixed(1) + ' dB';
            
            // Add visual feedback
            this.style.boxShadow = '0 0 10px rgba(33, 150, 243, 0.5)';
            setTimeout(() => {
                this.style.boxShadow = '';
            }, 200);
            
            fetch('/set_threshold', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({threshold: value})
            });
        }
        
        function dbToPercentage(db, min_db = -60, max_db = 0) {
            if (db < min_db) db = min_db;
            if (db > max_db) db = max_db;
            return ((db - min_db) / (max_db - min_db)) * 100;
        }

        // Fetch current state periodically
        function updateState() {
            fetch('/get_state')
                .then(response => response.json())
                .then(data => {
                    const db = data.current_db;
                    const threshold = data.threshold;

                    currentLevel.textContent = db.toFixed(1) + ' dB';
                    
                    // Update VU Meter
                    vuMeterBar.style.width = dbToPercentage(db) + '%';
                    vuMeterThreshold.style.left = dbToPercentage(threshold) + '%';
                    
                    if (data.is_monitoring) {
                        status.textContent = '● MONITORING';
                        status.style.color = 'var(--success)';
                        statusSection.classList.add('monitoring');
                        startBtn.disabled = true;
                        stopBtn.disabled = false;
                    } else {
                        status.textContent = '● STOPPED';
                        status.style.color = 'var(--warning)';
                        statusSection.classList.remove('monitoring');
                        startBtn.disabled = false;
                        stopBtn.disabled = true;
                    }
                    
                    // Update slider if threshold changed from main app
                    if (Math.abs(data.threshold - parseFloat(slider.value)) > 0.1) {
                        slider.value = data.threshold;
                        thresholdValue.textContent = data.threshold.toFixed(1) + ' dB';
                    }
                })
                .catch(err => {
                    status.textContent = '● CONNECTION ERROR';
                    status.style.color = 'var(--error)';
                    statusSection.classList.remove('monitoring');
                });
        }
        
        function startMonitoring() {
            vibrate();
            showToast('Starting monitoring...', 'success');
            fetch('/start_monitoring', {method: 'POST'});
        }
        
        function stopMonitoring() {
            vibrate();
            showToast('Stopping monitoring...', 'warning');
            fetch('/stop_monitoring', {method: 'POST'});
        }
        

        
        function showButtonFeedback(element, success) {
            const originalContent = element.innerHTML;
            element.disabled = true;
            
            if (success) {
                element.innerHTML = '✅ Sent!';
                element.style.background = 'linear-gradient(135deg, var(--success) 0%, #2E7D32 100%)';
            } else {
                element.innerHTML = '❌ Failed';
                element.style.background = 'linear-gradient(135deg, var(--error) 0%, #c62828 100%)';
            }
            
            setTimeout(() => {
                element.disabled = false;
                element.innerHTML = originalContent;
                element.style.background = '';
            }, 2000);
        }
        
        function sendMessage() {
            const message = messageInput.value.trim();
            if (!message) {
                showToast('Please enter a message to send.', 'error');
                messageInput.focus();
                return;
            }
            
            vibrate();
            const sendBtn = document.querySelector('.send-btn');
            const originalContent = sendBtn.innerHTML;
            sendBtn.disabled = true;
            sendBtn.innerHTML = '<div class="spinner"></div>Sending...';
            
            fetch('/send_message', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({message: message})
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    messageInput.value = '';
                    showToast('Message sent successfully!', 'success');
                    showButtonFeedback(sendBtn, true);
                } else {
                    showToast(`Failed to send message: ${data.error}`, 'error');
                    showButtonFeedback(sendBtn, false);
                }
            })
            .catch(err => {
                showToast(`Error sending message: ${err.message}`, 'error');
                showButtonFeedback(sendBtn, false);
            })
            .finally(() => {
                setTimeout(() => {
                    sendBtn.disabled = false;
                    sendBtn.innerHTML = originalContent;
                }, 2000);
            });
        }
        
        function sendQuickMessage(message) {
            vibrate();
            const btn = event.target;
            const originalContent = btn.innerHTML;
            btn.disabled = true;
            btn.innerHTML = '<div class="spinner"></div>Sending...';
            
            fetch('/send_message', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({message: message})
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    showToast('Quick message sent!', 'success');
                    showButtonFeedback(btn, true);
                } else {
                    showToast(`Failed to send message: ${data.error}`, 'error');
                    showButtonFeedback(btn, false);
                }
            })
            .catch(err => {
                showToast(`Error sending message: ${err.message}`, 'error');
                showButtonFeedback(btn, false);
            })
            .finally(() => {
                setTimeout(() => {
                    btn.disabled = false;
                    btn.innerHTML = originalContent;
                }, 2000);
            });
        }
        
        // Enhanced keyboard support
        messageInput.addEventListener('keydown', function(e) {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                sendMessage();
            }
        });
        
        // Add keyboard shortcuts
        document.addEventListener('keydown', function(e) {
            if (e.ctrlKey || e.metaKey) {
                switch(e.key) {
                    case 's':
                        e.preventDefault();
                        if (!startBtn.disabled) startMonitoring();
                        break;
                    case 'x':
                        e.preventDefault();
                        if (!stopBtn.disabled) stopMonitoring();
                        break;
                }
            }
        });
        
        // PWA-like experience - hide address bar on mobile
        window.addEventListener('load', function() {
            setTimeout(function() {
                window.scrollTo(0, 1);
            }, 0);
        });
        
        // Update every 100ms
        setInterval(updateState, 100);
        updateState(); // Initial load
        
        // Add smooth page transitions
        document.addEventListener('DOMContentLoaded', function() {
            document.body.style.opacity = '0';
            setTimeout(() => {
                document.body.style.transition = 'opacity 0.3s ease';
                document.body.style.opacity = '1';
            }, 100);
        });
    </script>
</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(WEB_TEMPLATE)

@app.route('/get_state')
def get_state():
    return jsonify(shared_state.get_state())

@app.route('/set_threshold', methods=['POST'])
def set_threshold():
    data = request.get_json()
    threshold = float(data['threshold'])
    shared_state.update_threshold(threshold)
    return jsonify({'success': True})

@app.route('/start_monitoring', methods=['POST'])
def start_monitoring_web():
    shared_state.web_start_requested = True
    return jsonify({'success': True})

@app.route('/stop_monitoring', methods=['POST'])
def stop_monitoring_web():
    shared_state.web_stop_requested = True
    return jsonify({'success': True})

@app.route('/send_message', methods=['POST'])
def send_message():
    try:
        data = request.get_json()
        message = data.get('message', '').strip()
        
        if not message:
            return jsonify({
                'success': False,
                'error': 'No message provided'
            })
        
        if len(message) > 500:
            return jsonify({
                'success': False,
                'error': 'Message too long (max 500 characters)'
            })
        
        shared_state.add_tts_message(message)
        
        return jsonify({
            'success': True,
            'message': 'Message queued for speech'
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        })

class WebServerThread(threading.Thread):
    def __init__(self, port=8080):
        super().__init__(daemon=True)
        self.port = port
        self.server = None
        self.should_stop = False
        
    def run(self):
        try:
            self.server = make_server('0.0.0.0', self.port, app)
            self.server.timeout = 0.5
            
            while not self.should_stop:
                self.server.handle_request()
                
        except Exception as e:
            print(f"Web server error: {e}")
            
    def stop(self):
        self.should_stop = True
        time.sleep(0.6)
        if self.server:
            self.server.server_close()

def stop_web_server():
    global web_server
    if web_server is not None:
        try:
            web_server.stop()
            web_server.join(timeout=2)
        except Exception as e:
            print(f"Error stopping web server: {e}")
        finally:
            web_server = None
            web_info_label.config(text="Remote access disabled.")
            port_entry.config(state="normal")

import tkinter as tk
from tkinter import ttk
from tkinter import messagebox

class AppColors:
    BACKGROUND = '#212121'
    CARD = '#2d2d2d'
    TEXT = '#e0e0e0'
    TEXT_MUTED = '#9e9e9e'
    PRIMARY = '#2196F3'
    SUCCESS = '#4CAF50'
    WARNING = '#FF9800'
    ERROR = '#f44336'
    BORDER = '#424242'

class Card(ttk.Frame):
    def __init__(self, parent, title="", **kwargs):
        super().__init__(parent, style='Card.TFrame', **kwargs)
        self.columnconfigure(0, weight=1)
        
        if title:
            title_label = ttk.Label(self, text=title, style='CardTitle.TLabel')
            title_label.grid(row=0, column=0, sticky='w', padx=15, pady=(10, 5))
            
            separator = ttk.Separator(self, orient='horizontal')
            separator.grid(row=1, column=0, sticky='ew', padx=15, pady=(0, 10))
            
            self.content_frame = ttk.Frame(self, style='Card.TFrame')
            self.content_frame.grid(row=2, column=0, sticky='nsew', padx=15, pady=(0, 15))
            self.content_frame.columnconfigure(0, weight=1)
        else:
            self.content_frame = self

class VUMeter(tk.Canvas):
    def __init__(self, parent, width=400, height=50, **kwargs):
        super().__init__(parent, width=width, height=height, bg=AppColors.CARD, 
                        highlightthickness=1, highlightbackground=AppColors.BORDER, **kwargs)
        self.width = width
        self.height = height
        self.current_db = -100
        self.threshold_db = -30
        self.peak_db = -100
        self.peak_hold_time = 0
        
        self.bar_width = 6
        self.bar_spacing = 1
        self.num_bars = (width - 40) // (self.bar_width + self.bar_spacing)
        
        self.db_min = -60
        self.db_max = 0
        
        self.draw_static_elements()
        
    def draw_static_elements(self):
        self.delete("all")
        
        for db in [-60, -40, -20, -10, -5, 0]:
            x_pos = self.db_to_x(db)
            self.create_line(x_pos, self.height-8, x_pos, self.height-3, fill=AppColors.TEXT_MUTED, width=1)
            self.create_text(x_pos, self.height-1, text=str(db), fill=AppColors.TEXT, 
                           font=('Segoe UI', 7), anchor='n')
    
    def db_to_x(self, db):
        if db <= self.db_min:
            return 20
        if db >= self.db_max:
            return 20 + (self.num_bars - 1) * (self.bar_width + self.bar_spacing)
        
        ratio = (db - self.db_min) / (self.db_max - self.db_min)
        return 20 + ratio * (self.num_bars - 1) * (self.bar_width + self.bar_spacing)
    
    def get_bar_color(self, db):
        if db < -20:
            return AppColors.SUCCESS
        elif db < -10:
            return AppColors.WARNING
        else:
            return AppColors.ERROR
    
    def update_level(self, db, threshold_db):
        self.current_db = db
        self.threshold_db = threshold_db
        
        if db > self.peak_db:
            self.peak_db = db
            self.peak_hold_time = time.time()
        elif time.time() - self.peak_hold_time > 1.0:
            self.peak_db = max(self.peak_db - 0.5, db)
        
        self.redraw()
    
    def redraw(self):
        self.delete("bar", "threshold", "peak", "level_text")
        
        current_x = self.db_to_x(self.current_db)
        for i in range(self.num_bars):
            x = 20 + i * (self.bar_width + self.bar_spacing)
            if x <= current_x:
                bar_db = self.db_min + (i / self.num_bars) * (self.db_max - self.db_min)
                color = self.get_bar_color(bar_db)
                self.create_rectangle(x, 8, x + self.bar_width, self.height-12, 
                                    fill=color, outline=color, tags="bar")
        
        thresh_x = self.db_to_x(self.threshold_db)
        self.create_line(thresh_x, 5, thresh_x, self.height-12, 
                        fill=AppColors.PRIMARY, width=2, tags="threshold")
        self.create_text(thresh_x, 4, text="TRIG", fill=AppColors.PRIMARY, 
                        font=('Segoe UI', 7, 'bold'), anchor='s', tags="threshold")
        
        if self.peak_db > self.db_min:
            peak_x = self.db_to_x(self.peak_db)
            self.create_line(peak_x, 8, peak_x, self.height-12, 
                           fill='white', width=1, tags="peak")
        
        level_text = f"{self.current_db:+5.1f} dB"
        self.create_text(self.width-5, 15, text=level_text, fill='white', 
                        font=('Segoe UI', 10, 'bold'), anchor='e', tags="level_text")

root = tk.Tk()
root.title("🔊 Noise Alert")
root.configure(background=AppColors.BACKGROUND)

import base64, zlib, tempfile, os

_BLANK_ICO_B64 = (
    "eJxjYGAEQgEBBiDJwZDBysAgxsDAoAHEQCEGBQaIOAg4sDIgACMUj4JRMApGwQgF/ykEAFXxQRc="
)

try:
    _ico_bytes = zlib.decompress(base64.b64decode(_BLANK_ICO_B64))

    _fd, _ico_path = tempfile.mkstemp(suffix=".ico")
    with os.fdopen(_fd, "wb") as _f:
        _f.write(_ico_bytes)

    root.iconbitmap(default=_ico_path)
except Exception as _e:
    print("Transparent-icon setup failed:", _e)

style = ttk.Style()
style.theme_use('clam')

style.configure('TFrame', background=AppColors.BACKGROUND)
style.configure('Card.TFrame', background=AppColors.CARD)
style.configure('TLabel', background=AppColors.BACKGROUND, foreground=AppColors.TEXT, font=('Segoe UI', 10))
style.configure('Title.TLabel', background=AppColors.BACKGROUND, foreground='white', font=('Segoe UI', 18, 'bold'))
style.configure('CardTitle.TLabel', background=AppColors.CARD, foreground='white', font=('Segoe UI', 12, 'bold'))
style.configure('Card.TLabel', background=AppColors.CARD, foreground=AppColors.TEXT, font=('Segoe UI', 10))
style.configure('Muted.Card.TLabel', background=AppColors.CARD, foreground=AppColors.TEXT_MUTED, font=('Segoe UI', 9))
style.configure('Primary.TLabel', background=AppColors.CARD, foreground=AppColors.PRIMARY, font=('Segoe UI', 10, 'bold'))
style.configure('TButton', font=('Segoe UI', 10, 'bold'), padding=(15, 8), relief='flat', borderwidth=0)
style.map('TButton',
          foreground=[('!active', 'white'), ('active', 'white')],
          background=[('!active', AppColors.PRIMARY), ('active', '#1976D2')])

style.configure('Success.TButton', background=AppColors.SUCCESS)
style.map('Success.TButton', background=[('active', '#388E3C')])
style.configure('Warning.TButton', background=AppColors.ERROR)
style.map('Warning.TButton', background=[('active', '#D32F2F')])
style.configure('Secondary.TButton', background=AppColors.WARNING)
style.map('Secondary.TButton', background=[('active', '#F57C00')])
style.configure('TCheckbutton', background=AppColors.CARD, foreground=AppColors.TEXT, font=('Segoe UI', 10))
style.map('TCheckbutton',
          background=[('active', AppColors.CARD)],
          indicatorcolor=[('selected', AppColors.PRIMARY), ('!selected', AppColors.BORDER)])
style.configure('TScale', background=AppColors.CARD)
style.configure('Horizontal.TScale', troughcolor=AppColors.BORDER, background=AppColors.PRIMARY)

input_dev_list = list_input_devices()
input_device_map_name_to_idx = {name: idx for idx, name in input_dev_list}
input_device_map_idx_to_name = {idx: name for idx, name in input_dev_list}
input_device_names = list(input_device_map_name_to_idx.keys())

output_dev_list = list_output_devices()
output_device_map_name_to_idx = {name: idx for idx, name in output_dev_list}
output_device_map_idx_to_name = {idx: name for idx, name in output_dev_list}
output_device_names = list(output_device_map_name_to_idx.keys())

main_frame = ttk.Frame(root, style='TFrame')
main_frame.pack(fill='both', expand=True, padx=15, pady=15)
main_frame.columnconfigure(0, weight=1)

title_label = ttk.Label(main_frame, text="🔊 Noise Alert", style='Title.TLabel')
title_label.grid(row=0, column=0, sticky='w', pady=(0, 20))

vu_card = Card(main_frame, title="📊 Audio Level Monitor")
vu_card.grid(row=1, column=0, sticky='ew', pady=(0, 10))
vu_content = vu_card.content_frame

vu_meter = VUMeter(vu_content, width=650, height=60)
vu_meter.pack(pady=5, fill='x', expand=True)

status_frame = ttk.Frame(vu_content, style='Card.TFrame')
status_frame.pack(fill='x', expand=True, pady=(5,0))
status_frame.columnconfigure(1, weight=1)

status_label = ttk.Label(status_frame, text="● STOPPED", style='Card.TLabel', foreground=AppColors.WARNING, font=('Segoe UI', 10, 'bold'))
status_label.grid(row=0, column=0, sticky='w')

thresh_frame = ttk.Frame(status_frame, style='Card.TFrame')
thresh_frame.grid(row=0, column=2, sticky='e')

push_job = None

def schedule_push_settings():
    global push_job
    if push_job:
        root.after_cancel(push_job)
    if monitor and monitor.is_alive():
        push_job = root.after(250, push_settings)

def on_threshold_change(value):
    val = float(value)
    thresh_value_label.config(text=f"{val:.1f} dB")
    if 'vu_meter' in globals():
        vu_meter.threshold_db = val
    shared_state.update_threshold(val)
    schedule_push_settings()

ttk.Label(thresh_frame, text="Threshold:", style='Card.TLabel').pack(side='left', padx=(0, 5))
th_var = tk.DoubleVar(value=shared_state.threshold)
thresh_scale = ttk.Scale(thresh_frame, from_=-60, to=0, orient="horizontal", variable=th_var, length=200, style='Horizontal.TScale', command=on_threshold_change)
thresh_scale.pack(side='left', padx=(0, 5))
thresh_value_label = ttk.Label(thresh_frame, text=f"{th_var.get():.1f} dB", style='Primary.TLabel')
thresh_value_label.pack(side='left')

settings_card = Card(main_frame, title="⚙️ Settings")
settings_card.grid(row=2, column=0, sticky='ew', pady=(0, 10))
settings_content = settings_card.content_frame
settings_content.columnconfigure(1, weight=1)

ttk.Label(settings_content, text="Input Device:", style='Card.TLabel', font=('Segoe UI', 10, 'bold')).grid(row=0, column=0, sticky='w', pady=5)
dev_name_var = tk.StringVar()
initial_device_name_to_set = "No input devices found"

if input_device_names:
    loaded_device_idx = cfg.get("device")
    selected_name = None
    if loaded_device_idx is not None:
        selected_name = input_device_map_idx_to_name.get(loaded_device_idx)

    if not selected_name:
        try:
            default_system_device_idx = get_default_device("input")
            if default_system_device_idx is not None:
                 selected_name = input_device_map_idx_to_name.get(default_system_device_idx)
        except Exception as e:
            print(f"Could not get default system device: {e}")
            pass
            
    if not selected_name:
        selected_name = input_device_names[0]
    
    initial_device_name_to_set = selected_name
    dev_name_var.set(initial_device_name_to_set)
    
    device_dropdown_style = ttk.Style()
    device_dropdown_style.configure('Custom.TCombobox', 
                                    fieldbackground=AppColors.BACKGROUND, 
                                    background=AppColors.CARD,
                                    foreground='white',
                                    arrowcolor='white',
                                    bordercolor=AppColors.BORDER,
                                    lightcolor=AppColors.CARD,
                                    darkcolor=AppColors.CARD,
                                    borderwidth=1,
                                    relief='solid')
    
    device_dropdown_style.map('Custom.TCombobox',
        foreground=[('readonly', 'white')],
        fieldbackground=[('readonly', AppColors.BACKGROUND)],
        selectbackground=[('readonly', AppColors.PRIMARY)],
        selectforeground=[('readonly', 'white')])

    device_dropdown = ttk.Combobox(settings_content, textvariable=dev_name_var, values=input_device_names, 
                                  state="readonly", width=60, font=('Segoe UI', 10), style='Custom.TCombobox')
    device_dropdown.grid(row=0, column=1, columnspan=4, sticky='ew', padx=(10, 0), pady=5)
else:
    dev_name_var.set(initial_device_name_to_set)
    ttk.Label(settings_content, textvariable=dev_name_var, style='Card.TLabel').grid(row=0, column=1, columnspan=4, sticky='ew', pady=3)

ttk.Label(settings_content, text="Output Device:", style='Card.TLabel', font=('Segoe UI', 10, 'bold')).grid(row=1, column=0, sticky='w', pady=5)
output_dev_name_var = tk.StringVar()
initial_output_device_name = "No output devices found"

if output_device_names:
    loaded_output_idx = cfg.get("output_device")
    selected_output_name = None
    if loaded_output_idx is not None:
        selected_output_name = output_device_map_idx_to_name.get(loaded_output_idx)
    
    if not selected_output_name:
        try:
            default_output_idx = get_default_device("output")
            if default_output_idx is not None:
                selected_output_name = output_device_map_idx_to_name.get(default_output_idx)
        except Exception:
            pass

    if not selected_output_name:
        selected_output_name = output_device_names[0]
        
    initial_output_device_name = selected_output_name
    output_dev_name_var.set(initial_output_device_name)

    output_device_dropdown = ttk.Combobox(settings_content, textvariable=output_dev_name_var, values=output_device_names,
                                          state="readonly", width=60, font=('Segoe UI', 10), style='Custom.TCombobox')
    output_device_dropdown.grid(row=1, column=1, columnspan=4, sticky='ew', padx=(10, 0), pady=5)
else:
    output_dev_name_var.set(initial_output_device_name)
    ttk.Label(settings_content, textvariable=output_dev_name_var, style='Card.TLabel').grid(row=1, column=1, columnspan=4, sticky='ew', pady=5)

ttk.Label(settings_content, text="Alert Tone:", style='Card.TLabel', font=('Segoe UI', 10, 'bold')).grid(row=2, column=0, sticky='w', pady=5)
tone_frame = ttk.Frame(settings_content, style='Card.TFrame')
tone_frame.grid(row=2, column=1, columnspan=4, sticky='ew', padx=(10,0))

shape_var = tk.StringVar(value=cfg.get("shape","Sine"))
shape_combo = ttk.Combobox(tone_frame, textvariable=shape_var, values=["Sine", "Square", "Saw"], 
                          state="readonly", width=8, font=('Segoe UI', 9))
shape_combo.pack(side='left', padx=(0, 10))

freq_var = tk.IntVar(value=cfg.get("freq",1000))
freq_frame = ttk.Frame(tone_frame, style='Card.TFrame')
freq_frame.pack(side='left', padx=(0, 10))
ttk.Label(freq_frame, text="Freq:", style='Muted.Card.TLabel').pack(side='left', padx=(0, 3))
ttk.Entry(freq_frame, textvariable=freq_var, width=6, font=('Segoe UI', 9)).pack(side='left')
ttk.Label(freq_frame, text="Hz", style='Muted.Card.TLabel').pack(side='left', padx=(2, 0))

dur_var = tk.IntVar(value=cfg.get("dur",200))
dur_frame = ttk.Frame(tone_frame, style='Card.TFrame')
dur_frame.pack(side='left', padx=(0, 10))
ttk.Label(dur_frame, text="Dur:", style='Muted.Card.TLabel').pack(side='left', padx=(0, 3))
ttk.Entry(dur_frame, textvariable=dur_var, width=6, font=('Segoe UI', 9)).pack(side='left')
ttk.Label(dur_frame, text="ms", style='Muted.Card.TLabel').pack(side='left', padx=(2, 0))

vol_var = tk.IntVar(value=cfg.get("vol",50))
vol_frame = ttk.Frame(tone_frame, style='Card.TFrame')
vol_frame.pack(side='left', padx=(0, 10))
ttk.Label(vol_frame, text="Vol:", style='Muted.Card.TLabel').pack(side='left', padx=(0, 3))
ttk.Entry(vol_frame, textvariable=vol_var, width=4, font=('Segoe UI', 9)).pack(side='left')
ttk.Label(vol_frame, text="%", style='Muted.Card.TLabel').pack(side='left', padx=(2, 0))

def schedule_push_settings():
    global push_job
    if push_job:
        root.after_cancel(push_job)
    if monitor and monitor.is_alive():
        push_job = root.after(250, push_settings)

def on_threshold_change(value):
    val = float(value)
    thresh_value_label.config(text=f"{val:.1f} dB")
    if 'vu_meter' in globals():
        vu_meter.threshold_db = val
    shared_state.update_threshold(val)
    schedule_push_settings()

def push_settings():
    selected_name = dev_name_var.get()
    device_idx = input_device_map_name_to_idx.get(selected_name, -1)
    
    if device_idx == -1 and input_device_names:
        print("Warning: No valid input device selected, trying first available.")
        device_idx = input_device_map_name_to_idx.get(input_device_names[0], -1)

    selected_output_name = output_dev_name_var.get()
    output_device_idx = output_device_map_name_to_idx.get(selected_output_name, None)
    
    shared_state.current_device = device_idx
    shared_state.update_threshold(th_var.get())

    q_cmd.put({
        "thresh": th_var.get(),
        "shape" : shape_var.get(),
        "freq"  : freq_var.get(),
        "dur"   : dur_var.get(),
        "vol"   : vol_var.get(),
        "fs"    : 44100,
        "chunk" : 0.1,
        "delay" : 0.02,
        "device": device_idx,
        "output_device": output_device_idx,
        "web_enabled": web_enabled_var.get(),
        "web_port": web_port_var.get(),
    })

def start():
    global monitor
    if monitor and monitor.is_alive():
        return
    monitor = Monitor(q_cmd, q_ui)
    push_settings()
    monitor.start()
    start_btn.config(state="disabled")
    stop_btn.config(state="normal")
    status_label.config(text="● MONITORING", foreground=AppColors.SUCCESS)
    shared_state.update_monitoring(True)

def stop():
    if monitor:
        monitor.stop_evt.set()
        monitor.join(timeout=1)
    start_btn.config(state="normal")
    stop_btn.config(state="disabled")
    status_label.config(text="● STOPPED", foreground=AppColors.WARNING)
    shared_state.update_monitoring(False)

def calibrate():
    selected_name = dev_name_var.get()
    device_idx = input_device_map_name_to_idx.get(selected_name, -1)
    if device_idx == -1:
        messagebox.showerror("Calibration Error", "No valid input device selected.")
        return

    calib_window = tk.Toplevel(root)
    calib_window.title("Auto Calibration")
    calib_window.configure(bg=AppColors.CARD)
    calib_window.geometry("400x200")
    calib_window.transient(root)
    calib_window.grab_set()
    calib_window.resizable(False, False)
    
    root_x = root.winfo_rootx()
    root_y = root.winfo_rooty()
    root_w = root.winfo_width()
    root_h = root.winfo_height()
    calib_window.geometry(f"+{root_x + root_w // 2 - 200}+{root_y + root_h // 2 - 100}")
    
    calib_window.columnconfigure(0, weight=1)
    
    ttk.Label(calib_window, text="🎯 Calibrating Audio Threshold", 
              style='CardTitle.TLabel', font=('Segoe UI', 14, 'bold')).pack(pady=20)
    ttk.Label(calib_window, text="Please remain quiet for 2 seconds while we measure\nthe ambient noise level...", 
              style='Card.TLabel', justify='center').pack(pady=10)
    
    progress = ttk.Progressbar(calib_window, mode='indeterminate', length=300)
    progress.pack(pady=20, padx=40)
    progress.start()
    
    calib_window.update()
    
    frames = int(2.0*44100)
    try:
        pa = pyaudio.PyAudio()
        try:
            stream = pa.open(
                format=pyaudio.paFloat32,
                channels=1,
                rate=44100,
                input=True,
                input_device_index=device_idx,
                frames_per_buffer=frames
            )
            audio_data = stream.read(frames)
            stream.stop_stream()
            stream.close()
            
            # Convert bytes to float values
            rec_flat = list(struct.unpack('<' + 'f' * frames, audio_data))
        finally:
            pa.terminate()
    except Exception as e:
        progress.stop()
        calib_window.destroy()
        messagebox.showerror("Calibration Error", f"Could not record audio: {e}")
        return

    if rec_flat is None or len(rec_flat) == 0 or all(r == 0 for r in rec_flat):
        progress.stop()
        calib_window.destroy()
        messagebox.showerror("Calibration Error", "No audio data received. Please check your microphone.")
        return

    rec_clipped = clip(rec_flat, -1.0, 1.0)
    rms = math.sqrt(mean([r*r for r in rec_clipped]))
    ambient = -100 if rms < 1e-10 else 20 * math.log10(rms)
    
    if not math.isfinite(ambient):
        ambient = -100
    
    new_threshold = round(ambient + 10, 1)
    new_value = max(new_threshold, -50)
    
    if not math.isfinite(new_value):
        new_value = -30.0
    
    progress.stop()
    calib_window.destroy()
    
    thresh_scale.set(float(new_value))
    
    messagebox.showinfo("Calibration Complete", 
                       f"✅ Calibration Successful!\n\n"
                       f"🔇 Ambient: {ambient:.1f} dB\n"
                       f"🎯 Threshold: {new_value:.1f} dB\n\n"
                       f"The alert will now trigger when audio exceeds {new_value:.1f} dB")

controls_card = Card(main_frame)
controls_card.grid(row=3, column=0, sticky='ew', pady=(0, 10))
button_frame = controls_card.content_frame

start_btn = ttk.Button(button_frame, text="▶️ Start Monitoring", style='Success.TButton', command=start)
start_btn.pack(side='left', expand=True, fill='x', padx=(0, 5))

stop_btn = ttk.Button(button_frame, text="⏹️ Stop", state="disabled", style='Warning.TButton', command=stop)
stop_btn.pack(side='left', expand=True, fill='x', padx=(5, 5))

calib_btn = ttk.Button(button_frame, text="🎯 Auto Calibrate", style='TButton', command=calibrate)
calib_btn.pack(side='left', expand=True, fill='x', padx=(5, 5))

test_btn = ttk.Button(button_frame, text="🔊 Test Tone", style='Secondary.TButton',
                     command=lambda: play_tone(shape_var.get(), freq_var.get(), dur_var.get(), vol_var.get(), output_device_map_name_to_idx.get(output_dev_name_var.get())))
test_btn.pack(side='left', expand=True, fill='x', padx=(5, 0))

web_card = Card(main_frame, title="🌐 Remote Control")
web_card.grid(row=4, column=0, sticky='ew', pady=(0, 10))
web_content = web_card.content_frame
web_content.columnconfigure(1, weight=1)

web_enabled_var = tk.BooleanVar(value=cfg.get("web_enabled", True))
web_port_var = tk.IntVar(value=cfg.get("web_port", 8080))

def start_web_server():
    global web_server
    if web_server is None and web_enabled_var.get():
        try:
            port = web_port_var.get()
            web_server = WebServerThread(port=port)
            web_server.start()
            local_ip = get_local_ip()
            web_info_label.config(text=f"📱 Access at: http://{local_ip}:{port}", foreground=AppColors.SUCCESS)
            print(f"Web server started at http://{local_ip}:{port}")
            port_entry.config(state="disabled")
        except Exception as e:
            print(f"Failed to start web server: {e}")
            web_info_label.config(text="❌ Failed to start", foreground=AppColors.ERROR)

def stop_web_server():
    global web_server
    if web_server is not None:
        try:
            web_server.stop()
            web_server.join(timeout=2)
        except Exception as e:
            print(f"Error stopping web server: {e}")
        finally:
            web_server = None
            web_info_label.config(text="Remote access disabled.")
            port_entry.config(state="normal")

def on_web_toggle():
    if web_enabled_var.get():
        start_web_server()
    else:
        stop_web_server()

controls_frame = ttk.Frame(web_content, style='Card.TFrame')
controls_frame.pack(fill='x', expand=True, pady=(0, 5))

web_checkbox = ttk.Checkbutton(controls_frame, text="Enable remote control access", 
                              variable=web_enabled_var, command=on_web_toggle)
web_checkbox.pack(side='left')

port_frame = ttk.Frame(controls_frame, style='Card.TFrame')
port_frame.pack(side='left', padx=20)
ttk.Label(port_frame, text="Port:", style='Muted.Card.TLabel').pack(side='left')
port_entry = ttk.Entry(port_frame, textvariable=web_port_var, width=6, font=('Segoe UI', 10))
port_entry.pack(side='left', padx=5)


web_info_label = ttk.Label(web_content, text="", style='Card.TLabel', font=('Segoe UI', 10, 'bold'))
web_info_label.pack(fill='x', expand=True)

info_card = Card(main_frame, title="ℹ️ Information")
info_card.grid(row=5, column=0, sticky='ew')
info_content = info_card.content_frame

info_text = ttk.Label(info_content, 
                     text="🎯 Sets threshold 10dB above ambient noise.\n"
                          "🌐 Allows remote control from other devices.\n"
                          "💬 Supports voice messages through web interface.",
                     style='Muted.Card.TLabel', justify='left')
info_text.pack(fill='x')

q_cmd = queue.Queue()
q_ui = queue.Queue()
monitor = None
web_server = None
push_job = None

def poll_ui():
    try:
        while 1:
            tag, val = q_ui.get_nowait()
            if tag=="DB":
                vu_meter.update_level(val, th_var.get())
                shared_state.update_current_db(val)
            elif tag=="ERR":
                status_label.config(text="● ERROR", foreground=AppColors.ERROR)
                stop()
    except queue.Empty:
        pass
    
    start_req, stop_req = shared_state.check_web_requests()
    if start_req:
        start()
    if stop_req:
        stop()
    
    if abs(shared_state.threshold - th_var.get()) > 0.1:
        th_var.set(shared_state.threshold)
        thresh_value_label.config(text=f"{shared_state.threshold:.1f} dB")
        if 'vu_meter' in globals():
            vu_meter.threshold_db = shared_state.threshold
        schedule_push_settings()
    
    root.after(100, poll_ui)

def on_close():
    stop()
    stop_web_server()
    
    if 'tts_worker' in globals():
        tts_worker.stop()
    
    cfg_to_save = {
        "thresh": th_var.get(),
        "shape" : shape_var.get(),
        "freq"  : freq_var.get(),
        "dur"   : dur_var.get(),
        "vol"   : vol_var.get(),
        "web_enabled": web_enabled_var.get(),
        "web_port": web_port_var.get(),
    }
    selected_name = dev_name_var.get()
    device_idx_to_save = input_device_map_name_to_idx.get(selected_name)
    if device_idx_to_save is not None:
        cfg_to_save["device"] = device_idx_to_save
    
    selected_output_name = output_dev_name_var.get()
    output_idx_to_save = output_device_map_name_to_idx.get(selected_output_name)
    if output_idx_to_save is not None:
        cfg_to_save["output_device"] = output_idx_to_save
    
    save_cfg(cfg_to_save)
    root.destroy()

root.protocol("WM_DELETE_WINDOW", on_close)

if web_enabled_var.get():
    start_web_server()

poll_ui()
root.mainloop()
