import json

import boto3
import pytest
from moto import mock_aws

from job_search import lambda_handler, notify
from job_search.nodes import job_key
from job_search.output import write_outputs
from job_search.state import JobPosting

BUCKET = "job-search-test"

JOBS = [
    JobPosting(title="Senior Computational Biologist", company="Genentech", matches_criteria="yes",
               relevance_score=9, resume_fit=5, url="https://genentech.example/1", work_arrangement="remote"),
    JobPosting(title="Staff Bioinformatics Scientist", company="Freenome", matches_criteria="yes",
               relevance_score=8, resume_fit=9, fit_reason="cancer genomics overlap", salary="$200k"),
    JobPosting(title="Old Role", company="Illumina", matches_criteria="unclear", posting_status="closed",
               resume_fit=10),
    JobPosting(title="Cleared Analyst", company="DefenseBio", matches_criteria="no"),
]


@pytest.fixture
def aws(monkeypatch, tmp_path):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-1")
    monkeypatch.setenv("BUCKET", BUCKET)
    monkeypatch.setenv("GOOGLE_API_KEY", "g")
    monkeypatch.setenv("TAVILY_API_KEY", "t")
    monkeypatch.setattr(lambda_handler, "WORK_DIR", tmp_path / "work")
    with mock_aws():
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "us-west-1"})
        sns = boto3.client("sns")
        topic = sns.create_topic(Name="job-search-reports")["TopicArn"]
        monkeypatch.setenv("NOTIFY_TOPIC_ARN", topic)
        yield s3


@pytest.fixture
def fake_run(monkeypatch):
    """Replaces the real search: records the profile it got and writes the report for JOBS."""
    calls = []

    def run(profile, output_dir):
        calls.append(profile)
        paths = write_outputs(JOBS, profile, output_dir)
        return {"jobs": JOBS, "output_files": [str(p) for p in paths]}

    monkeypatch.setattr(lambda_handler, "run", run)
    return calls


def published(monkeypatch):
    sent = []
    real = notify.publish
    monkeypatch.setattr(notify, "publish", lambda *a, **kw: sent.append(a) or real(*a, **kw))
    return sent


def test_handler_uploads_report_emails_and_remembers_jobs(aws, fake_run, monkeypatch):
    aws.put_object(Bucket=BUCKET, Key="resume/cv.txt", Body=b"Bioinformatics PhD")
    sent = published(monkeypatch)

    out = lambda_handler.handler({"queries_per_round": 6})

    assert fake_run[0]["queries_per_round"] == 6
    resume_dir = lambda_handler.WORK_DIR / "resume"
    assert fake_run[0]["resume_dir"] == str(resume_dir) and (resume_dir / "cv.txt").read_text() == "Bioinformatics PhD"

    keys = {o["Key"] for o in aws.list_objects_v2(Bucket=BUCKET, Prefix="reports/")["Contents"]}
    assert {k.rsplit(".", 1)[1] for k in keys} == {"csv", "pdf"}
    assert all(k.startswith(f"reports/{out['date']}/") for k in keys)
    assert out["new"] == 2 and out["message_id"]
    assert len(sent) == 1

    seen = json.loads(aws.get_object(Bucket=BUCKET, Key=lambda_handler.SEEN_KEY)["Body"].read())
    assert set(seen) == {job_key(j) for j in JOBS}


def test_second_run_reports_nothing_new(aws, fake_run, monkeypatch):
    lambda_handler.handler({})
    sent = published(monkeypatch)
    out = lambda_handler.handler({})
    assert out["new"] == 0
    _, jobs, new_keys, *_ = sent[0]
    assert new_keys == set()
    assert "2 postings are from earlier runs" in notify.message(jobs, new_keys, BUCKET, {}, "2026-11-03")


def test_failed_email_does_not_mark_jobs_seen(aws, fake_run, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("SNS down")

    monkeypatch.setattr(notify, "publish", boom)
    with pytest.raises(RuntimeError, match="SNS down"):
        lambda_handler.handler({})
    assert lambda_handler.read_seen(aws, BUCKET) == {}


def test_unknown_event_key_is_rejected(aws, fake_run):
    with pytest.raises(ValueError, match="queries_per_rnd"):
        lambda_handler.handler({"queries_per_rnd": 6})
    assert not fake_run


def test_api_keys_come_from_ssm(aws, monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY")
    monkeypatch.setenv("GOOGLE_API_KEY_PARAM", "/test/google")
    ssm = boto3.client("ssm")
    ssm.put_parameter(Name="/test/google", Value="from-ssm", Type="SecureString")
    lambda_handler.load_api_keys(ssm)
    assert lambda_handler.os.environ["GOOGLE_API_KEY"] == "from-ssm"


def test_message_lists_new_open_matches_by_resume_fit():
    new_keys = {job_key(j) for j in JOBS}
    text = notify.message(JOBS, new_keys, BUCKET, {"pdf": "reports/d/report.pdf"}, "2026-11-03")
    assert text.index("Freenome") < text.index("Genentech")
    assert "cancer genomics overlap" in text and "$200k" in text
    assert "Old Role" not in text and "Cleared Analyst" not in text  # closed and non-matching
    assert f"PDF: s3://{BUCKET}/reports/d/report.pdf" in text
    assert notify.subject(JOBS, new_keys, "2026-11-03") == "job-search: 2 new postings (best fit 9/10), 2026-11-03"
    assert "No new postings" in notify.message(JOBS, set(), BUCKET, {}, "2026-11-03")
