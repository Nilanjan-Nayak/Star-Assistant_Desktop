import os
import re
import time
import threading
import pygame
from PySide6.QtCore import QObject, Signal, QThread, QTimer

from .brain import AssistantBrain


class VoiceListener(QObject):
    """Background microphone worker for hands-free Bengali and English speech recognition."""

    # Common English words used to decide which transcript is the real one.
    _EN_WORDS = {
        "open", "launch", "close", "play", "search", "volume", "sound", "mute", "unmute",
        "brightness", "how", "are", "you", "who", "what", "is", "your", "my", "name",
        "calculate", "screenshot", "lock", "the", "a", "an", "please", "can", "could",
        "would", "tell", "me", "time", "date", "today", "tomorrow", "weather", "news",
        "stop", "start", "set", "increase", "decrease", "take", "read", "screen",
        "song", "music", "video", "on", "for", "to", "hello", "hi", "hey", "thanks",
        "thank", "good", "morning", "night", "remember", "remind", "when", "where",
        "why", "which", "do", "does", "did", "i", "we", "it", "this", "that", "and",
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self._running = True
        self._paused = False
        self._last_resume_time = 0.0
        self._thread = threading.Thread(target=self._run, daemon=True, name="voice-listener")
        self._thread.start()

    @classmethod
    def _prefer_english(cls, t_en: str, t_bn: str) -> bool:
        """Score both transcripts; prefer English when it looks genuinely English.

        The Bengali (bn-IN) recognizer often transliterates English speech into
        Bengali script, which then matches nothing.  If the English transcript
        contains ≥2 recognised common English words we trust it; otherwise keep
        the Bengali one (our NLU handles Bengali script and Banglish natively).
        """
        words = re.findall(r"[a-zA-Z']+", t_en.lower())
        if not words:
            return False
        hits = sum(1 for w in words if w in cls._EN_WORDS)
        if hits >= 2:
            return True
        # Short single-word commands ("play", "stop"…) — also trust English.
        if len(words) <= 2 and words[0] in cls._EN_WORDS:
            return True
        return False

    def pause(self):
        self._paused = True

    def resume(self):
        self._last_resume_time = time.time()
        self._paused = False

    def stop(self):
        self._running = False

    def _run(self):
        # FIX: any unexpected crash used to kill the listener thread silently —
        # the assistant then never heard anything again until restart.  Now the
        # loop self-heals with a short backoff.
        while self._running:
            try:
                self._run_once()
            except Exception as e:
                print(f"[VoiceListener] Continuous mic loop error: {e}")
                self.log_status.emit(f"[Voice Error] {e} — আবার চালু হচ্ছে...", "alert")
            if self._running:
                time.sleep(1.5)  # brief backoff before restarting the mic loop

    def _run_once(self):
        try:
            import speech_recognition as sr
            import concurrent.futures
            recognizer = sr.Recognizer()
            # Disable dynamic threshold drift so microphone never goes deaf over time
            recognizer.dynamic_energy_threshold = False
            recognizer.energy_threshold = 240
            recognizer.pause_threshold = 0.65  # Snappy pause detection (reduces waiting time)
            recognizer.phrase_threshold = 0.2
            recognizer.non_speaking_duration = 0.35
            mic = sr.Microphone()
        except Exception as e:
            print(f"[VoiceListener] Microphone init warning: {e}")
            self.log_status.emit(f"[Mic Error] {e}", "alert")
            time.sleep(3.0)
            return

        with mic as source:
            self.log_status.emit("[Voice] Calibrating ambient noise...", "cyan")
            recognizer.adjust_for_ambient_noise(source, duration=0.6)
            self.log_status.emit("[Voice] Google Recognizer চালু আছে (বাংলা/English)...", "ok")

            while self._running:
                if self._paused:
                    time.sleep(0.08)
                    continue

                try:
                    audio = recognizer.listen(source, timeout=1.8, phrase_time_limit=12)
                except sr.WaitTimeoutError:
                    continue
                except Exception as e:
                    time.sleep(0.1)
                    continue

                # If paused while listening, discard immediately
                if not self._running or self._paused:
                    continue

                # If unpaused within last 0.4s, ignore any residual room echo
                if (time.time() - self._last_resume_time) < 0.4:
                    continue

                self.speech_started.emit()

                # Run Google bn-IN and en-IN concurrently for 2x faster recognition
                def _query_google(lang):
                    try:
                        return recognizer.recognize_google(audio, language=lang)
                    except Exception:
                        return None

                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                    f_bn = executor.submit(_query_google, "bn-IN")
                    f_en = executor.submit(_query_google, "en-IN")
                    t_bn = f_bn.result()
                    t_en = f_en.result()

                # Smart selection between Bengali and English
                text = None
                if t_bn and t_en:
                    text = t_en if self._prefer_english(t_en, t_bn) else t_bn
                elif t_bn:
                    text = t_bn
                elif t_en:
                    text = t_en

                if not self._running or self._paused:
                    continue

                if text and text.strip():
                    clean = text.strip()
                    self.log_status.emit(f"[Google STT] শোনায় পেলাম: '{clean}'", "cyan")
                    clean = re.sub(r"^(?:bhai|bhaiya|dada|please|ektu|zara|shono|suno|star|স্টার|hey star)\s+", "", clean, flags=re.IGNORECASE).strip(" ,.-")
                    target = clean if clean else "hey star"
                    self.utterance_recognized.emit(target)


class SpeechPlayer(QObject):
    """Reliable SDL2/pygame-backed speech player that avoids Qt multimedia audio buffer dropouts."""

    started = Signal()
    finished = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._thread = None
        self._is_playing = False
        self._stop_event = threading.Event()
        try:
            if not pygame.mixer.get_init():
                pygame.mixer.init()
        except Exception as e:
            print(f"[SpeechPlayer] Pygame mixer init warning: {e}")

    def is_playing(self) -> bool:
        return self._is_playing

    def stop(self):
        self._stop_event.set()
        try:
            if pygame.mixer.get_init():
                pygame.mixer.music.stop()
        except Exception:
            pass
        self._is_playing = False

    def play_file(self, file_path: str):
        self.stop()
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._play_worker, args=(file_path,), daemon=True)
        self._thread.start()

    def _play_worker(self, file_path: str):
        try:
            if not pygame.mixer.get_init():
                pygame.mixer.init()
            pygame.mixer.music.load(file_path)
            self._is_playing = True
            self.started.emit()
            pygame.mixer.music.play()

            while pygame.mixer.music.get_busy() and not self._stop_event.is_set():
                time.sleep(0.04)

            if not self._stop_event.is_set():
                # Echo settling delay so room reflections don't re-trigger the microphone
                time.sleep(0.35)
        except Exception as e:
            print(f"[SpeechPlayer] Audio playback warning: {e}")
        finally:
            self._is_playing = False
            self.finished.emit()


class BrainWorker(QThread):
    """Background worker executing brain reasoning and TTS synthesis."""

    thinking_started = Signal()
    action_performed = Signal(dict)
    response_ready = Signal(dict)

    def __init__(self, brain: AssistantBrain, query: str, parent=None):
        super().__init__(parent)
        self.brain = brain
        self.query = query

    def run(self):
        self.thinking_started.emit()
        try:
            result = self.brain.process(self.query)
        except Exception as e:
            # FIX: an unhandled exception used to kill this QThread silently —
            # neither response_ready nor speaking_finished fired, so the mic
            # stayed paused forever and Star appeared dead.  Always answer.
            print(f"[BrainWorker] Brain error: {e}")
            result = {
                "response": "মাফ করো বন্ধু, ভেতরে একটু ঝামেলা হয়েছিল। এখন আমি ঠিক আছি — আবার বলো তো!",
                "audio_path": None,
                "actions": [],
                "source": "error_recovery",
            }
        try:
            for act in result.get("actions", []):
                self.action_performed.emit(act)
            self.response_ready.emit(result)
        except Exception as e:
            print(f"[BrainWorker] Signal emission error: {e}")


class AssistantBridge(QObject):
    """Bridge linking the StarWindow and Reactor to the Assistant Brain."""

    state_changed = Signal(str)            # 'idle', 'hear', 'think', 'act', 'speak'
    log_emitted = Signal(str, str)         # (text, color_tag: 'cyan' | 'gold' | 'ok' | 'alert')
    response_ready = Signal(str)           # Final text response
    speaking_started = Signal()
    speaking_finished = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.brain = AssistantBrain()
        if hasattr(self.brain, "agent"):
            self.brain.agent.add_log_listener(self._on_agent_log)

        # Rock-solid Pygame mixer audio player
        self.player = SpeechPlayer(self)
        self.player.started.connect(self._on_playback_started)
        self.player.finished.connect(self._on_playback_finished)

        # Keep strong references to running brain workers (GC safety)
        self._workers = set()
        self._query_seq = 0

        self._active_worker = None

        # Continuous voice listener thread
        self.voice_listener = VoiceListener(self)
        self.voice_listener.speech_started.connect(self._on_voice_speech_started)
        self.voice_listener.utterance_recognized.connect(self._on_voice_utterance)
        self.voice_listener.log_status.connect(self._on_listener_log)

    def _on_agent_log(self, text: str, tag: str):
        self.log_emitted.emit(text, tag)
        if any(m in text for m in ["[Agent Task]", "[Skill]", "[Vision]"]):
            self.state_changed.emit("act")

    def _on_listener_log(self, text: str, tag: str):
        self.log_emitted.emit(text, tag)

    def _on_playback_started(self):
        self.voice_listener.pause()
        self.state_changed.emit("speak")
        self.speaking_started.emit()

    def _on_playback_finished(self):
        # If a brain query is being processed (e.g. speech was interrupted by a
        # new command), leave state & mic alone — the query's own response
        # handler manages them.  Otherwise finish speaking cleanly.
        worker = getattr(self, "_active_worker", None)
        if worker is not None and worker.isRunning():
            return
        self.state_changed.emit("idle")
        self.speaking_finished.emit()
        self.voice_listener.resume()

    def stop_speaking(self):
        """Immediately stop speaking audio and return to idle."""
        if hasattr(self, "player") and self.player.is_playing():
            self.player.stop()

    def _on_voice_speech_started(self):
        # Prevent self-interruption from speaker echo
        if self.player.is_playing():
            return
        self.state_changed.emit("hear")

    def _on_voice_utterance(self, text: str):
        self.send_query(text)

    def shutdown(self):
        """Cleanly terminate background workers."""
        if hasattr(self, "brain") and hasattr(self.brain, "agent"):
            self.brain.agent.remove_log_listener(self._on_agent_log)
        if hasattr(self, "voice_listener") and self.voice_listener:
            self.voice_listener.stop()
        if hasattr(self, "player") and self.player:
            self.player.stop()

    def send_query(self, query: str):
        """Submit a user prompt (voice or text) to the brain."""
        if not query or not query.strip():
            return

        # Stop any active speech immediately when new query arrives
        self.stop_speaking()

        # Pause mic while thinking to prevent ambient noise or keyboard clicks triggering STT
        self.voice_listener.pause()

        self.log_emitted.emit(f"▸ {query}", "cyan")
        self.state_changed.emit("think")
        self._query_seq += 1

        # Launch background worker.
        # FIX: workers are kept alive until they finish — replacing the only
        # reference mid-run used to let Python GC the QThread while running
        # ("QThread: Destroyed while thread is still running" crash).
        worker = BrainWorker(self.brain, query, self)
        worker.action_performed.connect(self._on_action_performed)
        worker.response_ready.connect(self._on_response_ready)
        worker.finished.connect(lambda w=worker: self._workers.discard(w))
        self._workers.add(worker)
        self._active_worker = worker
        worker.start()

    def _on_action_performed(self, action: dict):
        tool_name = action.get("tool", "unknown")
        args = action.get("args", {})
        self.state_changed.emit("act")
        self.log_emitted.emit(f"[ACT] Executed: {tool_name}({args})", "gold")

    def _on_response_ready(self, result: dict):
        response_text = result.get("response", "")
        audio_path = result.get("audio_path")
        source = result.get("source", "brain")

        self.log_emitted.emit(f"[STAR // {source}] {response_text}", "ok")
        self.response_ready.emit(response_text)

        # Play spoken audio if available
        if audio_path and os.path.exists(audio_path):
            self.player.play_file(audio_path)
        else:
            # If no audio, flash "speak" briefly on the UI thread.
            # FIX: time.sleep(0.8) here froze the whole Qt GUI for 0.8s after
            # every response; a QTimer keeps the interface responsive and the
            # mic resume stays guaranteed.
            self.state_changed.emit("speak")
            seq = self._query_seq
            QTimer.singleShot(800, lambda: self._finish_silent_response(seq))

    def _finish_silent_response(self, seq: int):
        if seq != self._query_seq:
            return  # a newer query took over — it manages the mic itself
        self.state_changed.emit("idle")
        self.speaking_finished.emit()
        self.voice_listener.resume()

