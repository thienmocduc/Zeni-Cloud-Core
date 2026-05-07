"""
Zeni Cloud Core — Voice AI API (P0#4-5 ClawWits).

Speech-to-Text (Whisper VN-tuned) + Text-to-Speech (XTTS-v3 VN).
Replaces external Whisper API / ElevenLabs / Coqui hosting.

Endpoints (prefix /voice-ai):
  POST   /transcribe                 — STT: audio file → text (Whisper)
  GET    /transcribe/{job_id}        — Poll STT job
  POST   /synthesize                 — TTS: text → audio (XTTS)
  GET    /synthesize/{job_id}        — Poll TTS job
  GET    /voices                     — List available TTS voices (10 voices: 6 VN + 2 EN + premium)
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_workspace_access
from app.db.base import get_db

log = logging.getLogger("zeni.voice_ai")

router = APIRouter(prefix="/voice-ai", tags=["voice-ai"])

ALLOWED_AUDIO_FORMATS = {"mp3", "wav", "m4a", "ogg", "webm", "flac"}
ALLOWED_LANGUAGES = {"vi", "en", "auto", "ja", "ko", "zh", "th"}
ALLOWED_TTS_FORMATS = {"mp3", "wav", "opus"}
MAX_AUDIO_MB = 50
MAX_TTS_CHARS = 5000


# ===== Schemas =====

class STTOut(BaseModel):
    id: str
    workspace_id: str
    status: str
    language: str
    model: str
    audio_format: Optional[str] = None
    audio_duration_sec: Optional[float] = None
    result_text: Optional[str] = None
    result_segments: list[dict] = Field(default_factory=list)
    detected_language: Optional[str] = None
    confidence: Optional[float] = None
    error_message: Optional[str] = None
    created_at: str
    finished_at: Optional[str] = None
    poll_url: str


class TTSCreate(BaseModel):
    text: str = Field(..., max_length=MAX_TTS_CHARS, description="Text to synthesize (max 5K chars per request)")
    voice_id: str = Field("vn-female-1", description="Voice ID from /voice-ai/voices")
    language: str = Field("vi")
    speed: float = Field(1.0, ge=0.5, le=2.0)
    pitch: float = Field(0.0, ge=-12.0, le=12.0)
    format: str = Field("mp3")


class TTSOut(BaseModel):
    id: str
    workspace_id: str
    status: str
    voice_id: str
    text_length: int
    output_url: Optional[str] = None
    output_duration_sec: Optional[float] = None
    error_message: Optional[str] = None
    created_at: str
    finished_at: Optional[str] = None
    poll_url: str


class VoiceOut(BaseModel):
    id: str
    display_name: str
    language: str
    gender: str
    age_range: str
    style: str
    is_premium: bool
    sample_url: Optional[str] = None


# ===== STT Endpoints =====

@router.post("/transcribe", response_model=STTOut, status_code=202)
async def transcribe_audio(
    bg: BackgroundTasks,
    ws: str = Query(..., description="workspace_id"),
    audio: UploadFile = File(..., description="Audio file (mp3/wav/m4a/ogg/webm/flac, max 50MB)"),
    language: str = Form("vi"),
    model: str = Form("whisper-vn"),
    db: AsyncSession = Depends(get_db),
    me: CurrentUser = Depends(get_current_user),
):
    """Submit audio file for speech-to-text transcription.

    AI agent:
      curl -X POST 'https://zenicloud.io/api/v1/voice-ai/transcribe?ws=myws' \\
        -H "Authorization: Bearer $ZENI_TOKEN" \\
        -F "audio=@meeting.mp3" -F "language=vi"
    """
    await require_workspace_access(ws, me)

    if language not in ALLOWED_LANGUAGES:
        raise HTTPException(422, f"Invalid language. Allowed: {ALLOWED_LANGUAGES}")

    fname = (audio.filename or "audio.mp3").lower()
    fmt = fname.rsplit(".", 1)[-1] if "." in fname else "mp3"
    if fmt not in ALLOWED_AUDIO_FORMATS:
        raise HTTPException(422, f"Audio format '{fmt}' not allowed. Allowed: {ALLOWED_AUDIO_FORMATS}")

    # Read up to MAX_AUDIO_MB
    content = await audio.read()
    if len(content) > MAX_AUDIO_MB * 1024 * 1024:
        raise HTTPException(413, f"Audio file > {MAX_AUDIO_MB}MB")

    job_id = uuid.uuid4()
    # Upload to GCS via existing source_build helper (re-using bucket)
    gcs_path = f"gs://zeni-cloud-core_cloudbuild/voice-stt-input/{ws}/{job_id}.{fmt}"

    try:
        from app.services.source_build import upload_zip_to_gcs as _upload
        await _upload(content, f"voice-stt-input/{ws}/{job_id}.{fmt}")
    except Exception as e:
        log.warning("STT upload to GCS failed, will store inline: %s", e)
        gcs_path = f"inline:{job_id}"

    await db.execute(text(
        "INSERT INTO voice_stt_jobs (id, workspace_id, user_id, audio_gcs_path, audio_format, "
        "language, model, status) "
        "VALUES (:id, :ws, :uid, :gcs, :fmt, :lang, :model, 'queued')"
    ), {
        "id": str(job_id),
        "ws": ws,
        "uid": str(me.id) if me else None,
        "gcs": gcs_path,
        "fmt": fmt,
        "lang": language,
        "model": model,
    })
    await db.commit()

    # Schedule background transcription (Phase 2 worker pending — uses Vertex AI Speech or Whisper API)
    bg.add_task(_stub_stt_worker, str(job_id))

    return STTOut(
        id=str(job_id),
        workspace_id=ws,
        status="queued",
        language=language,
        model=model,
        audio_format=fmt,
        created_at=datetime.now(timezone.utc).isoformat(),
        poll_url=f"/api/v1/voice-ai/transcribe/{job_id}?ws={ws}",
    )


@router.get("/transcribe/{job_id}", response_model=STTOut)
async def get_stt_job(
    job_id: str,
    ws: str = Query(...),
    db: AsyncSession = Depends(get_db),
    me: CurrentUser = Depends(get_current_user),
):
    await require_workspace_access(ws, me)
    r = (await db.execute(text(
        "SELECT id, workspace_id, status, language, model, audio_format, audio_duration_sec, "
        "result_text, result_segments, detected_language, confidence, error_message, created_at, finished_at "
        "FROM voice_stt_jobs WHERE id = :id AND workspace_id = :ws"
    ), {"id": job_id, "ws": ws})).mappings().first()
    if not r:
        raise HTTPException(404, "STT job not found")
    return STTOut(
        id=str(r["id"]),
        workspace_id=ws,
        status=r["status"],
        language=r["language"],
        model=r["model"],
        audio_format=r["audio_format"],
        audio_duration_sec=r["audio_duration_sec"],
        result_text=r["result_text"],
        result_segments=r["result_segments"] if isinstance(r["result_segments"], list) else json.loads(r["result_segments"] or "[]"),
        detected_language=r["detected_language"],
        confidence=r["confidence"],
        error_message=r["error_message"],
        created_at=r["created_at"].isoformat() if r["created_at"] else "",
        finished_at=r["finished_at"].isoformat() if r["finished_at"] else None,
        poll_url=f"/api/v1/voice-ai/transcribe/{job_id}?ws={ws}",
    )


# ===== TTS Endpoints =====

@router.post("/synthesize", response_model=TTSOut, status_code=202)
async def synthesize_speech(
    data: TTSCreate,
    bg: BackgroundTasks,
    ws: str = Query(...),
    db: AsyncSession = Depends(get_db),
    me: CurrentUser = Depends(get_current_user),
):
    """Submit text for TTS synthesis.

    AI agent:
      curl -X POST 'https://zenicloud.io/api/v1/voice-ai/synthesize?ws=myws' \\
        -H "Authorization: Bearer $ZENI_TOKEN" \\
        -d '{"text":"Xin chao Zeni Cloud","voice_id":"vn-female-1"}'
    """
    await require_workspace_access(ws, me)

    if data.format not in ALLOWED_TTS_FORMATS:
        raise HTTPException(422, f"Format '{data.format}' not allowed. Allowed: {ALLOWED_TTS_FORMATS}")

    # Validate voice_id
    voice = (await db.execute(text(
        "SELECT id FROM voice_catalog WHERE id = :vid"
    ), {"vid": data.voice_id})).mappings().first()
    if not voice:
        raise HTTPException(404, f"Voice '{data.voice_id}' not found. Try GET /voice-ai/voices")

    job_id = uuid.uuid4()
    await db.execute(text(
        "INSERT INTO voice_tts_jobs (id, workspace_id, user_id, text_input, text_length, "
        "voice_id, speed, pitch, format, status) "
        "VALUES (:id, :ws, :uid, :tx, :tl, :vid, :sp, :pi, :fmt, 'queued')"
    ), {
        "id": str(job_id),
        "ws": ws,
        "uid": str(me.id) if me else None,
        "tx": data.text,
        "tl": len(data.text),
        "vid": data.voice_id,
        "sp": data.speed,
        "pi": data.pitch,
        "fmt": data.format,
    })
    await db.commit()

    bg.add_task(_stub_tts_worker, str(job_id))

    return TTSOut(
        id=str(job_id),
        workspace_id=ws,
        status="queued",
        voice_id=data.voice_id,
        text_length=len(data.text),
        created_at=datetime.now(timezone.utc).isoformat(),
        poll_url=f"/api/v1/voice-ai/synthesize/{job_id}?ws={ws}",
    )


@router.get("/synthesize/{job_id}", response_model=TTSOut)
async def get_tts_job(
    job_id: str,
    ws: str = Query(...),
    db: AsyncSession = Depends(get_db),
    me: CurrentUser = Depends(get_current_user),
):
    await require_workspace_access(ws, me)
    r = (await db.execute(text(
        "SELECT id, workspace_id, status, voice_id, text_length, output_gcs_path, "
        "output_duration_sec, error_message, created_at, finished_at "
        "FROM voice_tts_jobs WHERE id = :id AND workspace_id = :ws"
    ), {"id": job_id, "ws": ws})).mappings().first()
    if not r:
        raise HTTPException(404, "TTS job not found")
    output_url = None
    if r["output_gcs_path"] and r["output_gcs_path"].startswith("gs://"):
        # Public-ish URL; signed URL generation in Phase 2
        path = r["output_gcs_path"].replace("gs://", "")
        output_url = f"https://storage.googleapis.com/{path}"
    return TTSOut(
        id=str(r["id"]),
        workspace_id=ws,
        status=r["status"],
        voice_id=r["voice_id"],
        text_length=r["text_length"] or 0,
        output_url=output_url,
        output_duration_sec=r["output_duration_sec"],
        error_message=r["error_message"],
        created_at=r["created_at"].isoformat() if r["created_at"] else "",
        finished_at=r["finished_at"].isoformat() if r["finished_at"] else None,
        poll_url=f"/api/v1/voice-ai/synthesize/{job_id}?ws={ws}",
    )


@router.get("/voices", response_model=list[VoiceOut])
async def list_voices(
    language: Optional[str] = Query(None, description="Filter by language (vi, en, etc.)"),
    db: AsyncSession = Depends(get_db),
):
    """List available TTS voices. Public — no auth required."""
    sql = "SELECT id, display_name, language, gender, age_range, style, is_premium, sample_url FROM voice_catalog"
    params = {}
    if language:
        sql += " WHERE language = :lang"
        params["lang"] = language
    sql += " ORDER BY language, gender, display_name"
    rows = (await db.execute(text(sql), params)).mappings().all()
    return [
        VoiceOut(
            id=r["id"],
            display_name=r["display_name"],
            language=r["language"],
            gender=r["gender"],
            age_range=r["age_range"],
            style=r["style"],
            is_premium=r["is_premium"],
            sample_url=r["sample_url"],
        )
        for r in rows
    ]


async def _stub_stt_worker(job_id: str) -> None:
    """REAL Phase 2 worker — wires Google Cloud Speech-to-Text via voice_worker.run_stt_job."""
    try:
        from app.services.voice_worker import run_stt_job
        await run_stt_job(job_id)
    except Exception as e:
        log.exception("STT worker dispatch failed for %s: %s", job_id, e)


async def _stub_tts_worker(job_id: str) -> None:
    """REAL Phase 2 worker — wires Google Cloud Text-to-Speech via voice_worker.run_tts_job."""
    try:
        from app.services.voice_worker import run_tts_job
        await run_tts_job(job_id)
    except Exception as e:
        log.exception("TTS worker dispatch failed for %s: %s", job_id, e)
