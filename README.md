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
                                                                         └→ consolidate → write_report
```

- **plan_queries**: Gemini writes search queries from your criteria, mixing job boards, applicant tracking systems (Greenhouse, Lever, Ashby) and company career pages. Later rounds target gaps that `reflect` found.
- **search**: Tavily searches run in parallel and return the full page text.
- **extract**: Gemini pulls structured posting details from each page. It is told to leave fields empty rather than guess.
- **dedupe**: merges the same posting found on different pages, and marks postings that mention an `exclude_keywords` term as not a match.
- **verify**: for the best matches that are missing a URL, salary or work arrangement, it runs a follow-up search and fills in the gaps.
- **reflect**: asks what kinds of postings or sources are still missing and loops until `max_iterations`.
- **consolidate**: merges duplicates that name matching missed and labels each posting open, closed or unknown.

Always check that a posting is still open on the company's own careers page before applying.

## Tests

```bash
uv run pytest
```

The tests use a fake LLM and canned search results, so they make no API calls.

## Contributing

1. Fork the repository and clone your fork.
2. Create a branch, add your feature, and make sure `uv run pytest` passes.
3. Push the branch to your fork and open a pull request against `main`.
