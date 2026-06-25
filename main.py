from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from datetime import datetime
import os
import httpx
import sqlalchemy as sa

from database.connection import get_db, engine, Base
from database.models import Candidate, JobPosting, RecruiterPin, Settings, CandidateJobLink
from database.crud import (
    create_job, update_job, delete_job, get_jobs_with_counts, get_job_owner, get_link_job_owner,
    get_job_candidates, get_or_create_link, check_and_mark_filled, update_link_decision,
    set_recruiter_pin, has_recruiter_pin, verify_recruiter_pin, get_unassigned_applicants,
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
        # Creates job_postings + recruiter_pins + candidate_job_links only.
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
            "ALTER TABLE job_postings ADD COLUMN IF NOT EXISTS created_by_recruiter VARCHAR",
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
    recruiter: str  # required — every job posting must have a known creator
    openings: int = 1
    remark: str | None = None
    status: str | None = None


class JobAuthPayload(BaseModel):
    # Sent alongside edit/delete requests to verify the requester is the
    # SAME recruiter who created this job posting.
    recruiter: str
    pin: str


class DecisionPayload(BaseModel):
    # link_id identifies WHICH job-relationship this decision applies to —
    # required now that a candidate can be linked to multiple jobs at once.
    link_id: int
    decision: str
    reason: str | None = None
    # Required for ALL decisions now (not just HIRED) — every action on a
    # candidate-job link is restricted to that job's owner.
    recruiter: str
    pin: str | None = None  # only required when decision == HIRED


class RecruiterPinSetup(BaseModel):
    recruiter_name: str
    pin: str
    admin_password: str


class RecruiterPinVerify(BaseModel):
    recruiter_name: str
    pin: str


class AuthVerify(BaseModel):
    password: str


class MoveApplicantPayload(BaseModel):
    # The recruiter moving this applicant — must own the target job.
    recruiter: str


@app.get("/health")
def health_check():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}


@app.post("/auth/verify")
async def verify_app_password(payload: AuthVerify, db: AsyncSession = Depends(get_db)):
    """Checks a password against the same app_password used by the main ATS.
    Used only to gate the job board frontend's login screen — read-only,
    no side effects."""
    result = await db.execute(select(Settings).where(Settings.id == 1))
    settings = result.scalars().first()
    correct_password = settings.app_password if settings else "admin123"

    if payload.password != correct_password:
        raise HTTPException(status_code=401, detail="Incorrect password")

    return {"status": "ok"}


# -------------------------
# Job Postings
# -------------------------

@app.post("/jobs")
async def create_job_posting(payload: JobPayload, db: AsyncSession = Depends(get_db)):
    pin_exists = await has_recruiter_pin(db, payload.recruiter)
    if not pin_exists:
        raise HTTPException(
            status_code=400,
            detail=f"'{payload.recruiter}' needs to set up a PIN before creating a job posting. "
                   f"Ask an admin to set one up under Recruiter PINs."
        )

    job = await create_job(db, payload.dict())
    return {"status": "ok", "job_id": job.id}


@app.get("/jobs")
async def list_jobs(db: AsyncSession = Depends(get_db)):
    return await get_jobs_with_counts(db)


class JobEditPayload(JobPayload):
    pin: str  # the creator's PIN, required to prove they're the owner


@app.put("/jobs/{job_id}")
async def edit_job_posting(job_id: int, payload: JobEditPayload, db: AsyncSession = Depends(get_db)):
    job_exists, owner = await get_job_owner(db, job_id)
    if not job_exists:
        raise HTTPException(status_code=404, detail="Job not found")

    if owner is None:
        raise HTTPException(
            status_code=403,
            detail="This posting predates recruiter ownership tracking and can't be edited. "
                   "Delete it and recreate it under your name instead."
        )

    if owner != payload.recruiter:
        raise HTTPException(
            status_code=403,
            detail=f"Only {owner} can edit this posting."
        )

    valid = await verify_recruiter_pin(db, payload.recruiter, payload.pin)
    if not valid:
        raise HTTPException(status_code=401, detail="Incorrect PIN.")

    job = await update_job(db, job_id, payload.dict())
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"status": "ok"}


@app.delete("/jobs/{job_id}")
async def remove_job_posting(job_id: int, payload: JobAuthPayload, db: AsyncSession = Depends(get_db)):
    job_exists, owner = await get_job_owner(db, job_id)
    if not job_exists:
        raise HTTPException(status_code=404, detail="Job not found")

    if owner is None:
        raise HTTPException(
            status_code=403,
            detail="This posting predates recruiter ownership tracking and can't be deleted through this flow. "
                   "Contact an admin if it needs to be removed."
        )

    if owner != payload.recruiter:
        raise HTTPException(
            status_code=403,
            detail=f"Only {owner} can delete this posting."
        )

    valid = await verify_recruiter_pin(db, payload.recruiter, payload.pin)
    if not valid:
        raise HTTPException(status_code=401, detail="Incorrect PIN.")

    success = await delete_job(db, job_id)
    if not success:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"status": "ok"}


@app.get("/candidates/{candidate_id}/resume-url")
async def resume_url_proxy(candidate_id: int):
    """
    The stored resume_url on a candidate is just an internal storage key
    (e.g. 'resume/abc123_file.pdf'), not a usable link. The main ATS holds
    the actual cloud storage logic and knows how to turn that key into a
    real, openable URL — so we ask it rather than duplicating that logic
    or guessing at a URL shape here.
    """
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(f"{ATS_BASE_URL}/resume-url/{candidate_id}")
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise HTTPException(status_code=404, detail="Resume not found")
        raise HTTPException(status_code=502, detail=f"Main ATS error: {e}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Couldn't reach the main ATS: {e}")


# -------------------------
# Public Applicant Portal
# A separate public site (not the recruiter job board) lets candidates
# self-submit a resume + the role they're applying for. They land here as
# UNASSIGNED — not yet linked to any job posting — until a recruiter moves
# them into one of their own open jobs. Any recruiter may view and move
# any applicant; ownership only applies once they're linked to a specific
# job (the existing job-ownership rules then take over).
# -------------------------

@app.post("/applicants")
async def submit_applicant(
    file: UploadFile = File(...),
    name: str = Form(...),
    email: str = Form(...),
    phone: str = Form(...),
    role_applied: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    """
    Public endpoint — no auth, no recruiter identity. Forwards the resume
    to the main ATS for the same OCR + AI parsing every other upload path
    uses, then stores the candidate WITHOUT linking them to any job yet.
    """
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
        existing_id_raw = result.get("existing_candidate_id")
        existing_id = None
        if existing_id_raw is not None:
            try:
                existing_id = int(existing_id_raw)
            except (TypeError, ValueError):
                existing_id = None

        if existing_id is not None:
            # Already in the system. Update their role_applied to reflect
            # this latest application, but don't touch any job links —
            # if they're already linked somewhere, that's left alone; if
            # not, they simply stay (or become) an unassigned applicant.
            cand_result = await db.execute(select(Candidate).where(Candidate.id == existing_id))
            candidate = cand_result.scalars().first()
            if candidate:
                candidate.role_applied = role_applied
                await db.commit()

        return {
            "duplicate": True,
            "existing_candidate_id": existing_id,
            "message": result.get("message"),
        }

    candidate_id_raw = result.get("candidate_id")
    candidate_id = None
    if candidate_id_raw is not None:
        try:
            candidate_id = int(candidate_id_raw)
        except (TypeError, ValueError):
            candidate_id = None

    if candidate_id is not None:
        cand_result = await db.execute(select(Candidate).where(Candidate.id == candidate_id))
        candidate = cand_result.scalars().first()
        if candidate:
            # The portal collects name/email/phone/role directly — prefer
            # these over whatever the resume parser guessed, since the
            # candidate typed them in themselves just now.
            candidate.name = name
            candidate.email = email
            candidate.phone = phone
            candidate.role_applied = role_applied
            await db.commit()

    return {
        "duplicate": False,
        "candidate_id": candidate_id,
        "analysis": result.get("analysis"),
    }


@app.get("/applicants")
async def list_applicants(db: AsyncSession = Depends(get_db)):
    """Recruiter-facing: everyone who applied via the public portal and
    hasn't been moved into a job posting yet."""
    applicants = await get_unassigned_applicants(db)
    return [
        {
            "id": c.id,
            "name": c.name,
            "email": c.email,
            "phone": c.phone,
            "location": c.location,
            "score": c.score,
            "role_applied": c.role_applied,
            "resume_text": c.resume_text,
            "resume_url": c.resume_url,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        }
        for c in applicants
    ]


@app.post("/applicants/{candidate_id}/move-to-job/{job_id}")
async def move_applicant_to_job(
    candidate_id: int,
    job_id: int,
    payload: MoveApplicantPayload,
    db: AsyncSession = Depends(get_db),
):
    """Links an unassigned applicant to a job posting. The recruiter must
    own the TARGET job — same rule as uploading directly to it."""
    job_result = await db.execute(select(JobPosting).where(JobPosting.id == job_id))
    job = job_result.scalars().first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.created_by_recruiter is None:
        raise HTTPException(
            status_code=403,
            detail="This posting predates recruiter ownership tracking, so applicants can't be moved into it."
        )

    if job.created_by_recruiter != payload.recruiter:
        raise HTTPException(
            status_code=403,
            detail=f"Only {job.created_by_recruiter} can move applicants into this posting."
        )

    cand_result = await db.execute(select(Candidate).where(Candidate.id == candidate_id))
    candidate = cand_result.scalars().first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")

    link, created = await get_or_create_link(db, candidate_id, job_id)
    return {"status": "ok", "link_id": link.id, "already_linked": not created}


@app.get("/jobs/{job_id}/candidates")
async def candidates_for_job(job_id: int, db: AsyncSession = Depends(get_db)):
    pairs = await get_job_candidates(db, job_id)
    return [
        {
            "link_id": link.id,
            "id": c.id, "name": c.name, "email": c.email, "phone": c.phone,
            "location": c.location, "score": c.score,
            "status": link.status,
            "resume_text": c.resume_text, "resume_url": c.resume_url,
            "reject_reason": link.reject_reason, "recruiter_name": link.recruiter_name,
            "created_at": link.created_at.isoformat() if link.created_at else None,
            "hired_date": link.hired_date.isoformat() if link.hired_date else None,
        }
        for c, link in pairs
    ]


@app.post("/jobs/{job_id}/candidates")
async def analyze_for_job(
    job_id: int,
    file: UploadFile = File(...),
    recruiter: str = Form(...),
    db: AsyncSession = Depends(get_db)
):
    """
    Forwards the resume to the EXISTING ATS backend for OCR + AI parsing
    (no duplicate logic here). If the candidate is brand new, links them
    to this job. If the ATS reports they already exist (uploaded before,
    possibly for a different job), links the EXISTING candidate record to
    THIS job too — one candidate can be linked to multiple job postings.

    Only the job's owner may upload against it — non-owners are fully
    view-only on someone else's posting.
    """
    job_result = await db.execute(select(JobPosting).where(JobPosting.id == job_id))
    job = job_result.scalars().first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.created_by_recruiter is None:
        raise HTTPException(
            status_code=403,
            detail="This posting predates recruiter ownership tracking, so no one can upload to it through this flow."
        )

    if job.created_by_recruiter != recruiter:
        raise HTTPException(
            status_code=403,
            detail=f"Only {job.created_by_recruiter} can upload candidates to this posting."
        )

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
        existing_id_raw = result.get("existing_candidate_id")
        if not existing_id_raw:
            # ATS flagged a duplicate but didn't tell us which candidate —
            # nothing we can safely link, so just report it as before.
            return {
                "duplicate": True,
                "linked": False,
                "message": result.get("message"),
                "existing_candidate_id": None,
                "analysis": result.get("analysis"),
            }

        # The main ATS's JSON response may serialize this as a string —
        # Postgres (unlike SQLite) raises a hard error comparing a string
        # against an Integer column, so cast explicitly before any query.
        try:
            existing_id = int(existing_id_raw)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=502,
                detail=f"Main ATS returned an unusable candidate id: {existing_id_raw!r}"
            )

        link, created = await get_or_create_link(db, existing_id, job_id)

        return {
            "duplicate": True,
            "linked": True,
            "already_linked_to_this_job": not created,
            "message": result.get("message"),
            "existing_candidate_id": existing_id,
            "link_id": link.id,
            "analysis": result.get("analysis"),
        }

    candidate_id_raw = result.get("candidate_id")
    candidate_id = None
    if candidate_id_raw is not None:
        try:
            candidate_id = int(candidate_id_raw)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=502,
                detail=f"Main ATS returned an unusable candidate id: {candidate_id_raw!r}"
            )

    cand_result = await db.execute(select(Candidate).where(Candidate.id == candidate_id))
    candidate = cand_result.scalars().first()
    link_id = None
    if candidate:
        # Keep legacy column populated for any old code path that still
        # reads it directly, but the real relationship lives in the link table.
        candidate.role_applied = job.position_title
        await db.commit()
        link, _ = await get_or_create_link(db, candidate_id, job_id)
        link_id = link.id

    return {
        "duplicate": False,
        "linked": True,
        "candidate_id": candidate_id,
        "link_id": link_id,
        "analysis": result.get("analysis"),
    }


# -------------------------
# Decisions (KIV / Reject / Hire) — PIN-protected for HIRED
# Operates on a specific candidate-job LINK, since the same candidate can
# have a different status on different job postings.
# -------------------------

@app.post("/decision")
async def set_decision(payload: DecisionPayload, db: AsyncSession = Depends(get_db)):
    decision = payload.decision.upper()

    if decision not in VALID_DECISIONS:
        raise HTTPException(status_code=400, detail="Invalid decision")

    if decision == "REJECTED":
        if not payload.reason or payload.reason not in VALID_REJECT_REASONS:
            raise HTTPException(status_code=400, detail="Valid reject reason required")

    # Ownership check applies to EVERY decision (KIV, REJECTED, HIRED, etc.) —
    # only the recruiter who created this job posting may act on candidates
    # linked to it. Non-owners are fully view-only.
    link_exists, _job_id, owner = await get_link_job_owner(db, payload.link_id)
    if not link_exists:
        raise HTTPException(status_code=404, detail="Candidate-job link not found")

    if owner is None:
        raise HTTPException(
            status_code=403,
            detail="This posting predates recruiter ownership tracking, so candidates on it can't be updated through this flow."
        )

    if owner != payload.recruiter:
        raise HTTPException(
            status_code=403,
            detail=f"Only {owner} can update candidates on this posting."
        )

    if decision == "HIRED":
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

    updated_link = await update_link_decision(db, payload.link_id, decision, payload.reason, payload.recruiter)
    if not updated_link:
        raise HTTPException(status_code=404, detail="Candidate-job link not found")

    if decision == "HIRED":
        await check_and_mark_filled(db, updated_link.job_posting_id)

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
