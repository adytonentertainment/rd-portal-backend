# TuneScan Backend

FastAPI backend for TuneScan - A music analytics platform for tracking streaming metrics, royalties, and catalog performance.

## Features

- **Music Catalog Management** - Track and manage your music catalog
- **Streaming Analytics** - Real-time streaming data from Spotify, YouTube, and more
- **Royalty Calculations** - Track earnings and royalty distributions
- **Multi-Platform Integration** - Songstats, Soundcharts, ChartMetric, ACRCloud APIs
- **Song Detection** - Audio fingerprinting via ACRCloud
- **Payment Processing** - Stripe integration for subscriptions
- **Background Jobs** - Automated daily data updates via APScheduler
- **Security** - Rate limiting, account lockout, security headers

## Tech Stack

- **Framework:** FastAPI
- **Database:** SQLite (development) / SQLAlchemy ORM
- **Authentication:** JWT tokens with bcrypt password hashing
- **Task Scheduling:** APScheduler
- **Payment Processing:** Stripe
- **Audio Processing:** ACRCloud, librosa

## Prerequisites

- Python 3.10.19
- pip (latest version recommended)
- Git

## Installation

### 1. Clone the repository

```bash
git clone <repository-url>
cd tunescan_backend
```

### 2. Create a virtual environment

```bash
python -m venv venv

# Windows
venv\Scripts\activate

# macOS/Linux
source venv/bin/activate
```

### 3. Install dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

### 4. Set up environment variables

Copy the example environment file and configure it:

```bash
cp .env.example .env
```

Edit `.env` and add your API keys and credentials:

```env
# Required API Keys
SONGSTATS_API_KEY=your-songstats-api-key
SOUNDCHARTS_APP_ID=your-soundcharts-app-id
SOUNDCHARTS_API_KEY=your-soundcharts-api-key
ACRCLOUD_ACCESS_KEY=your-acrcloud-access-key
ACRCLOUD_ACCESS_SECRET=your-acrcloud-access-secret
STRIPE_API_SECRET_KEY=your-stripe-secret-key
SPOTIFY_CLIENT_ID=your-spotify-client-id
SPOTIFY_CLIENT_SECRET=your-spotify-client-secret

# Application Settings
MODE=development
SECRET_KEY=your-secret-key-here
PASSPHRASE=your-passphrase-here
SQLALCHEMY_DATABASE_URL=sqlite:///./tunescan_development.db
BASE_URL_FRONTEND=http://localhost:3000/
BASE_URL_BACKEND=http://localhost:8000/
```

See `.env.example` for all available configuration options.

## Running the Backend

### Development Server

Start the FastAPI development server:

```bash
python run.py --type server --env development
```

The API will be available at:
- **API:** http://localhost:8000
- **API Docs (Swagger):** http://localhost:8000/docs
- **API Docs (ReDoc):** http://localhost:8000/redoc

### Alternative: Direct uvicorn

```bash
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

### Background Tracker

Run the background job scheduler for daily data updates:

```bash
python run.py --type tracker --env development
```

### Production Server

```bash
python run.py --type server --env production
```

**Note:** In production mode, API docs are disabled for security.

## Database Setup

### Create Tables

Tables are automatically created on first run, or you can manually create them:

```bash
python run.py --create_tables
```

### Database Schema

The application uses SQLAlchemy ORM with the following main models:
- **User** - User accounts and authentication
- **Catalog** - Music catalog tracks
- **PlayCountOverTime** - Historical streaming data
- **Royalty** - Royalty calculations and distributions
- **Subscription** - User subscription tiers

## Project Structure

```
tunescan_backend/
├── app/
│   ├── main.py                 # FastAPI application entry point
│   ├── tracker.py              # Background job scheduler
│   ├── settings/               # Environment configuration
│   ├── routers/                # API endpoints (auth, catalog, playcount, etc.)
│   ├── services/               # Business logic
│   ├── models/                 # SQLAlchemy ORM models
│   ├── schemas/                # Pydantic request/response models
│   ├── crud/                   # Database CRUD operations
│   ├── database/               # Database configuration
│   ├── middleware/             # Rate limiting, account lockout
│   ├── scheduler/              # Background job definitions
│   ├── libs/                   # Third-party API integrations
│   │   ├── ACRCloud/          # Song detection API
│   │   ├── Songstats/         # Streaming data API
│   │   └── Email/             # Email utilities
│   ├── logger/                 # Logging configuration
│   └── misc/                   # Helper utilities
├── scripts/                    # Utility scripts
│   ├── data_fetchers/         # API data retrieval scripts
│   ├── admin/                 # Admin utilities
│   └── tests/                 # Test scripts
├── run.py                      # Application entry point
├── requirements.txt            # Python dependencies
├── .env.example               # Environment variables template
└── README.md                  # This file
```

## API Endpoints

### Authentication
- `POST /auth/signup` - Register new user
- `POST /auth/login` - Login
- `POST /auth/logout` - Logout
- `POST /auth/forgot-password` - Password reset
- `GET /auth/me` - Get current user

### Catalog
- `GET /catalog` - List catalog tracks
- `POST /catalog` - Add track to catalog
- `PUT /catalog/{id}` - Update track
- `DELETE /catalog/{id}` - Remove track
- `GET /catalog/search` - Search catalog

### Playcount & Analytics
- `GET /history/playcount` - Get historical streaming data
- `GET /playcount/today` - Today's streaming stats
- `GET /royalty/calculate` - Calculate royalties

### Song Detection
- `POST /acrcloud/identify` - Identify song from audio file

See `/docs` endpoint for complete API documentation (development mode only).

## Development

### Running Tests

```bash
# Run test scripts
python scripts/tests/test_songstats_api.py
```

### Code Style

The project follows PEP 8 guidelines. Key conventions:
- Use type hints where applicable
- Document functions with docstrings
- Keep functions focused and single-purpose

### Adding New Routes

1. Create a new router in `app/routers/`
2. Add route to `app/routers/router.py`
3. Include necessary CRUD operations in `app/crud/`
4. Define Pydantic schemas in `app/schemas/`

## Deployment

### Environment Configuration

For production deployment:

1. Set `MODE=production` in environment variables
2. Use a production-grade database (PostgreSQL recommended)
3. Set strong `SECRET_KEY` and `PASSPHRASE`
4. Configure HTTPS/SSL certificates
5. Use a reverse proxy (nginx/Apache)
6. Enable rate limiting and security headers (already configured)

### Production Server

```bash
# Using gunicorn with uvicorn workers
gunicorn app.main:app -w 4 -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:8000

# Or using uvicorn directly
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4
```

## Utilities & Scripts

### Data Fetchers (`scripts/data_fetchers/`)

Scripts for fetching data from external APIs:
- `fetch_catalog_daily_data.py` - Fetch daily catalog data
- `fetch_full_history_from_release.py` - Fetch complete streaming history
- `fetch_spotify_track_info.py` - Fetch Spotify metadata

Run with proper API keys configured in `.env`.

### Admin Scripts (`scripts/admin/`)

- `set_user_tier.py` - Manage user subscription tiers
- `trigger_songstats_fetch.py` - Manually trigger data updates

## Security

- **Rate Limiting:** Login/signup/password reset endpoints (10 req/min)
- **Account Lockout:** After 5 failed login attempts
- **Security Headers:** XSS protection, clickjacking prevention, CSP
- **GZip Compression:** Enabled for responses >1000 bytes
- **Password Hashing:** Bcrypt with salt
- **JWT Tokens:** Secure authentication

## Troubleshooting

### Common Issues

**1. Import errors on startup**
```bash
# Ensure virtual environment is activated
pip install -r requirements.txt
```

**2. Database errors**
```bash
# Recreate tables
python run.py --create_tables
```

**3. API key errors**
```bash
# Verify .env file exists and contains valid API keys
cat .env
```

**4. Port already in use**
```bash
# Change port in run.py or kill existing process
lsof -ti:8000 | xargs kill -9  # macOS/Linux
```

## Environment Variables Reference

See [.env.example](.env.example) for complete list of environment variables.

### Core Settings
- `MODE` - development/production
- `SECRET_KEY` - JWT token secret
- `SQLALCHEMY_DATABASE_URL` - Database connection string

### API Keys
- `SONGSTATS_API_KEY` - Songstats API
- `SOUNDCHARTS_APP_ID` / `SOUNDCHARTS_API_KEY` - Soundcharts API
- `ACRCLOUD_ACCESS_KEY` / `ACRCLOUD_ACCESS_SECRET` - ACRCloud
- `STRIPE_API_SECRET_KEY` - Stripe payments
- `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` - Spotify API

## Contributing

1. Create a feature branch
2. Make your changes
3. Test thoroughly
4. Submit a pull request

## License

[Add your license information here]

## Support

For issues and questions:
- Check the API documentation at `/docs`
- Review this README
- Contact: contact@tunescan.app

---

**Last Updated:** January 2025
