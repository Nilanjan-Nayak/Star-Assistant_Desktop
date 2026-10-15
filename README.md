<div align="center">

# ✨ STAR ASSISTANT // DESKTOP
### Futuristic Bengali + English AI desktop companion with a type-safe automation core

<p>
  <img alt="Python" src="https://img.shields.io/badge/Python-3.12%2B-7dd3fc?style=for-the-badge&logo=python&logoColor=white">
  <img alt="Status" src="https://img.shields.io/badge/Status-Beta-22d3ee?style=for-the-badge">
  <img alt="License" src="https://img.shields.io/badge/License-MIT-a78bfa?style=for-the-badge">
</p>

<p>
  <strong>Voice-first desktop assistant + modular automation agent</strong><br/>
  Responsive HUD • OCR vision • memory-aware planning • safety-governed actions
</p>

</div>

---

## 🌌 Why this project feels different

Star Assistant blends a cinematic desktop experience with practical automation:

- **3D-style reactive HUD UI** built with **PySide6** (`Frontend/`)
- **Voice loop** (listen → reason → respond) with Bengali/English speech handling (`Backend/`)
- **Type-safe automation core** with layered architecture, strict typing, and safety invariants (`agent/`)
- **Memory-aware behavior** so repeated tasks can become personalized over time

---

## 🧩 Experience & architecture at a glance

<table>
  <tr>
    <td valign="top" width="33%">
      <h3>🪐 Frontend (Face)</h3>
      <ul>
        <li>PySide6 HUD modes and animation system</li>
        <li>Reactor-style visual states (hear/think/act/speak)</li>
        <li>Main entry: <code>Frontend/main.py</code></li>
      </ul>
    </td>
    <td valign="top" width="33%">
      <h3>🧠 Backend (Brain)</h3>
      <ul>
        <li>Bridge between UI, voice, tools, and reasoning</li>
        <li>LLM provider routing (local-first, optional Gemini)</li>
        <li>Tool registry for apps/system/web/media workflows</li>
      </ul>
    </td>
    <td valign="top" width="33%">
      <h3>🛡️ Agent Core (Engine)</h3>
      <ul>
        <li>Safety governor + capability checks</li>
        <li>Skill system (volume, brightness, launch, OCR, screenshot, YouTube)</li>
        <li>CLI + memory + dry-run automation pipeline</li>
      </ul>
    </td>
  </tr>
</table>

---

## 🚀 Quick start

### 1) Run the desktop assistant (HUD + voice)

```bash
pip install -r requirements.txt
python run.py
```

Windows helper:

```bat
run.bat
```

---

### 2) Run the type-safe automation agent (CLI)

```bash
pip install -e ".[dev]"
pytest
agent --describe --dry-run
agent --dry-run "set the volume"
```

> Python requirement: **3.12+**

---

## ⚙️ Optional capabilities

### OCR / screen reading

```bash
# Debian/Ubuntu
sudo apt install tesseract-ocr tesseract-ocr-ben
pip install -e ".[gui,ocr]"
```

### Memory embeddings (optional)

```bash
pip install -e ".[memory]"
```

---

## 🧪 Useful CLI examples

```bash
agent --health
agent --skill volume --params '{"level":40}' --dry-run
agent --see --query Search
agent --remember "volume at night is 30" --remember-kind preference --remember-key volume.level --remember-value 30
agent --recall volume
agent --backup ./drive-folder
```

---

## 🔧 Configuration notes

`Backend/config.py` supports environment-based configuration.

### LLM routing
- `STAR_LOCAL_LLM_URL` (default `http://localhost:11434/v1`)
- `STAR_LOCAL_LLM_MODEL` (default `star-local:latest`)
- `STAR_USE_LOCAL_LLM` (default `true`)
- `GEMINI_API_KEY`
- `STAR_USE_GEMINI` (default `false`)
- `STAR_GEMINI_MODEL` (default `gemini-2.5-flash`)

### Voice output
- `STAR_TTS_VOICE`
- `STAR_TTS_RATE`
- `STAR_TTS_PITCH`

### Optional integrations
- `YOUTUBE_API_KEY` (used by YouTube analytics tool paths)

---

## 🗂️ Project structure

```text
.
├── run.py / run.bat          # Desktop launcher
├── Frontend/                 # PySide6 visual layer (HUD)
├── Backend/                  # Brain, bridge, voice, tools, LLM providers
├── agent/                    # v6 automation core (typed layered architecture)
├── tests/                    # Core tests for agent package
├── requirements.txt          # Full desktop stack dependencies
└── pyproject.toml            # Agent package metadata, extras, lint/test config
```

---

## 🛣️ Roadmap

- [ ] Expand desktop presence modes and UI polish in the Frontend layer
- [ ] Continue wiring richer tool/mission flows through the bridge
- [ ] Grow skill coverage while preserving safety-governed execution
- [ ] Keep strict typing, testability, and dry-run reliability at the core

---

## 🤝 Contributing

Contributions are welcome via pull requests and issues.

1. Fork the repository
2. Create a feature branch
3. Run relevant checks (for agent core: `pytest`)
4. Open a PR with a clear description

---

## 📜 License

This repository is licensed under the **MIT License**.
See [`LICENSE`](./LICENSE).

---

## 📡 Support / contact

For bugs, ideas, or feature requests, please open an issue in this repository.

<div align="center">
  <sub>Built for a premium desktop-assistant experience with practical automation depth.</sub>
</div>
