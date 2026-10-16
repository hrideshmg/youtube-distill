# youtube-distill

Turn an educational YouTube video into a standalone article, with screenshots pulled from the video.

## Run

Requires [uv](https://docs.astral.sh/uv/) and `ffmpeg`.

```sh
echo 'OPENAI_API_KEY=sk-...' > .env
uv run --env-file .env uvicorn app.main:app
```

Open http://localhost:8000.

The model defaults to `gpt-6-luna`; set `OPENAI_MODEL` to override.

### YouTube cookies

If YouTube answers "Sign in to confirm you're not a bot" (common on server IPs), give yt-dlp logged-in cookies:

- `YT_COOKIES_BROWSER=firefox` reads cookies from a local browser profile.
- `YT_COOKIES_FILE=/path/to/cookies.txt` uses a Netscape-format cookies file, for headless servers. The file must be writable.

### PO tokens

On a flagged server IP, YouTube may still withhold captions ("YouTube returned no caption tracks"), because its web client needs a PO token for subtitles. The [bgutil-ytdlp-pot-provider](https://github.com/Brainicism/bgutil-ytdlp-pot-provider) plugin is a dependency. To give it something to run, clone its provider (script mode) and install it with deno, which must be on `PATH`:

```sh
git clone --single-branch --branch 2.0.2 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git ~/bgutil-ytdlp-pot-provider
cd ~/bgutil-ytdlp-pot-provider/server && deno install --allow-scripts=npm:canvas --frozen
```

If the app runs as a different user than the one whose home holds the checkout, set `BGUTIL_SERVER_HOME=/path/to/bgutil-ytdlp-pot-provider/server`.
