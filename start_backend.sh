#!/bin/bash
cd /Users/stevengarcia/VERAX_2/verax_backend
export ENVIRONMENT=DEVELOPMENT
python3 -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
