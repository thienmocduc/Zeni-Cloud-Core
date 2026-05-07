"""
Zeni Cloud Core — Voice AI Worker (REAL Phase 2).

Wires Voice STT / TTS scaffold to Google Cloud Speech-to-Text + Text-to-Speech APIs.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from google.auth import default as google_auth_default
from google.auth.transport.requests import Request as GAR
from sqlalchemy import text

from app.db.base import SessionLocal

log = logging.getLogger("zeni.voice_worker")

GCP_PROJECT = "zeni-cloud-core"
STT_API = "https://speech.googleapis.com/v1/speech:recognize"
TTS_API = "https://texttospeech.googleapis.com/v1/text:synthesize"

# Map Zeni voice_id → Google Cloud TTS voice_name
GOOGLE_TTS_VOICE_MAP = {
    "vn-female-1": ("vi-VN", "vi-VN-Wavenet-A", "FEMALE"),
    "vn-female-2": ("vi-VN", "vi-VN-Wavenet-C", "FEMALE"),
    "vn-female-3": ("vi-VN", "vi-VN-Standard-A", "FEMALE"),
    "vn-male-1":   ("vi-VN", "vi-VN-Wavenet-B", "MALE"),
    "vn-male-2":   ("vi-VN", "vi-VN-Wavenet-D", "MALE"),
    "vn-male-3":   ("vi-VN", "vi-VN-Standard-B", "MALE"),
    "vn-child-1":  ("vi-VN", "vi-VN-Standard-C", "FEMALE"),
    "vn-news-1":   ("vi-VN", "vi-VN-Wavenet-A", "FEMALE"),
    "en-female-1": ("en-US", "en-US-Wavenet-F", "FEMALE"),
    "en-male-1":   ("en-US", "en-US-Wavenet-D", "MALE"),
}

FORMAT_MAP = {
    "mp3": "MP3",
    "wav": "LINEAR16",
    "opus": "OGG_OPUS",
}


def _get_token() -> str:
    creds, _ = google_auth_default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    creds.refresh(GAR())
    return creds.token


async def run_stt_job(job_id: str) -> None:
    """Process STT job: download audio from GCS → call Google Speech → save result."""
    async with SessionLocal() as db:
        row = (await db.execute(text(
            "SELECT id, audio_gcs_path, audio_format, language, model "
            "FROM voice_stt_jobs WHERE id = :id AND status = 'queued'"
        ), {"id": job_id})).mappings().first()
        if not row:
            return
        await db.execute(text(
            "UPDATE voice_stt_jobs SET status = 'running', started_at = NOW() WHERE id = :id"
        ), {"id": job_id})
        await db.commit()

    try:
        # Read audio bytes from GCS
        gcs_path = row["audio_gcs_path"]
        if gcs_path.startswith("gs://"):
            bucket = gcs_path.split("/")[2]
            object_path = "/".join(gcs_path.split("/")[3:])
            token = _get_token()
            async with httpx.AsyncClient(timeout=60.0) as client:
                r = await client.get(
                    f"https://storage.googleapis.com/storage/v1/b/{bucket}/o/{object_path.replace('/','%2F')}?alt=media",
                    headers={"Authorization": f"Bearer {token}"},
                )
                if r.status_code != 200:
                    raise RuntimeError(f"GCS read failed: {r.status_code}")
                audio_bytes = r.content
        else:
            raise RuntimeError(f"Unsupported audio path: {gcs_path}")

        # Encoding hint
        encoding_map = {"mp3": "MP3", "wav": "LINEAR16", "ogg": "OGG_OPUS", "webm": "WEBM_OPUS", "flac": "FLAC", "m4a": "MP3"}
        encoding = encoding_map.get(row["audio_format"], "ENCODING_UNSPECIFIED")

        # Call Google Speech-to-Text
        import base64 as b64
        token = _get_token()
        body = {
            "config": {
                "encoding": encoding,
                "languageCode": {"vi": "vi-VN", "en": "en-US", "auto": "vi-VN"}.get(row["language"], "vi-VN"),
                "model": "latest_long",
                "enableAutomaticPunctuation": True,
                "enableWordTimeOffsets": True,
            },
            "audio": {"content": b64.b64encode(audio_bytes).decode()},
        }
        async with httpx.AsyncClient(timeout=300.0) as client:
            resp = await client.post(STT_API, headers={"Authorization": f"Bearer {token}"}, json=body)
            if resp.status_code != 200:
                raise RuntimeError(f"Google STT failed: {resp.status_code} {resp.text[:200]}")
            data = resp.json()

        # Combine transcripts
        results = data.get("results", [])
        full_text = " ".join(r["alternatives"][0]["transcript"] for r in results if r.get("alternatives"))
        confidence = (sum(r["alternatives"][0].get("confidence", 0) for r in results if r.get("alternatives"))
                      / max(1, len(results)))
        segments = []
        for r in results:
            for alt in r.get("alternatives", []):
                seg = {"text": alt.get("transcript", ""), "confidence": alt.get("confidence", 0)}
                if "words" in alt and alt["words"]:
                    seg["start"] = alt["words"][0].get("startTime", "0s")
                    seg["end"] = alt["words"][-1].get("endTime", "0s")
                segments.append(seg)

        async with SessionLocal() as db:
            await db.execute(text(
                "UPDATE voice_stt_jobs SET status = 'success', finished_at = NOW(), "
                "result_text = :rt, result_segments = CAST(:rs AS jsonb), "
                "confidence = :cf, detected_language = :dl WHERE id = :id"
            ), {
                "rt": full_text,
                "rs": json.dumps(segments),
                "cf": float(confidence),
                "dl": row["language"],
                "id": job_id,
            })
            await db.commit()
        log.info("STT %s SUCCESS — %d chars", job_id, len(full_text))

    except Exception as e:
        log.exception("STT %s failed: %s", job_id, e)
        async with SessionLocal() as db:
            await db.execute(text(
                "UPDATE voice_stt_jobs SET status = 'failed', finished_at = NOW(), error_message = :err WHERE id = :id"
            ), {"err": str(e)[:500], "id": job_id})
            await db.commit()


async def run_tts_job(job_id: str) -> None:
    """Process TTS job: call Google Cloud TTS → save audio to GCS → return URL."""
    async with SessionLocal() as db:
        row = (await db.execute(text(
            "SELECT id, workspace_id, text_input, voice_id, speed, pitch, format "
            "FROM voice_tts_jobs WHERE id = :id AND status = 'queued'"
        ), {"id": job_id})).mappings().first()
        if not row:
            return
        await db.execute(text(
            "UPDATE voice_tts_jobs SET status = 'running' WHERE id = :id"
        ), {"id": job_id})
        await db.commit()

    try:
        voice_cfg = GOOGLE_TTS_VOICE_MAP.get(row["voice_id"], ("vi-VN", "vi-VN-Wavenet-A", "FEMALE"))
        lang, voice_name, gender = voice_cfg
        audio_format = FORMAT_MAP.get(row["format"], "MP3")

        body = {
            "input": {"text": row["text_input"]},
            "voice": {"languageCode": lang, "name": voice_name, "ssmlGender": gender},
            "audioConfig": {
                "audioEncoding": audio_format,
                "speakingRate": float(row["speed"]),
                "pitch": float(row["pitch"]),
            },
        }
        token = _get_token()
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(TTS_API, headers={"Authorization": f"Bearer {token}"}, json=body)
            if resp.status_code != 200:
                raise RuntimeError(f"Google TTS failed: {resp.status_code} {resp.text[:200]}")
            data = resp.json()

        import base64 as b64
        audio_bytes = b64.b64decode(data["audioContent"])

        # Upload to GCS
        ws = row["workspace_id"]
        gcs_object = f"voice-tts-output/{ws}/{job_id}.{row['format']}"
        bucket = "zeni-cloud-core_cloudbuild"
        async with httpx.AsyncClient(timeout=60.0) as client:
            mime = {"mp3": "audio/mpeg", "wav": "audio/wav", "opus": "audio/ogg"}.get(row["format"], "audio/mpeg")
            r = await client.post(
                f"https://storage.googleapis.com/upload/storage/v1/b/{bucket}/o",
                params={"uploadType": "media", "name": gcs_object},
                headers={"Authorization": f"Bearer {token}", "Content-Type": mime},
                content=audio_bytes,
            )
            if r.status_code not in (200, 201):
                raise RuntimeError(f"GCS upload failed: {r.status_code}")

        async with SessionLocal() as db:
            await db.execute(text(
                "UPDATE voice_tts_jobs SET status = 'success', finished_at = NOW(), "
                "output_gcs_path = :gp, output_size_bytes = :sz WHERE id = :id"
            ), {
                "gp": f"gs://{bucket}/{gcs_object}",
                "sz": len(audio_bytes),
                "id": job_id,
            })
            await db.commit()
        log.info("TTS %s SUCCESS — %d bytes", job_id, len(audio_bytes))

    except Exception as e:
        log.exception("TTS %s failed: %s", job_id, e)
        async with SessionLocal() as db:
            await db.execute(text(
                "UPDATE voice_tts_jobs SET status = 'failed', finished_at = NOW(), error_message = :err WHERE id = :id"
            ), {"err": str(e)[:500], "id": job_id})
            await db.commit()
