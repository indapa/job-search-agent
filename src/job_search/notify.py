"""The nightly email (via an SNS topic): postings not emailed before, ranked by resume fit."""

import os
from urllib.parse import quote

from .nodes import job_key
from .output import is_closed, report_sections, sort_jobs
from .state import JobPosting

MAX_LISTED = 15
SNS_SUBJECT_LIMIT = 100


def worth_listing(j: JobPosting) -> bool:
    return j.matches_criteria != "no" and not is_closed(j)


def new_postings(jobs: list[JobPosting], new_keys: set[str]) -> list[JobPosting]:
    return sort_jobs([j for j in jobs if job_key(j) in new_keys and worth_listing(j)])


def subject(jobs: list[JobPosting], new_keys: set[str], stamp: str) -> str:
    listed = new_postings(jobs, new_keys)
    text = f"job-search: {len(listed)} new posting{'' if len(listed) == 1 else 's'}"
    if best := max((j.resume_fit for j in listed if j.resume_fit), default=None):
        text += f" (best fit {best}/10)"
    return f"{text}, {stamp}"[:SNS_SUBJECT_LIMIT]


def _entry(n: int, j: JobPosting) -> list[str]:
    fit = f"{j.resume_fit}/10" if j.resume_fit else f"keyword fit {j.relevance_score}/10"
    arrangement = None if j.work_arrangement == "unknown" else j.work_arrangement
    facts = ", ".join(filter(None, (j.location, arrangement, j.salary)))
    lines = [f"{n}. [{fit}] {j.title} at {j.company}" + (f" ({facts})" if facts else "")]
    if j.fit_reason:
        lines.append(f"   {j.fit_reason}")
    lines.append(f"   {j.url or (j.source_urls[0] if j.source_urls else '(no link)')}")
    return lines


def console_url(bucket: str, prefix: str) -> str:
    region = os.environ.get("AWS_REGION", "us-west-1")
    return f"https://s3.console.aws.amazon.com/s3/buckets/{bucket}?region={region}&prefix={quote(prefix, safe='/')}"


def message(jobs: list[JobPosting], new_keys: set[str], bucket: str, keys: dict[str, str], stamp: str) -> str:
    """keys maps a file type ("pdf", "csv") to its S3 key."""
    listed = new_postings(jobs, new_keys)
    repeats = sum(worth_listing(j) for j in jobs) - len(listed)
    summary, _ = report_sections(jobs)

    lines = [f"Job search for {stamp}. {summary}", ""]
    if listed:
        lines += [f"New since earlier runs ({len(listed)}), best fit to your resume first:", ""]
        for n, j in enumerate(listed[:MAX_LISTED], 1):
            lines += _entry(n, j) + [""]
        if len(listed) > MAX_LISTED:
            lines += [f"... and {len(listed) - MAX_LISTED} more new postings in the report.", ""]
    else:
        lines += ["No new postings this time.", ""]
    if repeats:
        lines += [f"{repeats} posting{' is' if repeats == 1 else 's are'} from earlier runs and still listed "
                  "(see the report).", ""]

    folder = f"reports/{stamp}/"
    lines.append("Report:")
    for kind in ("pdf", "csv"):
        if kind in keys:
            lines.append(f"  {kind.upper()}: s3://{bucket}/{keys[kind]}")
    lines += [
        f"  S3 console: {console_url(bucket, folder)}",
        f"  Download:   aws s3 cp s3://{bucket}/{folder} . --recursive",
        "",
        "Before applying, check that the posting is still open on the company's own careers page.",
    ]
    return "\n".join(lines) + "\n"


def publish(topic_arn: str, jobs: list[JobPosting], new_keys: set[str], bucket: str, keys: dict[str, str],
            stamp: str, client=None) -> str:
    """Publish the nightly email to SNS; returns the message ID."""
    if client is None:
        import boto3

        client = boto3.client("sns")
    resp = client.publish(TopicArn=topic_arn, Subject=subject(jobs, new_keys, stamp),
                          Message=message(jobs, new_keys, bucket, keys, stamp))
    return resp["MessageId"]
