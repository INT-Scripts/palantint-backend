# PalantINT Backend 🐍

High-performance REST API for the PalantINT data visualization platform.

## Architecture
- **Framework**: FastAPI (Async)
- **Database**: PostgreSQL (via SQLModel)
- **Authentication**: JWT & Bcrypt
- **Security**: Fernet (AES-128) for encrypted credential storage
- **Migrations**: Managed via Alembic

## Installation
Requires `uv`.
```bash
uv sync
```

## Running
```bash
uv run fastapi dev src/main.py
```

## Database Management
All database models are located in `src/db/models.py`. 
To apply migrations:
```bash
uv run alembic upgrade head
```
To create a new migration:
```bash
uv run alembic revision --autogenerate -m "description"
```
