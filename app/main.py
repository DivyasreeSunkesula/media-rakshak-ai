from __future__ import annotations

import base64
import hashlib
import hmac
import json
import mimetypes
import os
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

import cv2
from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from google import genai
from google.genai import types
from google.api_core.exceptions import PermissionDenied
from google.cloud import firestore

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "app" / "static"
STORAGE = ROOT / "storage"
UPLOADS = STORAGE / "uploads"
DB_PATH = STORAGE / "cases.json"  # Read-only migration source; not the active database.
ENV_PATH = ROOT / ".env"
load_dotenv(ENV_PATH, override=False)
UPLOADS.mkdir(parents=True, exist_ok=True)
STORAGE.mkdir(parents=True, exist_ok=True)

PROJECT_ID = os.getenv("FIRESTORE_PROJECT_ID", "media-rakshak-ai").strip()
DATABASE_ID = os.getenv("FIRESTORE_DATABASE_ID", "mr-db").strip()
COLLECTION_NAME = os.getenv("FIRESTORE_CASES_COLLECTION", "cases").strip() or "cases"
CHUNK_BYTES = 600 * 1024  # Base64 expansion keeps each Firestore document below 1 MiB.
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_MB", "50")) * 1024 * 1024
_firestore_client: Any = None

@asynccontextmanager
async def lifespan(_app: FastAPI):
    # A failed connection is reported by /api/health and database endpoints, not hidden.
    migrate_local_cases_if_enabled()
    yield


app = FastAPI(
    title="MediaRakshak AI",
    version="2.0.0",
    description="Victim-first media incident response prototype with Firestore persistence",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.middleware("http")
async def deployment_basic_auth(request, call_next):
    """Optional app-level Basic Auth; require it for public deployments via SECURE_DEPLOYMENT=true."""
    secure = os.getenv("SECURE_DEPLOYMENT", "false").lower() in {"1", "true", "yes", "on"}
    expected_user = os.getenv("APP_AUTH_USERNAME", "")
    expected_password = os.getenv("APP_AUTH_PASSWORD", "")
    if not secure and not expected_user and not expected_password:
        return await call_next(request)
    if not expected_user or not expected_password:
        return JSONResponse({"detail": "Deployment authentication is enabled but APP_AUTH_USERNAME/APP_AUTH_PASSWORD are not configured."}, status_code=503)
    # Keep health check reachable for service diagnostics; it contains configuration identifiers only.
    if request.url.path == "/api/health":
        return await call_next(request)
    auth = request.headers.get("authorization", "")
    try:
        scheme, encoded = auth.split(" ", 1)
        supplied_user, supplied_password = base64.b64decode(encoded, validate=True).decode("utf-8").split(":", 1)
    except Exception:
        scheme, supplied_user, supplied_password = "", "", ""
    valid = (scheme.lower() == "basic" and
             hmac.compare_digest(supplied_user.encode(), expected_user.encode()) and
             hmac.compare_digest(supplied_password.encode(), expected_password.encode()))
    if not valid:
        return JSONResponse({"detail": "Authentication required."}, status_code=401,
                            headers={"WWW-Authenticate": 'Basic realm="MediaRakshak AI", charset="UTF-8"'})
    return await call_next(request)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_firestore() -> Any:
    """Return a cached Firestore client connected to the configured named database."""
    global _firestore_client
    if _firestore_client is None:
        if not PROJECT_ID or not DATABASE_ID:
            raise RuntimeError("FIRESTORE_PROJECT_ID and FIRESTORE_DATABASE_ID must be configured")
        # Uses GOOGLE_APPLICATION_CREDENTIALS if set, otherwise Application Default Credentials.
        _firestore_client = firestore.Client(project=PROJECT_ID, database=DATABASE_ID)
    return _firestore_client


def firestore_error(exc: Exception) -> HTTPException:
    if isinstance(exc, PermissionDenied):
        message = "Firestore permission denied. Check the service account's Cloud Datastore User role and database ID."
    else:
        message = f"Firestore is unavailable ({type(exc).__name__}). Check credentials, Firestore API access, project ID, database ID, and billing/API status."
    return HTTPException(status_code=503, detail=message)


def read_db() -> list[dict[str, Any]]:
    """Read all case records from Firestore. No silent local-JSON fallback."""
    try:
        docs = get_firestore().collection(COLLECTION_NAME).stream()
        items = []
        for doc in docs:
            item = doc.to_dict() or {}
            item["id"] = doc.id
            items.append(item)
        items.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
        return items
    except HTTPException:
        raise
    except Exception as exc:
        raise firestore_error(exc) from exc


def write_db(items: list[dict[str, Any]]) -> None:
    """Upsert cases to Firestore; never delete records as a side effect of an update."""
    try:
        client = get_firestore()
        collection = client.collection(COLLECTION_NAME)
        # Firestore batches have a 500-write limit. Split defensively for larger imports.
        for start in range(0, len(items), 400):
            batch = client.batch()
            for item in items[start:start + 400]:
                case_id = str(item.get("id", "")).strip()
                if not case_id:
                    continue
                payload = dict(item)
                payload.pop("id", None)
                batch.set(collection.document(case_id), payload, merge=True)
            batch.commit()
    except HTTPException:
        raise
    except Exception as exc:
        raise firestore_error(exc) from exc


def migrate_local_cases_if_enabled() -> dict[str, int]:
    """Idempotently import legacy JSON cases. Existing Firestore cases are never overwritten."""
    if os.getenv("MIGRATE_LOCAL_CASES", "true").lower() not in {"1", "true", "yes", "on"}:
        return {"imported": 0, "skipped": 0}
    if not DB_PATH.exists():
        return {"imported": 0, "skipped": 0}
    try:
        legacy = json.loads(DB_PATH.read_text(encoding="utf-8"))
        if not isinstance(legacy, list):
            return {"imported": 0, "skipped": 0}
        client = get_firestore()
        collection = client.collection(COLLECTION_NAME)
        imported = skipped = 0
        for item in legacy:
            if not isinstance(item, dict) or not item.get("id"):
                skipped += 1
                continue
            ref = collection.document(str(item["id"]))
            if ref.get().exists:
                skipped += 1
                continue
            payload = dict(item)
            payload.pop("id", None)
            # Legacy records can contain IDs that pointed to local-only files. Do not
            # present those files as if they had already been copied into Firestore.
            legacy_analysis = payload.get("analysis")
            if isinstance(legacy_analysis, dict) and legacy_analysis.get("upload_id"):
                legacy_analysis = dict(legacy_analysis)
                legacy_analysis.pop("upload_id", None)
                legacy_analysis["storage_note"] = "Legacy media was not automatically uploaded. Re-upload this file in the app to store its bytes in Firestore."
                payload["analysis"] = legacy_analysis
            legacy_evidence = payload.get("evidence")
            if isinstance(legacy_evidence, list):
                payload["evidence"] = [
                    {**entry, "storage": "legacy-local-not-migrated", **({"upload_id": None} if entry.get("upload_id") else {})}
                    if isinstance(entry, dict) else entry
                    for entry in legacy_evidence
                ]
            ref.set(payload)
            imported += 1
        return {"imported": imported, "skipped": skipped}
    except Exception:
        # Do not prevent startup; API endpoints surface actual Firestore failures.
        return {"imported": 0, "skipped": 0}


def safe_case(case_id: str) -> dict[str, Any]:
    for case in read_db():
        if case["id"] == case_id:
            return case
    raise HTTPException(status_code=404, detail="Case not found")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_video(path: Path) -> dict[str, Any]:
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    info: dict[str, Any] = {
        "filename": path.name,
        "size_bytes": path.stat().st_size,
        "mime_type": mime,
        "sha256": sha256_file(path),
        "metadata_method": "OpenCV; ffprobe when installed",
        "duration_seconds": None,
        "width": None,
        "height": None,
        "fps": None,
        "frame_count": None,
        "codec": None,
        "sampled_frames": [],
        "warnings": [
            "Metadata describes this file, not whether its depicted events are authentic.",
            "Metadata may be absent, edited, or stripped. Missing metadata is not proof of manipulation.",
            "SHA-256 identifies this exact file only; a re-encoded or resized copy will usually have a different hash."
        ],
    }
    has_video_stream = False
    if shutil.which("ffprobe"):
        try:
            cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                   "stream=codec_name,width,height,r_frame_rate,nb_frames:format=duration,format_name,tags=creation_time,encoder",
                   "-of", "json", str(path)]
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False)
            if out.returncode == 0:
                parsed = json.loads(out.stdout or "{}")
                streams = parsed.get("streams", [])
                fmt = parsed.get("format", {})
                if streams:
                    has_video_stream = True
                    s = streams[0]
                    info["codec"] = s.get("codec_name")
                    info["width"] = s.get("width")
                    info["height"] = s.get("height")
                    rate = s.get("r_frame_rate", "")
                    if "/" in rate:
                        n, d = rate.split("/", 1)
                        info["fps"] = round(float(n) / max(float(d), 1), 3)
                    info["frame_count"] = int(s["nb_frames"]) if str(s.get("nb_frames", "")).isdigit() else None
                if fmt.get("duration"):
                    info["duration_seconds"] = round(float(fmt["duration"]), 3)
                info["container"] = fmt.get("format_name")
                info["container_tags"] = fmt.get("tags", {})
        except Exception as exc:
            info["warnings"].append(f"ffprobe metadata unavailable: {type(exc).__name__}")
    cap = cv2.VideoCapture(str(path))
    decoded_frame = False
    if cap.isOpened():
        fps = cap.get(cv2.CAP_PROP_FPS) or 0
        fourcc_value = int(cap.get(cv2.CAP_PROP_FOURCC) or 0)
        fourcc = "".join(chr((fourcc_value >> (8 * i)) & 0xFF) for i in range(4)).strip("\x00 ")
        if not info["codec"] and fourcc:
            info["codec"] = fourcc
        frame_ok, _first_frame = cap.read()
        decoded_frame = bool(frame_ok)
        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        if info["width"] is None and width:
            info["width"] = width
        if info["height"] is None and height:
            info["height"] = height
        if info["fps"] is None and fps:
            info["fps"] = round(fps, 3)
        if info["frame_count"] is None and frame_count:
            info["frame_count"] = frame_count
        if info["duration_seconds"] is None and fps > 0 and frame_count:
            info["duration_seconds"] = round(frame_count / fps, 3)
        if frame_count > 0:
            for pct in (0.15, 0.5, 0.85):
                cap.set(cv2.CAP_PROP_POS_FRAMES, min(frame_count - 1, int(frame_count * pct)))
                ok, frame = cap.read()
                if ok:
                    small = cv2.resize(frame, (160, 90))
                    mean_bgr = small.mean(axis=(0, 1))
                    info["sampled_frames"].append({
                        "approx_position_percent": int(pct * 100),
                        "average_color_bgr": [int(v) for v in mean_bgr],
                        "note": "Frame sampled for technical inspection; average color is not a deepfake signal."
                    })
        cap.release()
    else:
        info["warnings"].append("The uploaded file could not be decoded as a video by OpenCV.")
    if not decoded_frame and not has_video_stream:
        raise HTTPException(status_code=422, detail="This file could not be recognized as a readable video. Try a valid MP4/MOV/WEBM/AVI/MKV file; no evidence record was created.")
    if not decoded_frame and has_video_stream:
        info["warnings"].append("ffprobe found a video stream, but OpenCV could not decode a frame. Some properties may be unavailable.")
    return info


def persist_media_to_firestore(case_id: str, upload_id: str, path: Path, metadata: dict[str, Any]) -> dict[str, Any]:
    """Store media bytes as bounded Base64 chunks in Firestore (no public URLs)."""
    client = get_firestore()
    media_ref = client.collection(COLLECTION_NAME).document(case_id).collection("media").document(upload_id)
    chunks_ref = media_ref.collection("chunks")
    count = (path.stat().st_size + CHUNK_BYTES - 1) // CHUNK_BYTES
    # At 50 MiB, this is under 100 writes and stays within Firestore's 500-write batch cap.
    batch = client.batch()
    batch.set(media_ref, {
        "upload_id": upload_id,
        "filename": Path(str(metadata.get("filename", path.name))).name,
        "size_bytes": path.stat().st_size,
        "mime_type": metadata.get("mime_type") or mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        "sha256": metadata.get("sha256") or sha256_file(path),
        "chunk_count": count,
        "created_at": now(),
        "storage": "firestore-chunks",
        "authenticity_assessment": "UNVERIFIED",
    }, merge=True)
    with path.open("rb") as f:
        index = 0
        while True:
            raw = f.read(CHUNK_BYTES)
            if not raw:
                break
            batch.set(chunks_ref.document(f"{index:06d}"), {
                "index": index,
                "data_b64": base64.b64encode(raw).decode("ascii"),
                "byte_count": len(raw),
            })
            index += 1
    batch.commit()
    return {"upload_id": upload_id, "storage": "firestore-chunks", "chunk_count": count, "sha256": metadata.get("sha256")}


def heuristic_response(case: dict[str, Any], analysis: dict[str, Any] | None = None) -> dict[str, Any]:
    links = case.get("links", [])
    evidence = case.get("evidence", [])
    return {
        "title": "Suggested response plan",
        "summary": "Authenticity is unverified from the information currently available. Preserve source details and avoid resharing the material while the case is assessed.",
        "findings": [
            "No definitive authenticity verdict has been made.",
            f"This case currently contains {len(links)} known post link(s) and {len(evidence)} evidence item(s).",
            "File metadata and hashes can support documentation but do not, by themselves, establish whether content is manipulated."
        ],
        "known_facts": [
            f"Case reference: {case.get('id')}" if case.get('id') else "Case reference not assigned.",
            f"Case title: {case.get('title')}" if case.get('title') else "Case title not provided.",
            f"Incident category selected: {case.get('incident_type')}" if case.get('incident_type') else "Incident category not provided.",
            f"User-entered context: {case.get('notes')}" if case.get('notes') else "No case context notes were provided.",
            *([f"Technical file name: {analysis.get('filename')}"] if analysis and analysis.get('filename') else []),
            *([f"SHA-256: {analysis.get('sha256')}"] if analysis and analysis.get('sha256') else []),
        ],
        "unknowns": [
            "Whether the media is authentic or manipulated has not been established.",
            *(["No known post URL has been recorded."] if not links else []),
            *(["No visible account name has been recorded for the known post(s)."] if not any(x.get("account") for x in links) else []),
            *(["The date/time the post was seen has not been recorded."] if not any(x.get("posted_at") for x in links) else []),
        ],
        "assumptions": [],
        "next_steps": [
            "Record the post URL, platform, visible account name, and the date/time you accessed it.",
            "Save screenshots of the post and surrounding context where lawful and safe; do not repost the material to gather evidence.",
            "Use the platform's official reporting route for impersonation, non-consensual imagery, or manipulated media, as applicable.",
            "Keep copies of report confirmations and update each known link's status only when you have verified it.",
            "If there are threats, extortion, or intimate imagery, consider contacting a trusted support organization or appropriate local authorities."
        ],
        "complaint_draft": f"Hello Trust & Safety team,\n\nI am reporting content that may misuse a person's identity or likeness. Case reference: {case.get('id', 'not assigned')}.\nPost URL(s): {', '.join(x.get('url','') for x in links) or '[add post URL]'}\n\nPlease review this content under your relevant impersonation, privacy, non-consensual imagery, or manipulated-media policies. I can provide the available context and supporting evidence through your secure reporting process. Please confirm receipt and advise on the next steps.\n\nThank you.",
        "caveat": "Draft only. Review for accuracy and remove any details that you cannot verify before submitting. This app does not submit reports or remove content automatically."
    }


async def generate_with_gemini(case: dict[str, Any], analysis: dict[str, Any] | None = None) -> dict[str, Any]:
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key or api_key.strip().startswith("REPLACE_"):
        result = heuristic_response(case, analysis)
        result["mode"] = "local fallback (configure GEMINI_API_KEY to enable Gemini)"
        return result
    client = genai.Client(api_key=api_key)
    prompt = f"""You are MediaRakshak AI, a careful victim-first media incident response assistant. Return one valid JSON object only with keys: title (string), summary (string), findings (array of strings), known_facts (array of strings copied or faithfully summarized from supplied case/analysis), unknowns (array of strings for important missing details), assumptions (array; MUST be empty unless an explicitly labelled hypothesis is necessary), next_steps (array of practical steps), complaint_draft (string), caveat (string).
GROUNDING RULES (strict): Use only facts explicitly present in CASE JSON or TECHNICAL ANALYSIS JSON. Never invent a person's name, identity, consent status, location, date/time, platform, account, motive, impact, legal conclusion, or report outcome. If a detail is missing, say it is not provided and use a bracketed placeholder such as [date/time not provided] in the complaint draft. Do not present hypotheses as observations. For every technical claim, retain its supplied value and uncertainty. Do not infer manipulation from different hashes, file sizes, frame colors, or metadata. Never declare media fake/real. Never imply you can remove content, crawl the internet, or submit reports. Avoid legal certainty. Keep tone calm, practical, and nonjudgmental. Do not ask the user to upload or redistribute intimate/abusive material. Do not assert lack of consent unless explicitly supplied. Do not turn a category label into proof that the event happened.
CASE JSON (the only incident source of truth):
{json.dumps(case, ensure_ascii=False)}
TECHNICAL ANALYSIS JSON (technical signals only, not authenticity proof):
{json.dumps(analysis or {}, ensure_ascii=False)}"""
    try:
        response = client.models.generate_content(
            model=os.getenv("GEMINI_MODEL", "gemini-3.8-flash"),
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        data = json.loads(response.text or "{}")
        if not isinstance(data, dict):
            raise ValueError("Gemini response was not a JSON object")
        for key in ("title", "summary", "complaint_draft", "caveat"):
            if not isinstance(data.get(key), str):
                data[key] = "" if key != "caveat" else "AI-generated draft. Verify every statement before use."
        for key in ("findings", "known_facts", "unknowns", "assumptions", "next_steps"):
            if not isinstance(data.get(key), list):
                data[key] = []
            data[key] = [str(item)[:1200] for item in data[key][:30]]
        data["mode"] = "Gemini"
        data["caveat"] = (data.get("caveat", "") + " Review every factual statement before use; missing details must remain placeholders.").strip()
        data.setdefault("known_facts", [])
        data.setdefault("unknowns", [])
        data.setdefault("assumptions", [])
        return data
    except Exception as exc:
        import logging
        logging.exception("Gemini response-plan generation failed")
        result = heuristic_response(case, analysis)
        result["mode"] = f"local fallback (Gemini request failed: {type(exc).__name__})"
        return result


@app.get("/", response_class=HTMLResponse)
def home() -> str:
    return (STATIC / "index.html").read_text(encoding="utf-8")


@app.get("/api/health")
def health() -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "ok",
        "gemini_configured": bool(os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")),
        "firestore_project_id": PROJECT_ID,
        "firestore_database_id": DATABASE_ID,
        "firestore_collection": COLLECTION_NAME,
        "time": now(),
    }
    try:
        get_firestore().collection(COLLECTION_NAME).limit(1).get()
        result["firestore"] = "connected"
        result["database_status"] = "ok"
    except Exception as exc:
        result["firestore"] = "error"
        result["database_status"] = "unavailable"
        result["firestore_error_type"] = type(exc).__name__
        result["status"] = "degraded"
    return result


@app.get("/api/cases")
def list_cases() -> list[dict[str, Any]]:
    return read_db()


@app.post("/api/cases")
def create_case(payload: dict[str, Any]) -> dict[str, Any]:
    title = str(payload.get("title", "Untitled incident")).strip()[:120] or "Untitled incident"
    case = {
        "id": "MR-" + uuid.uuid4().hex[:8].upper(),
        "title": title,
        "incident_type": str(payload.get("incident_type", "Possible misuse of likeness"))[:100],
        "notes": str(payload.get("notes", ""))[:5000],
        "created_at": now(),
        "updated_at": now(),
        "links": [],
        "evidence": [],
        "analysis": None,
        "response_plan": None,
    }
    write_db([case])
    return case


@app.delete("/api/cases/{case_id}")
def delete_case(case_id: str) -> dict[str, Any]:
    """Delete a case and its nested Firestore media documents on explicit user request."""
    client = get_firestore()
    case_ref = client.collection(COLLECTION_NAME).document(case_id)
    if not case_ref.get().exists:
        raise HTTPException(status_code=404, detail="Case not found")
    try:
        # Firestore does not cascade-delete subcollections. Remove media chunks and metadata explicitly.
        for media_doc in list(case_ref.collection("media").stream()):
            media_ref = case_ref.collection("media").document(media_doc.id)
            chunk_docs = list(media_ref.collection("chunks").stream())
            for start in range(0, len(chunk_docs), 400):
                batch = client.batch()
                for chunk_doc in chunk_docs[start:start + 400]:
                    batch.delete(media_ref.collection("chunks").document(chunk_doc.id))
                if chunk_docs[start:start + 400]:
                    batch.commit()
            client.batch().delete(media_ref).commit()
        client.batch().delete(case_ref).commit()
        return {"deleted": True, "case_id": case_id}
    except Exception as exc:
        raise firestore_error(exc) from exc


@app.post("/api/cases/{case_id}/links")
def add_link(case_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    items = read_db()
    for case in items:
        if case["id"] == case_id:
            url = str(payload.get("url", "")).strip()
            if not url.startswith(("https://", "http://")):
                raise HTTPException(status_code=400, detail="Enter a full http:// or https:// URL")
            link = {
                "id": uuid.uuid4().hex[:8], "url": url[:2000],
                "platform": str(payload.get("platform", "Other"))[:80],
                "account": str(payload.get("account", ""))[:200],
                "posted_at": str(payload.get("posted_at", ""))[:80],
                "status": "Not yet reported", "added_at": now(),
            }
            case.setdefault("links", []).append(link)
            case["updated_at"] = now()
            write_db(items)
            return link
    raise HTTPException(status_code=404, detail="Case not found")


@app.patch("/api/cases/{case_id}/links/{link_id}")
def update_link(case_id: str, link_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    allowed = {"Not yet reported", "Reported", "Awaiting response", "Removed (verified)", "Still accessible", "Unable to verify"}
    status = str(payload.get("status", ""))
    if status not in allowed:
        raise HTTPException(status_code=400, detail="Unsupported status")
    items = read_db()
    for case in items:
        if case["id"] == case_id:
            for link in case.get("links", []):
                if link["id"] == link_id:
                    link["status"] = status
                    link["updated_at"] = now()
                    case["updated_at"] = now()
                    write_db(items)
                    return link
    raise HTTPException(status_code=404, detail="Case or link not found")


@app.post("/api/cases/{case_id}/analyze")
async def analyze_media(case_id: str, file: UploadFile = File(...), reference: UploadFile | None = File(default=None)) -> dict[str, Any]:
    case = safe_case(case_id)
    allowed_ext = {".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv"}

    async def receive_upload(upload: UploadFile, prefix: str) -> tuple[Path, str, int]:
        filename = Path(upload.filename or "upload.mp4").name
        if Path(filename).suffix.lower() not in allowed_ext:
            raise HTTPException(status_code=400, detail="Upload a supported video file: MP4, MOV, M4V, WEBM, AVI, or MKV")
        target = UPLOADS / f"{case_id}_{prefix}_{uuid.uuid4().hex[:8]}_{filename}"
        size = 0
        try:
            with target.open("wb") as out:
                while chunk := await upload.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise HTTPException(status_code=413, detail=f"Maximum upload size is {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
                    out.write(chunk)
            if size == 0:
                raise HTTPException(status_code=400, detail="The uploaded file is empty")
            return target, filename, size
        except Exception:
            target.unlink(missing_ok=True)
            raise

    target, filename, size = await receive_upload(file, "evidence")
    ref_target: Path | None = None
    try:
        analysis = inspect_video(target)
        analysis["filename"] = filename
        upload_id = uuid.uuid4().hex[:12]
        analysis["upload_id"] = upload_id
        analysis["authenticity_assessment"] = "UNVERIFIED — available technical signals are not sufficient to determine authenticity."

        ref_info: dict[str, Any] | None = None
        ref_info_raw: dict[str, Any] | None = None
        ref_upload_id: str | None = None
        if reference and reference.filename:
            # Receive and validate both files before any remote write, avoiding a saved
            # primary file when the optional reference upload is invalid/oversized.
            ref_target, ref_name, _ = await receive_upload(reference, "reference")
            ref_info_raw = inspect_video(ref_target)
            ref_info_raw["filename"] = ref_name
            ref_upload_id = uuid.uuid4().hex[:12]
            ref_info = {k: ref_info_raw.get(k) for k in ("filename", "size_bytes", "sha256", "duration_seconds", "width", "height", "fps", "codec")}
            ref_info["upload_id"] = ref_upload_id
            analysis["reference_video"] = ref_info
            analysis["comparison_note"] = "A reference upload was supplied. Technical properties are compared only; this does not prove either video's authenticity."

        persist_media_to_firestore(case_id, upload_id, target, analysis)
        if ref_target and ref_info_raw and ref_upload_id:
            persist_media_to_firestore(case_id, ref_upload_id, ref_target, ref_info_raw)
        analysis["storage_note"] = "Original media bytes stored as private Firestore chunk documents; download is served through this API, not a public URL."

        evidence = {
            "id": uuid.uuid4().hex[:8], "type": "Video analysis", "filename": filename,
            "sha256": analysis["sha256"], "upload_id": upload_id, "added_at": now(),
            "storage": "firestore-chunks",
        }
        items = read_db()
        found = False
        for c in items:
            if c["id"] == case_id:
                c["analysis"] = analysis
                c.setdefault("evidence", []).append(evidence)
                c["updated_at"] = now()
                found = True
                break
        if not found:
            raise HTTPException(status_code=404, detail="Case not found")
        write_db(items)
        # The persistent copy is now in Firestore. Remove temporary local copies.
        target.unlink(missing_ok=True)
        if ref_target:
            ref_target.unlink(missing_ok=True)
        return analysis
    except Exception:
        target.unlink(missing_ok=True)
        if ref_target:
            ref_target.unlink(missing_ok=True)
        raise


@app.get("/api/cases/{case_id}/media/{upload_id}")
def download_media(case_id: str, upload_id: str) -> StreamingResponse:
    safe_case(case_id)
    try:
        client = get_firestore()
        media_ref = client.collection(COLLECTION_NAME).document(case_id).collection("media").document(upload_id)
        media_doc = media_ref.get()
        if not media_doc.exists:
            raise HTTPException(status_code=404, detail="Stored media not found")
        meta = media_doc.to_dict() or {}
        docs = list(media_ref.collection("chunks").stream())
        chunks = sorted((doc.to_dict() or {} for doc in docs), key=lambda x: int(x.get("index", -1)))
        expected = int(meta.get("chunk_count", -1))
        if expected < 1 or len(chunks) != expected or any(int(c.get("index", -1)) != i for i, c in enumerate(chunks)):
            raise HTTPException(status_code=500, detail="Stored media chunks are incomplete; do not treat this copy as complete evidence")
        filename = Path(str(meta.get("filename", "evidence.bin"))).name
        mime = str(meta.get("mime_type", "application/octet-stream"))

        decoded: list[bytes] = []
        digest = hashlib.sha256()
        for chunk in chunks:
            raw = base64.b64decode(chunk["data_b64"], validate=True)
            if int(chunk.get("byte_count", -1)) != len(raw):
                raise HTTPException(status_code=500, detail="Stored media chunk failed its length check")
            digest.update(raw)
            decoded.append(raw)
        expected_hash = str(meta.get("sha256", ""))
        if not expected_hash or digest.hexdigest() != expected_hash:
            raise HTTPException(status_code=500, detail="Stored media failed its SHA-256 integrity check")

        def content() -> Iterator[bytes]:
            yield from decoded

        return StreamingResponse(content(), media_type=mime, headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"})
    except HTTPException:
        raise
    except Exception as exc:
        raise firestore_error(exc) from exc


@app.post("/api/cases/{case_id}/response-plan")
async def response_plan(case_id: str) -> dict[str, Any]:
    case = safe_case(case_id)
    result = await generate_with_gemini(case, case.get("analysis"))
    items = read_db()
    for c in items:
        if c["id"] == case_id:
            c["response_plan"] = result
            c["updated_at"] = now()
            write_db(items)
            break
    return result


@app.get("/api/sample-videos/{filename}")
def sample_video(filename: str) -> FileResponse:
    allowed = {"synthetic_reference.mp4", "synthetic_variant.mp4", "README.txt"}
    if filename not in allowed:
        raise HTTPException(status_code=404, detail="Sample not found")
    path = ROOT / "sample_videos" / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="Sample not found")
    return FileResponse(path)


@app.get("/api/export/{case_id}")
def export_case(case_id: str) -> dict[str, Any]:
    case = safe_case(case_id)
    # Explicitly omit server file paths and raw uploaded content.
    return case
