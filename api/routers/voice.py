from __future__ import annotations
import base64
import logging
import time
from uuid import uuid4

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from starlette.responses import Response

from ..schemas import TextBody, VoiceAnalysisResponse
from ..services import VoiceService

router = APIRouter(prefix="/api/v1/voice", tags=["voice"])
logger = logging.getLogger("ieum.api.voice")

def service_from(request: Request) -> VoiceService:
    return request.app.state.voice_service


@router.post("/destination", response_model=VoiceAnalysisResponse)
async def destination(request: Request, file: UploadFile = File(...)):
    """
    프론트에서 오디오만 던지면:
    STT -> LLM 추출 -> TTS 생성까지 모두 처리하고
    텍스트 데이터와 Base64 오디오를 한 번에 반환합니다.
    """
    request_id = uuid4().hex[:8]
    total_started_at = time.perf_counter()

    read_started_at = time.perf_counter()
    data = await file.read()
    read_ms = (time.perf_counter() - read_started_at) * 1000
    if not data:
        raise HTTPException(status_code=400, detail="empty audio")
    
    service = service_from(request)
    
    try:
        stt_started_at = time.perf_counter()
        # 1. STT: 오디오 -> 텍스트
        text = service.transcribe(
            data,
            filename=file.filename,
            content_type=file.content_type,
        )
        stt_ms = (time.perf_counter() - stt_started_at) * 1000
        
        # 음성 인식이 안 된 경우의 처리
        if not text:
            fail_prompt = "음성이 인식되지 않았습니다."
            tts_started_at = time.perf_counter()
            fail_audio_bytes = await service.make_tts(fail_prompt)
            tts_ms = (time.perf_counter() - tts_started_at) * 1000
            total_ms = (time.perf_counter() - total_started_at) * 1000
            logger.info(
                "voice.destination request_id=%s bytes=%d read_ms=%.1f stt_ms=%.1f gemini_ms=0.0 tts_ms=%.1f total_ms=%.1f text_len=0 dest_len=0 status=no_speech",
                request_id,
                len(data),
                read_ms,
                stt_ms,
                tts_ms,
                total_ms,
            )
            return VoiceAnalysisResponse(
                text="", 
                destination="", 
                prompt=fail_prompt,
                audio=base64.b64encode(fail_audio_bytes).decode('utf-8')
            )

        # 2. LLM: 텍스트 -> 목적지 추출
        gemini_started_at = time.perf_counter()
        dest = await service.extract_destination(text)
        gemini_ms = (time.perf_counter() - gemini_started_at) * 1000
        
        # 3. Prompt: 결과 멘트 텍스트 생성
        prompt = service.build_prompt(dest)
        
        # 4. TTS: 결과 멘트를 오디오 바이트로 변환 (추가된 핵심 부분!)
        tts_started_at = time.perf_counter()
        audio_bytes = await service.make_tts(prompt)
        tts_ms = (time.perf_counter() - tts_started_at) * 1000
        audio_base64 = base64.b64encode(audio_bytes).decode('utf-8')
        total_ms = (time.perf_counter() - total_started_at) * 1000
        logger.info(
            "voice.destination request_id=%s bytes=%d read_ms=%.1f stt_ms=%.1f gemini_ms=%.1f tts_ms=%.1f total_ms=%.1f text_len=%d dest_len=%d status=ok",
            request_id,
            len(data),
            read_ms,
            stt_ms,
            gemini_ms,
            tts_ms,
            total_ms,
            len(text),
            len(dest),
        )
        
        return VoiceAnalysisResponse(
            text=text, 
            destination=dest, 
            prompt=prompt,
            audio=audio_base64  # 인코딩된 오디오 문자열 포함
        )
        
    except ValueError as exc:
        total_ms = (time.perf_counter() - total_started_at) * 1000
        logger.warning(
            "voice.destination request_id=%s bytes=%d read_ms=%.1f total_ms=%.1f status=bad_request detail=%s",
            request_id,
            len(data),
            read_ms,
            total_ms,
            str(exc),
        )
        raise HTTPException(status_code=400, detail=f"오디오 처리 실패: {str(exc)}")
    except Exception as exc:
        total_ms = (time.perf_counter() - total_started_at) * 1000
        logger.exception(
            "voice.destination request_id=%s bytes=%d read_ms=%.1f total_ms=%.1f status=error",
            request_id,
            len(data),
            read_ms,
            total_ms,
        )
        raise exc

@router.post("/tts")
async def tts(body: TextBody, request: Request):
    """(필요시) 텍스트를 음성 파일로 변환"""
    if not body.text.strip():
        raise HTTPException(status_code=400, detail="empty text")
    
    try:
        audio = await service_from(request).make_tts(body.text.strip())
        return Response(content=audio, media_type="audio/mpeg")
    except Exception as exc:
        raise HTTPException(status_code=502, detail="edge tts failed") from exc
