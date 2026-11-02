import argparse
import logging
import os
import re
import sys
from datetime import date
from pathlib import Path

import yaml
from dotenv import load_dotenv

from .graph import build_graph
from .output import read_csv, write_outputs
from .resume import load_resumes

DEFAULTS = {
    "keywords": [],
    "employment_type": [],
    "work_arrangement": [],
    "focus_areas": [],
    "seniority": [],
    "exclude_keywords": [],
    "resume_dir": "resume",
    "max_iterations": 2,
    "queries_per_round": 12,
    "results_per_query": 5,
    "verify_top_n": 15,
    "model": "gemini-3.8-flash",
}


def tavily_search(client):
    def search(query: str, max_results: int) -> list[dict]:
        resp = client.search(
            query,
            search_depth="advanced",
            max_results=max_results,
            include_raw_content=True,
        )
        return resp.get("results", [])

    return search


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Find job postings that match your criteria.")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--output-dir", default="output")
    parser.add_argument("--max-iterations", type=int, help="override config max_iterations")
    parser.add_argument("--queries", type=int, help="override config queries_per_round")
    parser.add_argument("--from-csv", metavar="CSV", help="rebuild the PDF report from an earlier run's CSV, without searching")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("httpx", "httpcore", "google_genai", "fontTools"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    with open(args.config) as f:
        profile = {**DEFAULTS, **yaml.safe_load(f)}

    if args.from_csv:
        csv_path = Path(args.from_csv)
        match = re.search(r"\d{4}-\d{2}-\d{2}", csv_path.stem)
        stamp = date.fromisoformat(match.group()) if match else date.today()
        paths = write_outputs(read_csv(csv_path), profile, args.output_dir, stamp, include_csv=False)
        print(f"Wrote {paths[0]}")
        return

    load_dotenv()
    missing = [k for k in ("GOOGLE_API_KEY", "TAVILY_API_KEY") if not os.getenv(k)]
    if missing:
        sys.exit(f"Missing {', '.join(missing)}. Copy .env.example to .env and fill it in.")

    if args.max_iterations:
        profile["max_iterations"] = args.max_iterations
    if args.queries:
        profile["queries_per_round"] = args.queries

    profile["resume_text"], n_resumes = load_resumes(profile["resume_dir"]) if profile["resume_dir"] else ("", 0)
    if n_resumes:
        logging.info("Ranking against %d resume(s) in %s", n_resumes, profile["resume_dir"])
    else:
        logging.info("No resume found in %r; ranking by keyword relevance only", profile["resume_dir"])

    # Imported here so --help and config errors don't pay the import cost
    from langchain_google_genai import ChatGoogleGenerativeAI
    from tavily import TavilyClient

    # The client has no timeout by default, so one stalled request can hang the whole run
    llm = ChatGoogleGenerativeAI(model=profile["model"], temperature=0, timeout=120, max_retries=3)
    search = tavily_search(TavilyClient(api_key=os.environ["TAVILY_API_KEY"]))
    graph = build_graph(llm, search, args.output_dir)

    result = graph.invoke({"profile": profile, "iteration": 0}, {"recursion_limit": 100})
    print(f"\nFound {len(result.get('jobs', []))} jobs.")
    for path in result.get("output_files", []):
        print(f"  {path}")


if __name__ == "__main__":
    main()
