# Health Endpoint Specification

## Purpose
Liveness probe for the FastAPI app in `agent/main.py`.

## Requirements

### Requirement: GET /health
The app MUST expose `GET /health` returning HTTP 200 with a JSON body, without requiring env secrets or network.

#### Scenario: Liveness
- GIVEN the app under a test client
- WHEN `GET /health` is requested
- THEN status is 200 and body is JSON

#### Scenario: No desktop env
- GIVEN `DESKTOP_URL`/`DESKTOP_PASSWORD` are unset
- WHEN `GET /health` is requested
- THEN status is still 200
