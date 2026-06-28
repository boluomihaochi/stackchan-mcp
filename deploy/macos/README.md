# macOS LaunchAgent examples

These files are templates for running Stack-chan host services under launchd.
Copy an example into `~/Library/LaunchAgents/`, remove the `.example` suffix,
then edit paths and environment values for your machine.

## Voice upload receiver

The voice upload receiver serves the phone/browser recorder page and accepts
`POST /voice/upload`.

```bash
mkdir -p ~/Library/LaunchAgents
cp deploy/macos/stackchan-voice-upload.plist.example \
  ~/Library/LaunchAgents/xyz.example.stackchan-voice-upload.plist

# Edit:
# - /path/to/stackchan
# - /path/to/stackchan/.venv/bin/python
# - any EnvironmentVariables you want launchd to own

plutil -lint ~/Library/LaunchAgents/xyz.example.stackchan-voice-upload.plist
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/xyz.example.stackchan-voice-upload.plist
launchctl kickstart -k "gui/$(id -u)/xyz.example.stackchan-voice-upload"
```

Check it:

```bash
launchctl print "gui/$(id -u)/xyz.example.stackchan-voice-upload"
curl -fsS http://127.0.0.1:8767/health
```

Do not put real API keys or upload tokens in this example file before
committing. Prefer a local `.env` loaded by the project scripts, or edit only the
copy under `~/Library/LaunchAgents/`.
