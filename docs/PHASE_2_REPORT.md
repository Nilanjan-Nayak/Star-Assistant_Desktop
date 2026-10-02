# STAR 2.0 — PHASE 2 REPORT
### Voice: STT + TTS + Bengali/English/Banglish routing + barge-in

*Branch `star-2.0` · base `star-2.0-phase-1` · blueprint §7 Phase 2*

> **Phase 2 requirement:** "define `STTProvider` and `TTSProvider` interfaces; add Bengali/English
> language routing; support interruption/barge-in; keep speaker verification separate from STT;
> make transcript retention configurable."

---

## 1. What was built (7 new modules, ~1 350 lines, additive only)

| Module | Lines | Purpose |
|---|---|---|
| `Backend/star/voice/base.py` | 120 | `STTProvider` / `TTSProvider` protocols, `AudioInput`, `Transcript` (+ `redacted()`), `SynthesisResult` |
| `Backend/star/voice/language.py` | 250 | detection (Bengali script / Banglish / English / code-switched), normalisation, wake-word stripping, response-language policy, STT locale order, TTS voice choice, `prefer_english()` |
| `Backend/star/voice/stt.py` | 330 | `WhisperSTT` (local, offline) · `GoogleDualSTT` (bn-IN + en-IN in parallel) · `BrowserSTT` (Web Speech API) · `FileSTT` (sidecar) · `ScriptedSTT` (tests) · `NullSTT` · `STTChain` · `build_stt()` |
| `Backend/star/voice/tts.py` | 200 | `EdgeTTSAdapter` **over the existing `Backend/voice/tts.VoiceEngine`** · `ScriptedTTS` (stdlib WAV) · `NullTTS` · `_TTSCascade` · `build_tts()` |
| `Backend/star/voice/speaker.py` | 230 | `SpeakerVerifier` protocol, `NullSpeakerVerifier`, `NumpySpeakerVerifier` (energy/ZCR/centroid profile), dependency-free WAV parser |
| `Backend/star/voice/pipeline.py` | 290 | `VoicePipeline`: hear → transcript → language → speak, barge-in generations, retention policy, `voice.*` verbs |
| `Backend/star/voice/__init__.py` | 60 | public surface (33 exports) |

Modified (additive): `config/settings.py` (+`auto_speak`), `gateway/api.py` (+`POST /api/v1/voice`),
`main.py` (language routing in `chat()`, voice pipeline auto-built, `_maybe_speak`).

**Nothing in `Frontend/`, `agent/`, `Backend/voice/`, `Backend/bridge.py` or `Backend/nlu/` changed.**
The existing `VoiceEngine` and the `VoiceListener` heuristic are *reused*, not forked.

---

## 2. Language routing (the Bengali-first brain)

Detection is pure, deterministic and <1 ms. Verified matrix (from `tests/test_star2_voice.py`):

| Input | code | script | Banglish | Star answers in |
|---|---|---|---|---|
| `আমার ভলিউম একটু বাড়াও` | `bn` | bengali | – | Bengali |
| `স্ক্রিনে কী আছে দেখো` | `bn` | bengali | – | Bengali |
| `volume ta ektu barao` | `bn` | latin | ✔ | Bengali |
| `gaan chalaao` | `bn` | latin | ✔ | Bengali |
| `screen e ki ache dakho` | `bn` | latin | ✔ | Bengali |
| `kemon acho?` | `bn` | latin | ✔ | Bengali |
| `play some lo-fi music` | `en` | latin | – | English |
| `what is on my screen` | `en` | latin | – | English |
| `volume টা ৪০ এ set koro` | `mixed` | mixed | – | user default (bn) |

Also: **Bengali digits → ASCII** (`ভলিউম ৪০ কোরো` → `ভলিউম 40 কোরো`), token-based **wake-word
stripping** that works across scripts (`হেই star, …` → `…`) and never eats a message that *is* the
wake word (`"star"` stays `"star"`, `"hello star"` stays intact).

A 122-entry weighted **Banglish lexicon** drives romanised-Bengali detection; tech words that appear
in both languages (`volume`, `screen`, `play`) are weight 0 so they cannot flip the decision.

**Dual-locale disambiguation** (`prefer_english`) is lifted as a pure function from
`Backend/bridge.py::VoiceListener._prefer_english` — same rule (≥2 common English words, or a 1–2
word English command), now unit-testable, while the Qt class stays untouched.

---

## 3. Provider model & graceful degradation

```
STT  auto → WhisperSTT(local) → GoogleDualSTT(network) → BrowserSTT → FileSTT → NullSTT
TTS  auto → EdgeTTSAdapter(existing VoiceEngine, edge-tts + bn/en chain + mp3 cache) → NullTTS
```

* Every provider implements `available()` / `supports(language)`; `STTChain` and `_TTSCascade`
  **skip unavailable providers and swallow their exceptions**, so a machine without `edge-tts`,
  `faster-whisper` or `speech_recognition` still runs (text-only Star).
* Blocking recognisers/synthesisers run through `asyncio.to_thread` — the existing `VoiceEngine`
  calls `asyncio.run()` internally, which would otherwise crash the gateway loop.
* Optional imports are lazy: importing the voice package never requires numpy/whisper/edge-tts.

**Speaker verification is a separate seam** (`voice/speaker.py`), gated by
`STAR_VOICE_SPEAKER_VERIFY` (default off). It is explicitly *not* a security control — permission
identity lives in Phase 11 `security/identity.py`. The default implementation is a dependency-light
heuristic (frame energy + ZCR + spectral centroid/roll-off → cosine vs an enrolled profile); swapping
in a real speaker-embedding model needs no other change.

---

## 4. Barge-in (interruption)

```
speech_started / voice.barge_in
        │
        ├─ generation += 1                  ← any synthesis started for the old generation is dropped
        ├─ stop_playback()                  ← HUD: SpeechPlayer.stop() / console: audio.pause()
        ├─ cancel_current("barge_in:…")     ← orchestrator CancellationToken (Phase 10 wires the real one)
        └─ publish task.cancelled
```

* `STAR_VOICE_BARGE_IN=false` disables it (then `barge_in()` returns `ok: false`).
* A `speak()` that finishes **after** the generation changed returns
  `ok=false, error="interrupted"` with the audio stripped — Star never talks over the user.
* Tested with a deliberately slow TTS provider and a mid-flight interruption.

---

## 5. Privacy: transcript retention is configurable and off by default

`STAR_VOICE_KEEP_TRANSCRIPTS=false` (default) ⇒

* `language.detected` events carry `text: "<redacted:<sha256-12>>"` and `retained: false`
* `response.spoken` events carry `text: "<NN chars>"` instead of the reply
* `voice.transcribe` replies return a `Transcript.redacted()` copy
* audio bytes are **never** stored; only a 16-hex fingerprint (`AudioInput.fingerprint`) is kept

With the flag on, full text flows through (needed for training-data collection, which is the point of
`data/*.jsonl` in this repo).

---

## 6. New API surface

```
POST /api/v1/voice     {"type": "voice.transcribe", "text": "..."}            # browser STT result
                       {"type": "voice.transcribe", "audio_base64": "...", "format": "wav"}
                       {"type": "voice.synthesize", "text": "...", "language": "bn|en"}
                       {"type": "voice.barge_in",   "reason": "..."}
                       {"type": "voice.speech_started"}
                       {"type": "voice.enrol",      "speaker_id": "...", "audio_base64": "..."}
                       {"type": "voice.status"}
WS   /ws               same verbs, streamed
GET  /api/v1/chat      now returns "language_profile" {code, script, banglish, confidence, spoken_form, matched_tokens}
```

New events: `language.detected`, `response.spoken`, `task.cancelled` (voice stage), `task.failed`
(stt/tts stage), `memory.updated` (speaker enrolment).

`STAR_VOICE_AUTO_SPEAK=false` by default: the desktop HUD owns playback (its `SpeechPlayer` already
works); the gateway only synthesises when asked, or when auto-speak is explicitly enabled.

---

## 7. Changed files (`git diff --name-status star-2.0-phase-1`)

```
M  Backend/star/config/settings.py      (+2  auto_speak flag)
M  Backend/star/gateway/api.py          (+14 POST /api/v1/voice)
M  Backend/star/main.py                 (+52 language routing, voice auto-build, _maybe_speak)
A  Backend/star/voice/{__init__,base,language,pipeline,speaker,stt,tts}.py   (1 340 lines)
A  tests/test_star2_voice.py            (42 tests)
M  tests/test_star2_gateway.py          (+2 tests: language profile, voice endpoint)
A  docs/PHASE_2_REPORT.md
```

---

## 8. Test results

```
$ python -m pytest
1 failed, 203 passed in 31.01s        ← 204 total (was 160 after Phase 1; +44 this phase)

$ python -m pytest tests/test_star2_voice.py
42 passed in 1.33s

$ python scripts/smoke.py
[smoke] 13 passed, 0 failed
```

The only failure remains the **pre-existing** `test_command_router.py::test_google_search`
(tool renamed `search_web` → `web_search`; scheduled for Phase 12).

New coverage: language matrix (10 cases + mixed script) · normalisation/wake-word edge cases ·
`prefer_english` parity with the legacy heuristic · response-language policy · locale order ·
voice selection + fallback chain · all STT providers incl. chain fall-through on exceptions ·
base64 audio decoding (incl. data-URI and strict validation) · WAV writer/parser round-trip ·
TTS providers + cascade fallback + edge-tts absence · speaker enrol/verify/forget ·
retention redaction (on and off) · STT failure isolation · barge-in generation semantics ·
in-flight interruption drops audio · all `voice.*` verbs · pipeline health/startup.

---

## 9. Risks

1. **Heuristic Banglish detection.** A 122-word lexicon cannot cover every romanisation
   (`bhalo`/`valo`, `chhobi`/`chobi` are both listed, but new spellings appear constantly).
   Mitigation: confidence is published with every decision, `mixed` falls back to the user's
   configured default (Bengali), and Phase 9 learning can add tokens from corrections.
2. **Network STT.** `GoogleDualSTT` uses the free Google Web Speech endpoint (as the existing
   product does). Offline machines fall back to Whisper (if installed) or `NullSTT` — Star stays
   usable text-first. No API key is required or stored.
3. **Speaker verification is not authentication.** The profile matcher is a personalisation seam;
   it must never gate HIGH/CRITICAL actions on its own (Phase 11 keeps confirmation as the control).
4. **TTS latency.** edge-tts is a network call; `auto_speak` is therefore off by default and the
   existing mp3 cache in `Backend/cache/audio/` is reused through `VoiceEngine`.
5. **Microphone capture is still Qt-side.** The gateway accepts audio pushed to it
   (`voice.transcribe` + base64) but does not open a microphone — capture stays in the existing
   `VoiceListener` so the HUD keeps working unchanged. A headless capture provider can be added
   behind `STTProvider` later without touching this layer.

## 10. Verification checklist

- [x] `STTProvider` / `TTSProvider` interfaces defined and implemented by 6 + 4 providers
- [x] Bengali + English + Banglish + mixed routing works end-to-end through `/api/v1/chat`
- [x] Barge-in stops playback, cancels the in-flight generation, is configurable
- [x] Speaker verification is a separate module and disabled by default
- [x] Transcript retention configurable, defaults to redaction
- [x] Existing voice code reused (`VoiceEngine`, `prefer_english`), none rewritten
- [x] No new hard dependency (numpy/whisper/edge-tts/speech_recognition all optional)
- [x] 44 new tests green; whole suite green apart from the known pre-existing failure
- [x] Smoke test 13/13; frontend and `agent/` untouched
- [x] Committed as one milestone, tagged `star-2.0-phase-2`

## 11. Next: exact Phase 3 scope (Brain)

1. `brain/schemas.py` — `UserRequest`, `Context`, `Plan`, `Task`, `ToolCall`, `Verification`, `TaskResult`.
2. `brain/context.py` — build `Context` from session + working memory + retrieval + screen summary; **retrieve memory before planning**.
3. `brain/reasoning.py` — reasoning over `Backend/llm/provider.get_llm_provider()` (Ollama/Gemini/offline) with a deterministic fallback; **raw model output never executes anything**.
4. `brain/planning.py` — produce a structured `Plan` of `Task`s with agent assignment + task state; reuse `agent.planning` FSM vocabulary.
5. `brain/prediction.py` — likely-next-action proposals from history; every prediction still passes policy (Phase 11).
6. `brain/reflection.py` — post-execution critique: expected vs observed, retry/recover recommendation.
7. `main.py` — replace the Phase 1 legacy fallback with the real `StarBrain` pipeline; keep the router as a fast-path intent source.
8. Tests: schema validation, plan shape, memory-before-planning order, reflection on failure, prediction never bypasses policy, LLM-output sanitisation.
