import operator
from typing import Annotated, Literal, Optional, TypedDict

from pydantic import BaseModel, Field

WorkArrangement = Literal["remote", "hybrid", "on-site", "unknown"]
EmploymentType = Literal["full-time", "part-time", "contract", "internship", "unknown"]
PostingStatus = Literal["open", "closed", "unknown"]


class JobDetails(BaseModel):
    """A job posting that may match the search criteria."""

    title: str = Field(description="Job title as posted, e.g. 'Senior Computational Biologist'")
    company: str = Field(description="Hiring company")
    url: Optional[str] = Field(default=None, description="Posting or application page, if known")
    description: str = Field(default="", description="One or two sentences on what the role does")
    location: Optional[str] = Field(default=None, description="Location as stated on the page; null if not stated")
    work_arrangement: WorkArrangement = Field(default="unknown")
    employment_type: EmploymentType = Field(default="unknown")
    seniority: Optional[str] = Field(default=None, description="Seniority level as stated, e.g. 'senior', 'staff'")
    salary: Optional[str] = Field(default=None, description="Salary or pay range as stated; null if not stated")
    posted_date: Optional[str] = Field(default=None, description="Date the job was posted; null if not stated")
    application_deadline: Optional[str] = Field(default=None, description="Closing date, if stated")
    requirements: Optional[str] = Field(default=None, description="Key required skills/experience, as stated")
    matches_criteria: Literal["yes", "no", "unclear"] = Field(
        default="unclear", description="Whether the posting fits the search criteria"
    )
    relevance_score: int = Field(default=5, ge=1, le=10, description="1-10 fit for the search")
    source_urls: list[str] = Field(default_factory=list)


class JobPosting(JobDetails):
    """JobDetails plus fields the agent sets after extraction."""

    posting_status: PostingStatus = "unknown"
    resume_fit: Optional[int] = Field(default=None, ge=1, le=10, description="1-10 fit for the person's resume")
    fit_reason: Optional[str] = None


class ExtractionResult(BaseModel):
    """Job postings found on one web page. Empty if the page describes no concrete posting."""

    jobs: list[JobDetails] = Field(default_factory=list)


class Reflection(BaseModel):
    gaps: list[str] = Field(
        default_factory=list,
        description="Specific kinds of postings or sources still missing; empty if coverage is good",
    )


class JobReview(BaseModel):
    id: int
    posting_status: PostingStatus


class Consolidation(BaseModel):
    duplicate_groups: list[list[int]] = Field(
        default_factory=list,
        description="Each group lists the ids of records describing the same posting; omit jobs with no duplicate",
    )
    reviews: list[JobReview] = Field(default_factory=list, description="One entry per job id")


class ResumeFit(BaseModel):
    id: int
    score: int = Field(ge=1, le=10, description="10 = the resume strongly fits the role; 1 = little overlap")
    reason: str = Field(description="The concrete overlap or gap, in one short sentence")


class ResumeRanking(BaseModel):
    fits: list[ResumeFit] = Field(default_factory=list, description="One entry per job id")


class QueryPlan(BaseModel):
    queries: list[str]


class GraphState(TypedDict, total=False):
    profile: dict
    queries: list[str]
    pending_queries: list[str]
    search_results: Annotated[list[dict], operator.add]
    seen_urls: list[str]
    candidates: list[JobPosting]
    jobs: list[JobPosting]
    verified: list[str]
    gaps: list[str]
    iteration: int
    output_files: list[str]


class SearchTask(TypedDict):
    query: str
    max_results: int
