# REC Coach Desktop

Native macOS client for the production REC Coach service. The application uses
Tauri 2 for the desktop shell, a bundled Sales-only frontend, and Rust for
authentication, session streaming, microphone/system-audio capture, AEC3 and
STT transport.

The same Rust core also builds a separate `REC Personal` product. It accepts
only `personal` accounts, keeps its own Keychain token and Application Support
cache, creates sessions without Sales coaching, and shows a source-separated
transcript with native recording controls. The web UI for the same account is
read-only and follows that desktop session over SSE/polling.

The center column streams an automatic answer after an interviewer question is
identified. The right-hand emergency button starts a separate Gemini request;
both answers can stream at the same time without canceling each other.

## Development

```bash
cd desktop
./scripts/prepare-ui.sh
cargo run --manifest-path src-tauri/Cargo.toml
```

Install dependencies once, then use the checked-in Tauri CLI and macOS bundle
script:

```bash
cd desktop
npm install
npm run tauri -- dev
npm run build:macos
npm run build:personal:macos
```

`npm run build:macos` produces `REC Coach.app` and an unsigned, unnotarized
DMG with an Applications link. It avoids Finder automation, so it also works in
CI and restricted desktop shells. In an interactive macOS session the standard
`npm run tauri -- build --bundles app,dmg` command remains available.

`npm run build:personal:macos` produces `REC Personal.app` and
`REC Personal_<version>_aarch64.dmg` with bundle identifier
`ru.teamgenius.rec-personal`. Both products require macOS 14.2 or newer for the
global Core Audio tap. Recording continues while the main window is minimized.

Run Rust and visual regression tests with:

```bash
cargo test --manifest-path src-tauri/Cargo.toml
npm run test:ui
npm run test:personal:ui
```

The desktop client always connects to `https://rec.teamgenius.ru`. Credentials
are stored in the macOS Keychain. The last session snapshot is cached under the
user's Application Support directory.

Runtime diagnostics are written without tokens or transcript text to:

```text
~/Library/Application Support/ru.TeamGenius.REC-Coach/logs/rec-coach.log
```

The same path is shown inside the in-app audio diagnostics panel.

Personal diagnostics use the isolated path:

```text
~/Library/Application Support/ru.TeamGenius.REC-Personal/logs/rec-personal.log
```

macOS 14.2 or newer is required for global system-audio capture.
