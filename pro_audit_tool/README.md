# PRO Audit Tool

Standalone tool to search ASCAP and BMI databases for song registration verification.

## Quick Start

```bash
./run.sh
```

Then open http://localhost:8080

## Manual Setup

```bash
# Create venv
python3 -m venv venv
source venv/bin/activate

# Install deps
pip install -r requirements.txt

# Install Playwright browsers
playwright install chromium

# Run
python server.py
```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Web UI |
| `/api/search` | POST | Search both ASCAP and BMI |
| `/api/search/ascap` | POST | Search ASCAP only |
| `/api/search/bmi` | POST | Search BMI only |
| `/api/health` | GET | Health check |

### Search Request Body

```json
{
  "title": "Song Title",
  "writer": "Writer Name (optional)",
  "iswc": "T-000.000.000-0 (optional)"
}
```

## Notes

- Searches run headless Chromium via Playwright
- Rate limit yourself to avoid being blocked
- Results are scraped from public PRO databases
- ASCAP supports URL-based search
- BMI requires cookie acceptance (handled automatically)
