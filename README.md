# Job Search Agent

A LangGraph agent that searches the web for job postings matching the keywords, employment types, work arrangements, seniority and focus areas in `config.yaml`. It writes:

- `output/jobs_YYYY-MM-DD.csv`: every posting found, with empty `status` and `notes` columns for tracking applications
- `output/report_YYYY-MM-DD.pdf`: a printable report with clickable links, grouped into strong matches, unclear fit, and possibly closed postings

## Setup

```bash
uv sync --extra dev    # creates .venv with Python 3.12 and installs everything
cp .env.example .env   # add your GOOGLE_API_KEY (Gemini) and TAVILY_API_KEY
```

### API keys

- **`GOOGLE_API_KEY`** (Gemini): sign in at [Google AI Studio](https://aistudio.google.com/app/apikey) with a Google account and click **Create API key**. The free tier is enough for test runs; full runs may hit its rate limits.
- **`TAVILY_API_KEY`** (web search): sign up at [tavily.com](https://app.tavily.com) and copy the key from your dashboard (it starts with `tvly-`). The free plan includes a monthly allowance of searches; a full run uses about 40.

Paste both into `.env`.

Edit `config.yaml` with your keywords, employment types, work arrangements, seniority, focus areas and any terms to exclude.

Put your resume (PDF, TXT or Markdown) in `resume/`. Results are then ranked by how well each posting fits it, and the report shows the reason for each score. You can add more than one resume; they're scored together. The folder is git-ignored. Without a resume, results are ranked by keyword relevance.

## Run

```bash
# small test run (~3 searches + verification)
uv run job-search --max-iterations 1 --queries 3

# full run (~40 Tavily searches with the default config)
uv run job-search

# rebuild the PDF from an earlier run's CSV, with no searching (e.g. after editing the CSV)
uv run job-search --from-csv output/jobs_2026-11-01.csv
```

## How it works

```
plan_queries → search (parallel) → extract → dedupe → verify → reflect ─┬→ plan_queries (if gaps remain)
                                                                         └→ consolidate → rank → write_report
```

- **plan_queries**: Gemini writes search queries from your criteria, mixing job boards, applicant tracking systems (Greenhouse, Lever, Ashby) and company career pages. Later rounds target gaps that `reflect` found.
- **search**: Tavily searches run in parallel and return the full page text.
- **extract**: Gemini pulls structured posting details from each page. It is told to leave fields empty rather than guess.
- **dedupe**: merges the same posting found on different pages, and marks postings that mention an `exclude_keywords` term as not a match.
- **verify**: for the best matches that are missing a URL, salary or work arrangement, it runs a follow-up search and fills in the gaps.
- **reflect**: asks what kinds of postings or sources are still missing and loops until `max_iterations`.
- **consolidate**: merges duplicates that name matching missed and labels each posting open, closed or unknown.
- **rank**: scores each posting 1–10 against your resume with a one-line reason (`resume_fit`, `fit_reason` in the CSV). Each report section is sorted by this score.

Always check that a posting is still open on the company's own careers page before applying.

## Running nightly on AWS

EventBridge Scheduler invokes a Lambda function (`job-search-nightly`) every day at 22:00 Pacific. Each night it:

1. downloads your resume from `s3://job-search-<account-id>/resume/`
2. runs the search with `config.yaml`, built into the image, using a smaller run by default (1 round, 6 queries, top 8 verified, about 15 Tavily searches)
3. writes the CSV and PDF to `s3://job-search-<account-id>/reports/<date>/`
4. emails you a summary

The email lists postings not seen in earlier runs, best resume fit first, with links to the report. `state/seen_jobs.json` in the bucket tracks which postings were already emailed. A night with nothing new still sends an email, so you know the job ran.

The infrastructure is two CloudFormation templates in [infra/](infra/), modeled on [omics-sra](https://github.com/embedbio/omics-sra):

- **`infra/bootstrap.yaml`:** the ECR image repository, plus an IAM role that GitHub Actions on `main` can assume (through OIDC) to push images and update the function. It can do nothing else.
- **`infra/app.yaml`:** contains:
  - the S3 bucket: private, versioned, encrypted, and kept if the stack is deleted
  - the Lambda function: arm64, 1 GB, 15-minute timeout, at most one copy running at a time
  - the nightly schedule
  - the `job-search-reports` email topic
  - a failure alarm that emails `job-search-alerts`

The API keys are stored as SSM SecureStrings under `/job-search/`. They aren't in the image or the template.

**Deploying** (needs the AWS CLI, and the `gh` CLI for step 2):

```bash
infra/deploy.sh bootstrap                                     # 1. once
gh variable set AWS_DEPLOY_ROLE_ARN --body <DeployRoleArn>    # 2. once, from the bootstrap output
infra/deploy.sh secrets                                       # 3. copies both API keys from .env to SSM
# 4. merge to main: the deploy workflow builds the image and pushes it to ECR
infra/deploy.sh app "" you@example.com                        # 5. bucket, function, schedule, emails, alarm
infra/deploy.sh resume                                        # 6. uploads resume/ to the bucket
```

AWS sends a confirmation email for each topic subscription. Nothing is delivered until you click the link. After the first deploy, every merge to `main` builds a new image and updates the function. Rerun `infra/deploy.sh resume` after you change your resume, and `infra/deploy.sh secrets` after you rotate a key.

**Running on demand, and checking results:**

```bash
aws lambda invoke --function-name job-search-nightly --cli-read-timeout 900 out.json && cat out.json
aws lambda invoke --function-name job-search-nightly --cli-read-timeout 900 --cli-binary-format raw-in-base64-out \
    --payload '{"queries_per_round": 3}' out.json            # override any config.yaml key
aws s3 ls --recursive s3://job-search-<account-id>/reports/
aws logs tail /aws/lambda/job-search-nightly --since 1d
```

An invocation with no payload uses the full `config.yaml` settings. Only the schedule passes the smaller run. To change the nightly run size, redeploy with a different `ScheduleInput`. For example, `'{}'` gives the full run, which takes about 50 searches a night and needs a paid Tavily plan. To change the time, redeploy with a different `ScheduleExpression`, or edit the `job-search-nightly` schedule in the EventBridge Scheduler console.

The schedule uses `America/Los_Angeles`, so the run is at 22:00 local time all year (PDT in summer). The image sets the same time zone, so report dates match the local date of the run.

## Tests

```bash
uv run pytest
```

The tests use a fake LLM, canned search results and [moto](https://github.com/getmoto/moto) for AWS, so they make no API calls.

## Contributing

1. Fork the repository and clone your fork.
2. Create a branch, add your feature, and make sure `uv run pytest` passes.
3. Push the branch to your fork and open a pull request against `main`.
