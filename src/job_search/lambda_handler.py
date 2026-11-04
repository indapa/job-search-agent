"""AWS Lambda entry point: run the job search, store the report in S3 and email a summary.

Settings come from the bundled config.yaml, then the invocation event, which can hold any
config key (the nightly schedule passes a smaller run, e.g. {"queries_per_round": 6}).

Environment (set by infra/app.yaml):
    BUCKET                 resumes are read from resume/, reports written to reports/<date>/, and
                           state/seen_jobs.json remembers which postings were already emailed
    NOTIFY_TOPIC_ARN       SNS topic for the email (optional)
    GOOGLE_API_KEY_PARAM   SSM SecureString names for the API keys
    TAVILY_API_KEY_PARAM   (default /job-search/<KEY>)

The email is sent before seen_jobs.json is updated, so if sending fails, the next run still
reports those postings as new.
"""

import json
import logging
import os
import shutil
from datetime import date
from pathlib import Path

import boto3

from . import notify
from .main import API_KEYS, DEFAULTS, load_profile, quiet_noisy_loggers, run
from .nodes import job_key

CONFIG = Path(__file__).resolve().parents[2] / "config.yaml"
WORK_DIR = Path("/tmp/job-search")
RESUME_PREFIX = "resume/"
SEEN_KEY = "state/seen_jobs.json"

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def load_api_keys(ssm=None) -> None:
    """Fill API keys missing from the environment from SSM; run() reports any still missing."""
    names = {k: os.environ.get(f"{k}_PARAM", f"/job-search/{k}") for k in API_KEYS if not os.environ.get(k)}
    if not names:
        return
    ssm = ssm or boto3.client("ssm")
    found = {p["Name"]: p["Value"] for p in ssm.get_parameters(Names=list(names.values()), WithDecryption=True)["Parameters"]}
    for key, name in names.items():
        if name in found:
            os.environ[key] = found[name]


def download_resumes(s3, bucket: str, dest: Path) -> int:
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    n = 0
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=RESUME_PREFIX):
        for obj in page.get("Contents", []):
            name = obj["Key"][len(RESUME_PREFIX):]
            if name and "/" not in name:
                s3.download_file(bucket, obj["Key"], str(dest / name))
                n += 1
    return n


def read_seen(s3, bucket: str) -> dict[str, str]:
    """job_key -> date the posting was first emailed."""
    try:
        return json.loads(s3.get_object(Bucket=bucket, Key=SEEN_KEY)["Body"].read())
    except s3.exceptions.NoSuchKey:
        return {}


def handler(event: dict | None, context: object = None) -> dict:
    quiet_noisy_loggers()
    event = dict(event or {})
    if unknown := sorted(set(event) - set(DEFAULTS)):
        raise ValueError(f"Unknown setting(s) in event: {', '.join(unknown)}")

    bucket = os.environ["BUCKET"]
    s3 = boto3.client("s3")
    load_api_keys()

    resume_dir, output_dir = WORK_DIR / "resume", WORK_DIR / "output"
    if not download_resumes(s3, bucket, resume_dir):
        logger.warning("No resume in s3://%s/%s; ranking by keyword relevance only", bucket, RESUME_PREFIX)
    shutil.rmtree(output_dir, ignore_errors=True)

    stamp = date.today().isoformat()
    profile = load_profile(CONFIG, {**event, "resume_dir": str(resume_dir)})
    logger.info("Settings: %s", json.dumps({k: profile[k] for k in DEFAULTS if k != "resume_dir"}))
    result = run(profile, str(output_dir))
    jobs = result.get("jobs", [])

    keys = {}
    for path in map(Path, result.get("output_files", [])):
        keys[path.suffix.lstrip(".")] = key = f"reports/{stamp}/{path.name}"
        s3.upload_file(str(path), bucket, key)

    seen = read_seen(s3, bucket)
    new_keys = {job_key(j) for j in jobs} - set(seen)
    message_id = None
    if topic_arn := os.environ.get("NOTIFY_TOPIC_ARN"):
        message_id = notify.publish(topic_arn, jobs, new_keys, bucket, keys, stamp)
        logger.info("Email sent (SNS message %s)", message_id)

    seen.update(dict.fromkeys(new_keys, stamp))
    s3.put_object(Bucket=bucket, Key=SEEN_KEY, Body=json.dumps(seen, indent=1, sort_keys=True).encode(),
                  ContentType="application/json")

    summary = {"date": stamp, "jobs": len(jobs), "new": len(notify.new_postings(jobs, new_keys)),
               "files": [f"s3://{bucket}/{k}" for k in keys.values()], "message_id": message_id}
    logger.info("Done: %s", json.dumps(summary))
    return summary
