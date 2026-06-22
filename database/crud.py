import hashlib
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from .models import Candidate, JobPosting, RecruiterPin


# -----------------------------
# RECRUITER PINS
# -----------------------------

def hash_pin(pin: str) -> str:
    return hashlib.sha256(pin.strip().encode()).hexdigest()


async def set_recruiter_pin(db: AsyncSession, recruiter_name: str, pin: str):
    result = await db.execute(
        select(RecruiterPin).where(RecruiterPin.recruiter_name == recruiter_name)
    )
    rp = result.scalars().first()

    if not rp:
        rp = RecruiterPin(recruiter_name=recruiter_name, pin_hash=hash_pin(pin))
        db.add(rp)
    else:
        rp.pin_hash = hash_pin(pin)

    await db.commit()
    await db.refresh(rp)
    return rp


async def has_recruiter_pin(db: AsyncSession, recruiter_name: str) -> bool:
    result = await db.execute(
        select(RecruiterPin).where(RecruiterPin.recruiter_name == recruiter_name)
    )
    return result.scalars().first() is not None


async def verify_recruiter_pin(db: AsyncSession, recruiter_name: str, pin: str) -> bool:
    result = await db.execute(
        select(RecruiterPin).where(RecruiterPin.recruiter_name == recruiter_name)
    )
    rp = result.scalars().first()

    if not rp:
        return False

    return rp.pin_hash == hash_pin(pin)


# -----------------------------
# JOB POSTINGS
# -----------------------------

async def create_job(db: AsyncSession, data: dict):
    job = JobPosting(
        client          = data.get("client"),
        position_title  = data.get("position_title"),
        employment_type = data.get("employment_type"),
        location        = data.get("location"),
        recruiter       = data.get("recruiter"),
        openings        = data.get("openings", 1),
        remark          = data.get("remark"),
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)
    return job


async def update_job(db: AsyncSession, job_id: int, data: dict):
    result = await db.execute(select(JobPosting).where(JobPosting.id == job_id))
    job = result.scalars().first()
    if not job:
        return None

    for field in ["client", "position_title", "employment_type", "location",
                  "recruiter", "openings", "remark", "status"]:
        if field in data and data[field] is not None:
            setattr(job, field, data[field])

    await db.commit()
    await db.refresh(job)
    return job


async def delete_job(db: AsyncSession, job_id: int) -> bool:
    result = await db.execute(select(JobPosting).where(JobPosting.id == job_id))
    job = result.scalars().first()
    if not job:
        return False
    await db.delete(job)
    await db.commit()
    return True


async def get_jobs_with_counts(db: AsyncSession):
    jobs_result = await db.execute(select(JobPosting))
    jobs = jobs_result.scalars().all()

    output = []

    for job in jobs:
        cand_result = await db.execute(
            select(Candidate).where(Candidate.job_posting_id == job.id)
        )
        candidates = cand_result.scalars().all()

        submitted   = len(candidates)
        shortlisted = sum(1 for c in candidates if c.status in ("KIV", "APPROVED"))
        offered     = sum(1 for c in candidates if c.status in ("OFFERED", "HIRED"))
        hired       = sum(1 for c in candidates if c.status == "HIRED")
        remaining   = max((job.openings or 0) - hired, 0)

        output.append({
            "id":              job.id,
            "client":          job.client,
            "position_title":  job.position_title,
            "employment_type": job.employment_type,
            "location":        job.location,
            "recruiter":       job.recruiter,
            "openings":        job.openings,
            "date_open":       job.date_open.isoformat() if job.date_open else None,
            "date_filled":     job.date_filled.isoformat() if job.date_filled else None,
            "remark":          job.remark,
            "status":          job.status,
            "submitted":       submitted,
            "shortlisted":     shortlisted,
            "offered":         offered,
            "hired":           hired,
            "remaining":       remaining,
        })

    return output


async def get_job_candidates(db: AsyncSession, job_id: int):
    result = await db.execute(
        select(Candidate).where(Candidate.job_posting_id == job_id)
    )
    return result.scalars().all()


async def check_and_mark_filled(db: AsyncSession, job_id: int):
    result = await db.execute(select(JobPosting).where(JobPosting.id == job_id))
    job = result.scalars().first()
    if not job:
        return

    cand_result = await db.execute(
        select(Candidate).where(Candidate.job_posting_id == job_id, Candidate.status == "HIRED")
    )
    hired_count = len(cand_result.scalars().all())

    if hired_count >= (job.openings or 0) and job.status != "FILLED":
        job.status = "FILLED"
        job.date_filled = datetime.utcnow()
        await db.commit()


# -----------------------------
# DECISIONS (operates on shared candidates table)
# -----------------------------

async def update_decision(db: AsyncSession, candidate_id, decision: str, reason: str = None, recruiter: str = None):
    result = await db.execute(select(Candidate).where(Candidate.id == candidate_id))
    candidate = result.scalars().first()

    if not candidate:
        return None

    candidate.status = decision

    if decision == "ABSCONDED" and reason:
        candidate.abscond_date = reason
    elif decision == "REJECTED" and reason:
        candidate.reject_reason = reason
    elif decision == "HIRED":
        if recruiter:
            candidate.recuiter_name = recruiter
        candidate.hired_date = datetime.utcnow()

    await db.commit()
    await db.refresh(candidate)

    return candidate
