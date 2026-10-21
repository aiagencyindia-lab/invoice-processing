"""The public /v2 API (PRD §9): a thin façade over the /api handlers, for
API-key callers. The v2/w3-api branch adds the routes."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/v2")
