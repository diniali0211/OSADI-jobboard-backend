from sqlalchemy import Column, Integer, String, Float, Text, DateTime, ForeignKey
from datetime import datetime
from .connection import Base

# NOTE: This mirrors the EXISTING candidates table exactly (same DB, same
# table name). SQLAlchemy's create_all() will see this table already
# exists and will NOT touch it or its existing data — it only creates
# tables that don't exist yet (job_postings, recruiter_pins, candidate_job_links).

class Candidate(Base):
    __tablename__ = "candidates"

    id             = Column(Integer, primary_key=True, index=True)
    name           = Column(String)
    email          = Column(String)
    phone          = Column(String)
    location       = Column(String)
    score          = Column(Float)
    status         = Column(String, default="PENDING")
    resume_text    = Column(Text)
    created_at     = Column(DateTime, default=datetime.utcnow)
    abscond_date   = Column(String,   nullable=True)
    resume_url     = Column(String,   nullable=True)
    reject_reason  = Column(String,   nullable=True)
    recuiter_name  = Column(String,   nullable=True)
    hired_date     = Column(DateTime, nullable=True)
    role_applied   = Column(String,   nullable=True)
    # LEGACY — kept for backward compatibility with old data and the main
    # ATS backend, which still writes/reads this column directly. The job
    # board backend no longer reads or writes job_posting_id itself; a
    # candidate's relationship(s) to jobs now live in candidate_job_links,
    # which supports linking one candidate to MULTIPLE job postings.
    job_posting_id = Column(Integer,  ForeignKey("job_postings.id"), nullable=True)


class JobPosting(Base):
    __tablename__ = "job_postings"

    id                  = Column(Integer, primary_key=True, index=True)
    client              = Column(String)
    position_title      = Column(String)
    employment_type     = Column(String)
    location            = Column(String, nullable=True)
    recruiter           = Column(String, nullable=True)
    openings            = Column(Integer, default=1)
    date_open           = Column(DateTime, default=datetime.utcnow)
    date_filled         = Column(DateTime, nullable=True)
    remark              = Column(Text, nullable=True)
    status              = Column(String, default="OPEN")
    created_at          = Column(DateTime, default=datetime.utcnow)
    # Who created this posting — only this recruiter (verified via PIN) may
    # edit or delete it. Nullable so existing rows created before this
    # feature don't break; those rows simply have no enforced owner yet.
    created_by_recruiter = Column(String, nullable=True)


class CandidateJobLink(Base):
    """
    Join table enabling one candidate to be linked to MULTIPLE job postings.
    Status (KIV/REJECTED/OFFERED/HIRED/etc.) lives HERE, per job — not on
    the candidate — because the same person can be at different stages on
    different postings (e.g. rejected for Job A, hired for Job B).
    """
    __tablename__ = "candidate_job_links"

    id              = Column(Integer, primary_key=True, index=True)
    candidate_id    = Column(Integer, ForeignKey("candidates.id"), nullable=False, index=True)
    job_posting_id  = Column(Integer, ForeignKey("job_postings.id"), nullable=False, index=True)

    status          = Column(String, default="PENDING")
    reject_reason   = Column(String, nullable=True)
    recruiter_name  = Column(String, nullable=True)
    hired_date      = Column(DateTime, nullable=True)
    abscond_date    = Column(String,   nullable=True)

    created_at      = Column(DateTime, default=datetime.utcnow)


class RecruiterPin(Base):
    __tablename__ = "recruiter_pins"

    id             = Column(Integer, primary_key=True, index=True)
    recruiter_name = Column(String, unique=True, index=True)
    pin_hash       = Column(String)
    created_at     = Column(DateTime, default=datetime.utcnow)


# Read-only mirror of the existing settings table — used only to verify
# the admin password before letting someone set up a recruiter's PIN.
class Settings(Base):
    __tablename__ = "settings"

    id               = Column(Integer, primary_key=True, index=True)
    company_name     = Column(String, default="My Company")
    hr_name          = Column(String, default="")
    hr_email         = Column(String, default="")
    hiring_position  = Column(String, default="")
    min_score        = Column(Float,  default=50.0)
    data_retention   = Column(String, default="90")
    language         = Column(String, default="en")
    date_format      = Column(String, default="DD/MM/YYYY")
    app_password     = Column(String, default="admin123")
    delete_password  = Column(String, default="delete124")
