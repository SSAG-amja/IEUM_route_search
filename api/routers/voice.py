from __future__ import annotations
import base64

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from starlette.responses import Response
from aiohttp.client_exceptions import ClientError

from ..schemas import TextBody, VoiceAnalysisResponse
from ..services import VoiceService

router = APIRouter(prefix="/api/v1/voice", tags=["voice"])

def service_from(request: Request) -> VoiceService:
    return request.app.state.voice_service


@router.post("/destination", response_model=VoiceAnalysisResponse)
async def destination(request: Request, file: UploadFile = File(...)):
    """
    프론트에서 오디오만 던지면:
    STT -> LLM 추출 -> TTS 생성까지 모두 처리하고
    텍스트 데이터와 Base64 오디오를 한 번에 반환합니다.
    """
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty audio")
    
    service = service_from(request)
    
    try:
        # 1. STT: 오디오 -> 텍스트
        text = service.transcribe(data)
        
        # 음성 인식이 안 된 경우의 처리
        if not text:
            fail_prompt = "음성이 인식되지 않았습니다."
            fail_audio_bytes = await service.make_tts(fail_prompt)
            return VoiceAnalysisResponse(
                text="", 
                destination="", 
                prompt=fail_prompt,
                audio=base64.b64encode(fail_audio_bytes).decode('utf-8')
            )

        # 2. LLM: 텍스트 -> 목적지 추출
        dest = await service.extract_destination(text)
        
        # 3. Prompt: 결과 멘트 텍스트 생성
        prompt = service.build_prompt(dest)
        
        # 4. TTS: 결과 멘트를 오디오 바이트로 변환 (추가된 핵심 부분!)
        audio_bytes = await service.make_tts(prompt)
        audio_base64 = base64.b64encode(audio_bytes).decode('utf-8')
        
        return VoiceAnalysisResponse(
            text=text, 
            destination=dest, 
            prompt=prompt,
            audio=audio_base64  # 인코딩된 오디오 문자열 포함
        )
        
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"오디오 처리 실패: {str(exc)}")

@router.post("/tts")
async def tts(body: TextBody, request: Request):
    """(필요시) 텍스트를 음성 파일로 변환"""
    if not body.text.strip():
        raise HTTPException(status_code=400, detail="empty text")
    
    try:
        audio = await service_from(request).make_tts(body.text.strip())
        return Response(content=audio, media_type="audio/mpeg")
    except ClientError as exc:
        raise HTTPException(status_code=502, detail="edge tts failed") from exc