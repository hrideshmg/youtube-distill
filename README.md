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
