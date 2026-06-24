import hashlib
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from .models import Candidate, JobPosting, RecruiterPin, CandidateJobLink


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
        created_by_recruiter = data.get("recruiter"),
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)
    return job


async def get_job_owner(db: AsyncSession, job_id: int):
    """Returns (job_exists, created_by_recruiter). created_by_recruiter is
    None both when the job doesn't exist AND when it predates the
    ownership feature — callers must check job_exists to tell those apart."""
    result = await db.execute(select(JobPosting).where(JobPosting.id == job_id))
    job = result.scalars().first()
    if not job:
        return False, None
    return True, job.created_by_recruiter


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

    # Clean up any links pointing at this job so they don't become orphans.
    links_result = await db.execute(
        select(CandidateJobLink).where(CandidateJobLink.job_posting_id == job_id)
    )
    for link in links_result.scalars().all():
        await db.delete(link)

    await db.delete(job)
    await db.commit()
    return True


async def get_jobs_with_counts(db: AsyncSession):
    jobs_result = await db.execute(select(JobPosting))
    jobs = jobs_result.scalars().all()

    output = []

    for job in jobs:
        links_result = await db.execute(
            select(CandidateJobLink).where(CandidateJobLink.job_posting_id == job.id)
        )
        links = links_result.scalars().all()

        submitted   = len(links)
        shortlisted = sum(1 for l in links if l.status in ("KIV", "APPROVED"))
        offered     = sum(1 for l in links if l.status in ("OFFERED", "HIRED"))
        hired       = sum(1 for l in links if l.status == "HIRED")
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
            "created_by_recruiter": job.created_by_recruiter,
        })

    return output


async def get_job_candidates(db: AsyncSession, job_id: int):
    """
    Returns (Candidate, CandidateJobLink) pairs for everyone linked to this
    job. A candidate appears once per link — if somehow linked twice to the
    same job (shouldn't happen via the API) they'd show twice; callers
    should treat link.id as the unique row identity, not candidate.id.
    """
    result = await db.execute(
        select(Candidate, CandidateJobLink)
        .join(CandidateJobLink, CandidateJobLink.candidate_id == Candidate.id)
        .where(CandidateJobLink.job_posting_id == job_id)
    )
    return result.all()


async def get_or_create_link(db: AsyncSession, candidate_id: int, job_id: int):
    """
    Links a candidate to a job if not already linked. If a link already
    exists between this exact candidate and this exact job, returns it
    unchanged (prevents duplicate rows from a second upload of the same
    resume against the same job).
    """
    existing = await db.execute(
        select(CandidateJobLink).where(
            CandidateJobLink.candidate_id == candidate_id,
            CandidateJobLink.job_posting_id == job_id,
        )
    )
    link = existing.scalars().first()
    if link:
        return link, False

    link = CandidateJobLink(candidate_id=candidate_id, job_posting_id=job_id)
    db.add(link)
    await db.commit()
    await db.refresh(link)
    return link, True


async def check_and_mark_filled(db: AsyncSession, job_id: int):
    result = await db.execute(select(JobPosting).where(JobPosting.id == job_id))
    job = result.scalars().first()
    if not job:
        return

    links_result = await db.execute(
        select(CandidateJobLink).where(
            CandidateJobLink.job_posting_id == job_id,
            CandidateJobLink.status == "HIRED",
        )
    )
    hired_count = len(links_result.scalars().all())

    if hired_count >= (job.openings or 0) and job.status != "FILLED":
        job.status = "FILLED"
        job.date_filled = datetime.utcnow()
        await db.commit()


# -----------------------------
# DECISIONS (operate on a candidate_job_links row, not the candidate directly)
# -----------------------------

async def update_link_decision(db: AsyncSession, link_id: int, decision: str, reason: str = None, recruiter: str = None):
    result = await db.execute(select(CandidateJobLink).where(CandidateJobLink.id == link_id))
    link = result.scalars().first()

    if not link:
        return None

    link.status = decision

    if decision == "ABSCONDED" and reason:
        link.abscond_date = reason
    elif decision == "REJECTED" and reason:
        link.reject_reason = reason
    elif decision == "HIRED":
        if recruiter:
            link.recruiter_name = recruiter
        link.hired_date = datetime.utcnow()

    await db.commit()
    await db.refresh(link)

    return link
