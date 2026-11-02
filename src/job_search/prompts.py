def _list(profile: dict, key: str) -> str:
    return ", ".join(profile.get(key) or []) or "any"


def profile_summary(profile: dict) -> str:
    return (
        f"Job titles/keywords: {_list(profile, 'keywords')}. "
        f"Employment type: {_list(profile, 'employment_type')}. "
        f"Work arrangement: {_list(profile, 'work_arrangement')}. "
        f"Seniority: {_list(profile, 'seniority')}. "
        f"Focus areas: {_list(profile, 'focus_areas')}. "
        f"Exclude postings mentioning: {', '.join(profile.get('exclude_keywords') or []) or 'nothing'}."
    )


PLAN_QUERIES = """You are helping a job seeker find current job postings.

Search criteria: {profile}

Write {n} distinct web search queries that will surface individual job postings matching these criteria.
Cover a mix of:
- the keywords combined with the focus areas and seniority levels
- applicant tracking systems that host postings directly (e.g. site:boards.greenhouse.io, site:jobs.lever.co, site:jobs.ashbyhq.com)
- general job boards (LinkedIn, Indeed, Wellfound) and niche boards for the focus areas
- career pages of companies and organizations known to hire for these roles
- the requested work arrangements and employment types (e.g. "remote", "contract")
Prefer queries that find individual postings rather than articles, salary guides or "top jobs" lists.
{gap_section}
Do not repeat any of these earlier queries:
{previous}
"""

GAP_SECTION = """
Earlier rounds missed these areas. Focus most queries on them:
{gaps}
"""

EXTRACT = """Extract every concrete job posting described on this web page.

Search criteria: {profile}

Rules:
- Use only information on the page. If a field (salary, location, posted date, arrangement, type) is not stated, leave it null or "unknown". Never guess.
- A page that lists several postings: extract each one that has a title and a company.
- Skip articles, salary guides, resume advice and "best jobs" lists that don't describe a specific opening.
- matches_criteria: "no" if the posting mentions an excluded term, or clearly has an employment type, work arrangement or seniority outside the criteria; "yes" if it clearly fits the keywords and criteria; otherwise "unclear".
- relevance_score: 10 = clearly fits the keywords, focus areas and seniority; 1 = barely relevant.
- Return an empty list if the page describes no concrete posting.

Page URL: {url}
Page title: {title}
Page content:
{content}
"""

REFLECT = """You are reviewing coverage of a job search with these criteria:
{profile}

Postings found so far ({count}):
{jobs}

List up to 5 specific gaps: kinds of postings or sources that probably exist but are missing.
Think about job boards and applicant tracking systems, employer types (startups, large tech, pharma and biotech,
academia and research institutes, nonprofits, government contractors), the focus areas, and adjacent job titles.
Return an empty list if coverage is already good.
"""

CONSOLIDATE = """Below are job postings gathered from many web pages. Today is {today}.

Each line: id | company | job title | url | location | posted date | application deadline

{jobs}

1. duplicate_groups: group the ids of records that describe the SAME opening, even when the company or title
   is written differently (e.g. the same role on LinkedIn and on the company's careers page). Different roles at
   the same company, or the same title in different locations, are NOT duplicates. When unsure, don't group.
2. reviews: for every id, set posting_status:
   - "closed": the deadline is before today, the page says the posting is closed / no longer accepting
     applications, or it was posted more than 60 days before today
   - "open": posted within the last 60 days, or the deadline is today or later
   - "unknown": no posted date or deadline, or it can't be interpreted
"""

RANK = """Score how well this person's resume fits each job posting below.

Resume:
{resume}

Each line: id | job title | company | seniority | requirements | description

{jobs}

For every id, return:
- score: 10 = the person's skills, domain and seniority strongly match what the posting asks for; 1 = little overlap.
- reason: one short sentence naming the concrete overlap or gap (e.g. "strong NGS pipeline experience; no Kubernetes").
  Write it without names or pronouns.
Judge only from what the posting states. If it says little, score on the title and description and say so.
"""
