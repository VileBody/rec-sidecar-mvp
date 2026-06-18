# Call Simulator

Small desktop simulator for testing the Clean Start coach with a synthetic client.

## Run

```bash
cd /Users/ergin/Desktop/rec-sidecar-mvp
./call_simulator/run.sh
```

The app loads `.env` and `.env.iac`, uses the existing `zai-glm-4.7` client actor and Inworld TTS helpers, and writes logs/audio to `logs/call_simulator/<timestamp>/`.

## Flow

- Read or copy the opener from the app.
- Paste what the seller said into the right textarea.
- Press `Клиент отвечает`.
- The app generates the client reply, saves a WAV, and plays only the client voice.

This keeps seller context textual while the played system audio belongs to the simulated client.
