# Clip Maker (Website)

Browser me video upload karo -> AI best moments chunta hai -> 9:16 captioned clips download karo.
Limit: 5 clips/din per visitor (cookie + IP dono se check hota hai). Login ki zarurat nahi.

## Structure

- `app.py` - backend (FastAPI): upload, queue, Whisper, Claude, FFmpeg, daily limit
- `static/index.html` - website ka frontend
- `Dockerfile`, `docker-compose.yml` - deploy ke liye

## Zaroori baat

GitHub sirf code store karta hai. **GitHub Pages par ye website nahi chalegi**, kyunki backend
(Python + FFmpeg + Whisper) ko server chahiye. Flow: GitHub par code -> server par deploy.

## 1. GitHub par daalo

```
git init
git add .
git commit -m "clip website"
git branch -M main
git remote add origin https://github.com/<username>/<repo>.git
git push -u origin main
```
`.env` push mat karna (`.gitignore` me block hai).

## 2. Deploy

**A. VPS (recommended)** - Hetzner/Contabo/DigitalOcean, 4 GB RAM:
```
git clone https://github.com/<username>/<repo>.git
cd <repo>
cp .env.example .env      # ANTHROPIC_API_KEY bharo
docker compose up -d --build
```
Website khulegi: `http://<server-ip>`. Update: `git pull && docker compose up -d --build`.

**Domain + HTTPS (free):** domain ka A record server IP par lagao, phir Caddy lagao
(`sudo apt install caddy`) aur `/etc/caddy/Caddyfile` me:
```
tumhara-domain.com {
    reverse_proxy localhost:80
}
```
Phir `.env` me `TRUST_PROXY=1` karo aur `docker compose up -d`.

**B. Render / Railway / Fly.io** - GitHub repo connect karo, "Web Service" chuno (Dockerfile detect hoga).
Env var `ANTHROPIC_API_KEY` daalo, `TRUST_PROXY=1` karo, aur `/data` par persistent disk lagao
(warna daily limit aur clips reset ho jayenge). Free plan me RAM kam hoti hai, paid chahiye.

## Local chalana

1. FFmpeg install karo, Python 3.10+
2. `pip install -r requirements.txt`
3. `.env.example` -> `.env`, key bharo
4. `uvicorn app:app --reload` aur `http://localhost:8000` kholo

## Settings (`app.py` ke upar)

`DAILY_LIMIT`, `IP_DAILY_LIMIT`, `MAX_CLIPS_PER_VIDEO`, `MAX_UPLOAD_MB`, `MAX_VIDEO_MIN`, `KEEP_HOURS`.

## Dhyan rakhne wali baatein

- Ek time par ek hi video process hoti hai, baaki line me rehti hain. Users badhein to workers/queue badhao.
- Cloudflare free plan 100 MB se bade uploads rokta hai, `MAX_UPLOAD_MB` usi hisaab se rakho.
- Server ka kharcha (CPU/RAM/bandwidth) aur Claude API ka bill tumhara hoga, limit isiliye hai.
- Sirf wahi videos process karo jinke rights ho.
