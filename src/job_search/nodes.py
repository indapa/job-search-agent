import logging
import re
from datetime import date
from typing import Callable

from langchain_core.language_models import BaseChatModel
from langgraph.types import Send

from . import prompts
from .output import write_outputs
from .state import (
    Consolidation, ExtractionResult, GraphState, JobPosting, QueryPlan, Reflection, ResumeRanking, SearchTask,
)

log = logging.getLogger(__name__)

# (query, max_results) -> [{"url", "title", "content", "raw_content"}, ...]
SearchFn = Callable[[str, int], list[dict]]

MAX_PAGE_CHARS = 12_000
LLM_CONCURRENCY = 8
RANK_BATCH = 20

_STOPWORDS = {"the", "inc", "incorporated", "llc", "ltd", "corp", "corporation", "company", "companie", "co",
              "for", "of", "and"}


def _words(s: str) -> list[str]:
    # Drop apostrophes and a trailing "s" so "Moderna's", "Modernas" and "Moderna" all match
    words = re.sub(r"[^a-z0-9 ]", " ", s.lower().replace("'", "").replace("’", "")).split()
    words = [w[:-1] if len(w) > 3 and w.endswith("s") else w for w in words]
    return [w for w in words if w not in _STOPWORDS]


def job_key(j: JobPosting) -> str:
    # Company names vary a lot ("Genentech" vs "Genentech, Inc."), so only its first word counts
    company = _words(j.company)[:1]
    return f"{' '.join(company)}|{' '.join(_words(j.title))}"


_MATCH_RANK = {"unclear": 0, "no": 1, "yes": 2}
_STATUS_RANK = {"unknown": 0, "closed": 1, "open": 2}


def merge_jobs(a: JobPosting, b: JobPosting) -> JobPosting:
    """Combine two records of the same posting, keeping every stated field."""
    primary, secondary = (a, b) if a.relevance_score >= b.relevance_score else (b, a)
    merged = primary.model_dump()
    for field, value in secondary.model_dump().items():
        if merged.get(field) in (None, "", "unknown") and value not in (None, "", "unknown"):
            merged[field] = value
    merged["source_urls"] = list(dict.fromkeys(a.source_urls + b.source_urls))
    merged["relevance_score"] = max(a.relevance_score, b.relevance_score)
    # A definite yes/no beats "unclear"; if they disagree, prefer "yes" so a human checks it
    merged["matches_criteria"] = max(a.matches_criteria, b.matches_criteria, key=_MATCH_RANK.get)
    merged["posting_status"] = max(a.posting_status, b.posting_status, key=_STATUS_RANK.get)
    return JobPosting(**merged)


def dedupe(jobs: list[JobPosting]) -> list[JobPosting]:
    by_key: dict[str, JobPosting] = {}
    for j in jobs:
        k = job_key(j)
        by_key[k] = merge_jobs(by_key[k], j) if k in by_key else j
    return list(by_key.values())


def excluded(j: JobPosting, profile: dict) -> bool:
    """True if the posting mentions one of the profile's exclude_keywords."""
    text = " ".join(filter(None, (j.title, j.description, j.requirements)))
    return any(re.search(rf"\b{re.escape(term)}\b", text, re.IGNORECASE) for term in profile.get("exclude_keywords") or [])


def needs_verification(j: JobPosting) -> bool:
    return j.matches_criteria != "no" and (j.url is None or j.salary is None or j.work_arrangement == "unknown")


class Nodes:
    def __init__(self, llm: BaseChatModel, search: SearchFn, output_dir: str = "output"):
        self.llm = llm
        self.search = search
        self.output_dir = output_dir
        self.extractor = llm.with_structured_output(ExtractionResult)

    # --- graph nodes -------------------------------------------------------

    def plan_queries(self, state: GraphState) -> dict:
        profile = state["profile"]
        previous = state.get("queries", [])
        gaps = state.get("gaps", [])
        prompt = prompts.PLAN_QUERIES.format(
            profile=prompts.profile_summary(profile),
            n=profile["queries_per_round"],
            gap_section=prompts.GAP_SECTION.format(gaps="\n".join(f"- {g}" for g in gaps)) if gaps else "",
            previous="\n".join(f"- {q}" for q in previous) or "(none)",
        )
        plan = self.llm.with_structured_output(QueryPlan).invoke(prompt)
        new = [q for q in plan.queries if q not in previous][: profile["queries_per_round"]]
        log.info("Round %d: %d queries", state.get("iteration", 0) + 1, len(new))
        return {"queries": previous + new, "pending_queries": new}

    def fan_out_search(self, state: GraphState) -> list[Send] | str:
        n = state["profile"]["results_per_query"]
        pending = state.get("pending_queries", [])
        if not pending:
            return "extract"
        return [Send("search", SearchTask(query=q, max_results=n)) for q in pending]

    def run_search(self, task: SearchTask) -> dict:
        try:
            results = self.search(task["query"], task["max_results"])
        except Exception as e:  # one failed query shouldn't sink the run
            log.warning("Search failed for %r: %s", task["query"], e)
            results = []
        return {"search_results": results}

    def extract(self, state: GraphState) -> dict:
        seen = set(state.get("seen_urls", []))
        pages = []
        for r in state.get("search_results", []):
            if r.get("url") and r["url"] not in seen:
                seen.add(r["url"])
                pages.append(r)
        log.info("Extracting from %d new pages", len(pages))
        return {"candidates": self._extract_pages(pages, state["profile"]), "seen_urls": list(seen)}

    def dedupe(self, state: GraphState) -> dict:
        profile = state["profile"]
        jobs = [
            # The LLM sometimes misses an excluded term, so check it here too
            j.model_copy(update={"matches_criteria": "no"}) if excluded(j, profile) else j
            for j in dedupe(state.get("jobs", []) + state.get("candidates", []))
        ]
        log.info("%d unique jobs", len(jobs))
        return {"jobs": jobs, "candidates": []}

    def verify(self, state: GraphState) -> dict:
        profile = state["profile"]
        verified = set(state.get("verified", []))
        to_check = sorted(
            (j for j in state["jobs"] if needs_verification(j) and job_key(j) not in verified),
            key=lambda j: j.relevance_score,
            reverse=True,
        )[: profile["verify_top_n"]]
        log.info("Verifying %d jobs", len(to_check))

        seen = set(state.get("seen_urls", []))
        pages = []
        for j in to_check:
            verified.add(job_key(j))
            try:
                results = self.search(f"{j.company} {j.title} job posting", 2)
            except Exception as e:
                log.warning("Verify search failed for %s: %s", j.title, e)
                continue
            for r in results:
                if r.get("url") and r["url"] not in seen:
                    seen.add(r["url"])
                    pages.append(r)

        # Only keep extracted records that match a job we already have
        by_key = {job_key(j): j for j in state["jobs"]}
        for found in self._extract_pages(pages, profile):
            k = job_key(found)
            if k in by_key:
                by_key[k] = merge_jobs(by_key[k], found)
        return {"jobs": list(by_key.values()), "verified": list(verified), "seen_urls": list(seen)}

    def reflect(self, state: GraphState) -> dict:
        profile = state["profile"]
        iteration = state.get("iteration", 0) + 1
        if iteration >= profile["max_iterations"]:
            return {"iteration": iteration, "gaps": []}
        jobs = state.get("jobs", [])
        listing = "\n".join(f"- {j.company}: {j.title} ({j.work_arrangement}, {j.employment_type})" for j in jobs)
        reflection = self.llm.with_structured_output(Reflection).invoke(
            prompts.REFLECT.format(
                profile=prompts.profile_summary(profile),
                count=len(jobs),
                jobs=listing or "(none)",
            )
        )
        log.info("Gaps: %s", reflection.gaps or "none")
        return {"iteration": iteration, "gaps": reflection.gaps}

    def route_after_reflect(self, state: GraphState) -> str:
        if state.get("gaps") and state["iteration"] < state["profile"]["max_iterations"]:
            return "plan_queries"
        return "consolidate"

    def consolidate(self, state: GraphState) -> dict:
        """Merge duplicates that title matching missed and label each posting open/closed as of today."""
        jobs = state.get("jobs", [])
        if not jobs:
            return {}
        listing = "\n".join(
            f"{i} | {j.company} | {j.title} | {j.url or ''} | {j.location or ''} | "
            f"{j.posted_date or ''} | {j.application_deadline or ''}"
            for i, j in enumerate(jobs)
        )
        try:
            result = self.llm.with_structured_output(Consolidation).invoke(
                prompts.CONSOLIDATE.format(today=date.today().isoformat(), jobs=listing)
            )
        except Exception as e:  # the report is still useful without this step
            log.warning("Consolidation failed, keeping jobs as-is: %s", e)
            return {}

        statuses = {r.id: r.posting_status for r in result.reviews}
        labelled = [j.model_copy(update={"posting_status": statuses.get(i, "unknown")}) for i, j in enumerate(jobs)]

        used: set[int] = set()
        merged = []
        for group in result.duplicate_groups:
            ids = [i for i in dict.fromkeys(group) if 0 <= i < len(jobs) and i not in used]
            if len(ids) < 2:
                continue
            used.update(ids)
            combined = labelled[ids[0]]
            for i in ids[1:]:
                combined = merge_jobs(combined, labelled[i])
            merged.append(combined)
        merged += [j for i, j in enumerate(labelled) if i not in used]
        log.info("Consolidated %d jobs into %d", len(jobs), len(merged))
        return {"jobs": merged}

    def rank(self, state: GraphState) -> dict:
        """Score each posting against the person's resume, so the report lists the best fits first."""
        resume = state["profile"].get("resume_text")
        jobs = state.get("jobs", [])
        to_rank = [i for i, j in enumerate(jobs) if j.matches_criteria != "no"]
        if not resume or not to_rank:
            return {}
        chunks = [to_rank[k : k + RANK_BATCH] for k in range(0, len(to_rank), RANK_BATCH)]
        inputs = [
            prompts.RANK.format(
                resume=resume,
                jobs="\n".join(
                    f"{n} | {jobs[i].title} | {jobs[i].company} | {jobs[i].seniority or ''} | "
                    f"{jobs[i].requirements or ''} | {jobs[i].description}"
                    for n, i in enumerate(chunk)
                ),
            )
            for chunk in chunks
        ]
        results = self.llm.with_structured_output(ResumeRanking).batch(
            inputs, config={"max_concurrency": LLM_CONCURRENCY}, return_exceptions=True
        )

        ranked = list(jobs)
        for chunk, result in zip(chunks, results):
            if isinstance(result, Exception) or result is None:  # those jobs keep their keyword ranking
                log.warning("Resume ranking failed for %d jobs: %s", len(chunk), result)
                continue
            for fit in result.fits:
                if 0 <= fit.id < len(chunk):
                    i = chunk[fit.id]
                    ranked[i] = jobs[i].model_copy(update={"resume_fit": fit.score, "fit_reason": fit.reason})
        log.info("Ranked %d jobs against the resume", sum(j.resume_fit is not None for j in ranked))
        return {"jobs": ranked}

    def write_report(self, state: GraphState) -> dict:
        paths = write_outputs(state.get("jobs", []), state["profile"], self.output_dir)
        return {"output_files": [str(p) for p in paths]}

    # --- helpers -----------------------------------------------------------

    def _extract_pages(self, pages: list[dict], profile: dict) -> list[JobPosting]:
        if not pages:
            return []
        summary = prompts.profile_summary(profile)
        inputs = [
            prompts.EXTRACT.format(
                profile=summary,
                url=page["url"],
                title=page.get("title", ""),
                content=(page.get("raw_content") or page.get("content") or "")[:MAX_PAGE_CHARS],
            )
            for page in pages
        ]
        results = self.extractor.batch(inputs, config={"max_concurrency": LLM_CONCURRENCY}, return_exceptions=True)
        jobs = []
        for page, result in zip(pages, results):
            if isinstance(result, Exception) or result is None:
                log.warning("Extraction failed for %s: %s", page["url"], result)
                continue
            for j in result.jobs:
                sources = list(dict.fromkeys(j.source_urls + [page["url"]]))
                jobs.append(JobPosting(**j.model_dump(exclude={"source_urls"}), source_urls=sources))
        return jobs
