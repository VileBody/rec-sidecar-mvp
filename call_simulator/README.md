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
- The app voices the seller line, generates the client reply, then voices the client line.

Seller and client use separate Inworld voices, so the simulator behaves like a complete two-sided call.
