from __future__ import annotations
import logging
import time
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request

from ..schemas import TextBody, VoiceAnalysisResponse
from ..services import VoiceService

router = APIRouter(prefix="/api/v1/voice", tags=["voice"])
logger = logging.getLogger("ieum.api.voice")

def service_from(request: Request) -> VoiceService:
    return request.app.state.voice_service


@router.post("/destination", response_model=VoiceAnalysisResponse)
async def destination(body: TextBody, request: Request):
    request_id = uuid4().hex[:8]
    total_started_at = time.perf_counter()
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty text")
    
    service = service_from(request)
    
    try:
        dest = await service.extract_destination(text)
        total_ms = (time.perf_counter() - total_started_at) * 1000
        logger.info(
            "voice.destination request_id=%s total_ms=%.1f text_len=%d dest_len=%d status=ok",
            request_id,
            total_ms,
            len(text),
            len(dest),
        )
        
        return VoiceAnalysisResponse(
            text=text, 
            destination=dest,
        )
        
    except ValueError as exc:
        total_ms = (time.perf_counter() - total_started_at) * 1000
        logger.warning(
            "voice.destination request_id=%s total_ms=%.1f status=bad_request detail=%s",
            request_id,
            total_ms,
            str(exc),
        )
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        total_ms = (time.perf_counter() - total_started_at) * 1000
        logger.exception(
            "voice.destination request_id=%s total_ms=%.1f status=error",
            request_id,
            total_ms,
        )
        raise exc
