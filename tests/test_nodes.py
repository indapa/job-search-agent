import csv

from langchain_core.runnables import RunnableLambda

from job_search.graph import build_graph
from job_search.nodes import Nodes, dedupe, excluded, job_key, merge_jobs
from job_search.output import read_csv, report_sections, sort_jobs, write_outputs
from job_search.resume import load_resumes
from job_search.state import (
    Consolidation, ExtractionResult, JobPosting, JobReview, QueryPlan, Reflection, ResumeFit, ResumeRanking,
)

PROFILE = {
    "keywords": ["computational biologist"],
    "employment_type": ["full-time"],
    "work_arrangement": ["remote"],
    "focus_areas": ["genomics"],
    "seniority": ["senior"],
    "exclude_keywords": ["security clearance"],
    "max_iterations": 2,
    "queries_per_round": 2,
    "results_per_query": 3,
    "verify_top_n": 5,
    "model": "fake",
}

PAGES = {
    "https://boards.example/genentech": JobPosting(
        title="Senior Computational Biologist", company="Genentech", work_arrangement="remote",
        employment_type="full-time", matches_criteria="yes", relevance_score=9,
    ),
    "https://boards.example/illumina": JobPosting(
        title="Bioinformatics Scientist", company="Illumina", salary="$150k-$180k", url="https://illumina.example/1",
        work_arrangement="hybrid", matches_criteria="unclear", relevance_score=7,
    ),
    "https://boards.example/defense": JobPosting(
        title="Senior Computational Biologist", company="DefenseBio",
        requirements="Active Security Clearance required", matches_criteria="yes", relevance_score=8,
        salary="$170k", url="https://defense.example/1", work_arrangement="remote",
    ),
    "https://genentech.example/apply": JobPosting(
        title="Senior Computational Biologists", company="Genentech, Inc.",
        url="https://genentech.example/apply", salary="$190k-$230k",
    ),
}


class FakeLLM:
    """Duck-types the parts of a chat model the nodes use: with_structured_output(...).invoke/batch."""

    def __init__(self, gaps_first_round=("academic research institutes",), consolidation=None, resume_scores=None):
        self.gaps = list(gaps_first_round)
        self.reflect_calls = 0
        self.consolidation = consolidation or Consolidation()
        self.consolidate_prompt = None
        self.resume_scores = resume_scores or {}  # job title -> score
        self.rank_prompts = []

    def with_structured_output(self, schema):
        def respond(prompt: str):
            if schema is QueryPlan:
                return QueryPlan(queries=[f"q{len(prompt) % 97}-a", f"q{len(prompt) % 89}-b"])
            if schema is Reflection:
                self.reflect_calls += 1
                return Reflection(gaps=self.gaps)
            if schema is Consolidation:
                self.consolidate_prompt = prompt
                return self.consolidation
            if schema is ExtractionResult:
                url = next(u for u in PAGES if f"Page URL: {u}" in prompt)
                return ExtractionResult(jobs=[PAGES[url].model_copy(deep=True)])
            if schema is ResumeRanking:
                self.rank_prompts.append(prompt)
                lines = [line.split(" | ") for line in prompt.splitlines() if line[:1].isdigit() and " | " in line]
                return ResumeRanking(fits=[
                    ResumeFit(id=int(i), score=self.resume_scores[title], reason=f"fit for {title}")
                    for i, title, *_ in lines
                ])
            raise AssertionError(schema)

        return RunnableLambda(respond)


def fake_search(query: str, max_results: int) -> list[dict]:
    if query.endswith("job posting"):
        return [{"url": "https://genentech.example/apply", "title": "Apply", "content": "..."}]
    return [
        {"url": "https://boards.example/genentech", "title": "Genentech", "content": "..."},
        {"url": "https://boards.example/illumina", "title": "Illumina", "content": "..."},
        {"url": "https://boards.example/defense", "title": "DefenseBio", "content": "..."},
    ][:max_results]


def test_job_key_ignores_punctuation_and_corporate_suffixes():
    a = PAGES["https://boards.example/genentech"]
    b = PAGES["https://genentech.example/apply"]
    assert job_key(a) == job_key(b)


def test_merge_fills_missing_fields_and_unions_sources():
    a = PAGES["https://boards.example/genentech"].model_copy(update={"source_urls": ["u1"]})
    b = PAGES["https://genentech.example/apply"].model_copy(update={"source_urls": ["u2"]})
    m = merge_jobs(a, b)
    assert m.company == "Genentech"  # higher relevance record wins
    assert m.salary == "$190k-$230k"
    assert m.url == "https://genentech.example/apply"
    assert m.source_urls == ["u1", "u2"]
    assert m.matches_criteria == "yes"
    assert m.work_arrangement == "remote"


def test_merge_fills_unknown_enums_and_keeps_open_status():
    a = JobPosting(title="X", company="Y", relevance_score=9, posting_status="closed")
    b = JobPosting(title="X", company="Y", employment_type="contract", posting_status="open")
    m = merge_jobs(a, b)
    assert (m.employment_type, m.posting_status) == ("contract", "open")


def test_dedupe_merges_same_job():
    assert len(dedupe(list(PAGES.values()))) == 3


def test_excluded_matches_whole_terms_case_insensitively():
    assert excluded(PAGES["https://boards.example/defense"], PROFILE)
    assert not excluded(PAGES["https://boards.example/genentech"], PROFILE)
    assert not excluded(JobPosting(title="International Genomics Lead", company="x"), {"exclude_keywords": ["intern"]})
    assert not excluded(PAGES["https://boards.example/defense"], {})


def test_full_graph_loops_once_on_gaps_then_writes_report(tmp_path):
    llm = FakeLLM()
    graph = build_graph(llm, fake_search, str(tmp_path))
    result = graph.invoke({"profile": PROFILE, "iteration": 0})

    assert result["iteration"] == 2
    assert llm.reflect_calls == 1  # last round skips the LLM call
    assert len(result["queries"]) == 4

    jobs = {j.company: j for j in result["jobs"]}
    assert len(jobs) == 3
    genentech = jobs["Genentech"]
    assert genentech.salary == "$190k-$230k"  # filled in by verify
    assert "https://genentech.example/apply" in genentech.source_urls
    assert jobs["DefenseBio"].matches_criteria == "no"  # exclude_keywords overrides the LLM

    csv_path, pdf_path = map(lambda p: tmp_path / p.split("/")[-1], result["output_files"])
    rows = list(csv.DictReader(csv_path.open()))
    assert len(rows) == 3 and all(r["source_urls"] for r in rows)
    assert pdf_path.suffix == ".pdf" and pdf_path.read_bytes().startswith(b"%PDF")
    _, sections = report_sections(result["jobs"])
    by_heading = {h: [j.company for j in items] for h, _, items in sections}
    assert by_heading == {"Strong matches": ["Genentech"], "Worth a look (fit unclear)": ["Illumina"]}


def test_no_gaps_stops_after_first_round(tmp_path):
    llm = FakeLLM(gaps_first_round=())
    result = build_graph(llm, fake_search, str(tmp_path)).invoke({"profile": PROFILE, "iteration": 0})
    assert result["iteration"] == 1
    assert len(result["queries"]) == 2


SAME_ROLE = [
    JobPosting(title="Sr. Computational Biologist, Genomics", company="Genentech", relevance_score=9,
               source_urls=["a"]),
    JobPosting(title="Senior Computational Biologist - Genomics", company="Roche / Genentech",
               posted_date="2026-10-20", relevance_score=8, source_urls=["b"]),
    JobPosting(title="Bioinformatics Scientist", company="Illumina", posted_date="2026-03-01",
               relevance_score=7, source_urls=["c"]),
]


def test_consolidate_merges_llm_groups_and_labels_status():
    llm = FakeLLM(consolidation=Consolidation(
        duplicate_groups=[[0, 1], [2, 99]],  # 99 is out of range; a group of 1 valid id is ignored
        reviews=[JobReview(id=0, posting_status="unknown"), JobReview(id=1, posting_status="open"),
                 JobReview(id=2, posting_status="closed")],
    ))
    out = Nodes(llm, fake_search).consolidate({"jobs": SAME_ROLE})["jobs"]

    assert len(out) == 2
    merged = next(j for j in out if j.source_urls[0] in ("a", "b"))
    assert sorted(merged.source_urls) == ["a", "b"]
    assert merged.posting_status == "open"
    assert merged.posted_date == "2026-10-20"
    assert next(j for j in out if j.company == "Illumina").posting_status == "closed"
    assert "1 | Roche / Genentech |" in llm.consolidate_prompt


def test_consolidate_failure_keeps_jobs():
    class Broken(FakeLLM):
        def with_structured_output(self, schema):
            return RunnableLambda(lambda _: (_ for _ in ()).throw(RuntimeError("boom")))

    assert Nodes(Broken(), fake_search).consolidate({"jobs": SAME_ROLE}) == {}


def test_report_puts_closed_postings_in_their_own_section():
    jobs = [
        JobPosting(title="Open One", company="A", matches_criteria="yes", posting_status="open"),
        JobPosting(title="Old One", company="B", matches_criteria="unclear", posting_status="closed"),
        JobPosting(title="Wrong One", company="C", matches_criteria="no"),
    ]
    summary, sections = report_sections(jobs)
    by_heading = {h: [j.title for j in items] for h, _, items in sections}
    assert by_heading == {"Strong matches": ["Open One"], "Possibly closed": ["Old One"]}
    assert summary.startswith("Found 3 postings")


def test_csv_round_trip_rebuilds_pdf(tmp_path):
    jobs = [j.model_copy(update={"source_urls": ["https://x.example/a"], "work_arrangement": "remote"})
            for j in SAME_ROLE]
    csv_path, _ = write_outputs(jobs, PROFILE, str(tmp_path))
    loaded = read_csv(csv_path)
    assert {j.title for j in loaded} == {j.title for j in jobs}
    assert all(j.work_arrangement == "remote" and j.source_urls == ["https://x.example/a"] for j in loaded)

    (pdf_path,) = write_outputs(loaded, PROFILE, str(tmp_path / "rebuilt"), include_csv=False)
    assert pdf_path.read_bytes().startswith(b"%PDF")


RANKABLE = [
    JobPosting(title="Senior Computational Biologist", company="Genentech", matches_criteria="yes",
               relevance_score=9),
    JobPosting(title="Bioinformatics Scientist", company="Illumina", matches_criteria="yes", relevance_score=7),
    JobPosting(title="Cleared Analyst", company="DefenseBio", matches_criteria="no", relevance_score=8),
]


def test_rank_orders_by_resume_fit_over_keyword_relevance():
    llm = FakeLLM(resume_scores={"Senior Computational Biologist": 4, "Bioinformatics Scientist": 9})
    profile = {**PROFILE, "resume_text": "Bioinformatics PhD, NGS pipelines"}
    out = Nodes(llm, fake_search).rank({"profile": profile, "jobs": RANKABLE})["jobs"]

    assert [j.company for j in sort_jobs(out)] == ["Illumina", "Genentech", "DefenseBio"]
    illumina = next(j for j in out if j.company == "Illumina")
    assert (illumina.resume_fit, illumina.fit_reason) == (9, "fit for Bioinformatics Scientist")
    assert next(j for j in out if j.company == "DefenseBio").resume_fit is None  # non-matches aren't scored
    assert len(llm.rank_prompts) == 1 and "NGS pipelines" in llm.rank_prompts[0]
    assert "DefenseBio" not in llm.rank_prompts[0]


def test_rank_without_resume_keeps_keyword_order():
    llm = FakeLLM()
    assert Nodes(llm, fake_search).rank({"profile": PROFILE, "jobs": RANKABLE}) == {}
    assert not llm.rank_prompts
    assert [j.company for j in sort_jobs(RANKABLE)] == ["Genentech", "Illumina", "DefenseBio"]


def test_rank_failure_leaves_jobs_unscored():
    class Broken(FakeLLM):
        def with_structured_output(self, schema):
            return RunnableLambda(lambda _: (_ for _ in ()).throw(RuntimeError("boom")))

    out = Nodes(Broken(), fake_search).rank({"profile": {**PROFILE, "resume_text": "cv"}, "jobs": RANKABLE})["jobs"]
    assert all(j.resume_fit is None for j in out)


def test_resume_fit_survives_csv_round_trip(tmp_path):
    jobs = [RANKABLE[0].model_copy(update={"resume_fit": 8, "fit_reason": "strong genomics overlap"})]
    csv_path, pdf_path = write_outputs(jobs, PROFILE, str(tmp_path))
    (loaded,) = read_csv(csv_path)
    assert (loaded.resume_fit, loaded.fit_reason) == (8, "strong genomics overlap")
    assert pdf_path.read_bytes().startswith(b"%PDF")


def test_load_resumes_reads_text_files_and_tolerates_missing_folder(tmp_path):
    (tmp_path / "cv.md").write_text("Jane Doe\n\n  Bioinformatics   scientist")
    (tmp_path / "photo.jpg").write_bytes(b"not a resume")
    assert load_resumes(str(tmp_path)) == ("=== cv.md ===\nJane Doe Bioinformatics scientist", 1)
    assert load_resumes(str(tmp_path / "missing")) == ("", 0)
