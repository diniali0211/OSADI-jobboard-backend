from fastapi import FastAPI, UploadFile, File, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from datetime import datetime
import os
import httpx
import sqlalchemy as sa

from database.connection import get_db, engine, Base
from database.models import Candidate, JobPosting, RecruiterPin, Settings
from database.crud import (
    create_job, update_job, delete_job, get_jobs_with_counts,
    get_job_candidates, check_and_mark_filled, update_decision,
    set_recruiter_pin, has_recruiter_pin, verify_recruiter_pin,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

print("JOB BOARD BACKEND IS RUNNING")

# URL of your EXISTING ATS backend — used only to forward resumes for
# OCR + AI parsing. This service never duplicates that logic.
ATS_BASE_URL = os.getenv("ATS_BASE_URL", "https://osadiatsinitial-production.up.railway.app")

app = FastAPI(title="OSADI Job Board")

@app.on_event("startup")
async def startup():
    async with engine.begin() as conn:
        # Creates job_postings + recruiter_pins only.
        # candidates + settings already exist and are left untouched.
        await conn.run_sync(Base.metadata.create_all)

        # Safe, additive-only migration: adds new nullable columns to the
        # EXISTING candidates table if they don't already exist. This does
        # not modify or remove any existing data, and the original ATS
        # backend simply ignores these new columns.
        migrations = [
            "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS job_posting_id INTEGER",
            "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS role_applied VARCHAR",
            "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS hired_date TIMESTAMP",
        ]
        for stmt in migrations:
            try:
                await conn.execute(sa.text(stmt))
            except Exception as e:
                print(f"Migration skipped (likely already applied): {e}")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

VALID_DECISIONS = {"APPROVED", "REJECTED", "KIV", "OFFERED", "HIRED", "RESIGNED", "ABSCONDED"}
VALID_REJECT_REASONS = {"INCOMPLETE", "LOW_SKILL", "INSTRUCTIONS", "LEVEL_MISMATCH", "CULTURE", "VETTING"}


class JobPayload(BaseModel):
    client: str
    position_title: str
    employment_type: str
    location: str | None = None
    recruiter: str | None = None
    openings: int = 1
    remark: str | None = None
    status: str | None = None


class DecisionPayload(BaseModel):
    candidate_id: str
    decision: str
    reason: str | None = None
    recruiter: str | None = None
    pin: str | None = None


class RecruiterPinSetup(BaseModel):
    recruiter_name: str
    pin: str
    admin_password: str


class RecruiterPinVerify(BaseModel):
    recruiter_name: str
    pin: str


@app.get("/health")
def health_check():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}


# -------------------------
# Job Postings
# -------------------------

@app.post("/jobs")
async def create_job_posting(payload: JobPayload, db: AsyncSession = Depends(get_db)):
    job = await create_job(db, payload.dict())
    return {"status": "ok", "job_id": job.id}


@app.get("/jobs")
async def list_jobs(db: AsyncSession = Depends(get_db)):
    return await get_jobs_with_counts(db)


@app.put("/jobs/{job_id}")
async def edit_job_posting(job_id: int, payload: JobPayload, db: AsyncSession = Depends(get_db)):
    job = await update_job(db, job_id, payload.dict())
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"status": "ok"}


@app.delete("/jobs/{job_id}")
async def remove_job_posting(job_id: int, db: AsyncSession = Depends(get_db)):
    success = await delete_job(db, job_id)
    if not success:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"status": "ok"}


@app.get("/jobs/{job_id}/candidates")
async def candidates_for_job(job_id: int, db: AsyncSession = Depends(get_db)):
    candidates = await get_job_candidates(db, job_id)
    return [
        {
            "id": c.id, "name": c.name, "email": c.email, "phone": c.phone,
            "location": c.location, "score": c.score, "status": c.status,
            "resume_text": c.resume_text, "resume_url": c.resume_url,
            "reject_reason": c.reject_reason, "recruiter_name": c.recuiter_name,
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "hired_date": c.hired_date.isoformat() if c.hired_date else None,
        }
        for c in candidates
    ]


@app.post("/jobs/{job_id}/candidates")
async def analyze_for_job(
    job_id: int,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db)
):
    """
    Forwards the resume to the EXISTING ATS backend for OCR + AI parsing
    (no duplicate logic here), then links the resulting candidate record
    to this job posting in the shared database.
    """
    job_result = await db.execute(select(JobPosting).where(JobPosting.id == job_id))
    job = job_result.scalars().first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    file_bytes = await file.read()

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                f"{ATS_BASE_URL}/analyze",
                files={"file": (file.filename, file_bytes, file.content_type)}
            )
        response.raise_for_status()
        result = response.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Resume analysis service error: {e}")

    if result.get("duplicate"):
        return {
            "duplicate": True,
            "message": result.get("message"),
            "existing_candidate_id": result.get("existing_candidate_id"),
            "analysis": result.get("analysis"),
        }

    candidate_id = result.get("candidate_id")

    cand_result = await db.execute(select(Candidate).where(Candidate.id == candidate_id))
    candidate = cand_result.scalars().first()
    if candidate:
        candidate.job_posting_id = job_id
        candidate.role_applied = job.position_title
        await db.commit()

    return {"duplicate": False, "candidate_id": candidate_id, "analysis": result.get("analysis")}


# -------------------------
# Decisions (KIV / Reject / Hire) — PIN-protected for HIRED
# -------------------------

@app.post("/decision")
async def set_decision(payload: DecisionPayload, db: AsyncSession = Depends(get_db)):
    decision = payload.decision.upper()

    if decision not in VALID_DECISIONS:
        raise HTTPException(status_code=400, detail="Invalid decision")

    if decision == "REJECTED":
        if not payload.reason or payload.reason not in VALID_REJECT_REASONS:
            raise HTTPException(status_code=400, detail="Valid reject reason required")

    if decision == "HIRED":
        if not payload.recruiter:
            raise HTTPException(status_code=400, detail="Recruiter name is required to mark as hired")
        if not payload.pin:
            raise HTTPException(status_code=400, detail="PIN is required to confirm this hire")

        pin_exists = await has_recruiter_pin(db, payload.recruiter)
        if not pin_exists:
            raise HTTPException(
                status_code=400,
                detail=f"'{payload.recruiter}' has no PIN set up yet. Ask an admin to set one up in Settings."
            )

        valid = await verify_recruiter_pin(db, payload.recruiter, payload.pin)
        if not valid:
            raise HTTPException(status_code=401, detail="Incorrect PIN. This hire was not credited.")

    updated = await update_decision(db, payload.candidate_id, decision, payload.reason, payload.recruiter)
    if not updated:
        raise HTTPException(status_code=404, detail="Candidate not found")

    if decision == "HIRED" and getattr(updated, "job_posting_id", None):
        await check_and_mark_filled(db, updated.job_posting_id)

    return {"status": "ok"}


# -------------------------
# Recruiter PINs
# -------------------------

@app.post("/recruiter-pins/setup")
async def setup_recruiter_pin(payload: RecruiterPinSetup, db: AsyncSession = Depends(get_db)):
    """Admin-only: register or reset a recruiter's personal PIN.
    Verifies against the SAME app password used to log into the main ATS."""
    result = await db.execute(select(Settings).where(Settings.id == 1))
    settings = result.scalars().first()
    correct_admin_pw = settings.app_password if settings else "admin123"

    if payload.admin_password != correct_admin_pw:
        raise HTTPException(status_code=401, detail="Invalid admin password")

    if len(payload.pin.strip()) < 4:
        raise HTTPException(status_code=400, detail="PIN must be at least 4 characters")

    await set_recruiter_pin(db, payload.recruiter_name, payload.pin)
    return {"status": "ok", "message": f"PIN set for {payload.recruiter_name}"}


@app.post("/recruiter-pins/verify")
async def verify_pin_endpoint(payload: RecruiterPinVerify, db: AsyncSession = Depends(get_db)):
    valid = await verify_recruiter_pin(db, payload.recruiter_name, payload.pin)
    if not valid:
        raise HTTPException(status_code=401, detail="Incorrect PIN")
    return {"status": "ok"}


@app.get("/recruiter-pins/status/{recruiter_name}")
async def pin_status(recruiter_name: str, db: AsyncSession = Depends(get_db)):
    exists = await has_recruiter_pin(db, recruiter_name)
    return {"recruiter_name": recruiter_name, "pin_set": exists}
