# =============================================================================
# FILE: api.py
# PURPOSE: FastAPI wrapper around the ADO Test Plan Generator
#
# ENDPOINTS:
#   POST /api/stories/run          → Run story mode (single or batch)
#   POST /api/bugs/run             → Run bug mode (single or batch)
#   GET  /api/jobs/{job_id}        → Poll job status + progress
#   GET  /api/jobs/{job_id}/logs   → Stream logs via Server-Sent Events
#   GET  /api/results              → All completed results
#   GET  /api/results/{job_id}     → Results for a specific job
#   POST /api/config/validate      → Validate .env / ADO connection
#   GET  /api/tools                → List available ADO MCP tools
#   POST /api/upload/video         → Upload video for bug mode
#   GET  /health                   → Health check
#
# RUN:
#   uvicorn api:app --reload --port 8000
# =============================================================================

import os, sys, re, json, csv, uuid, base64, logging, asyncio, threading, time, shutil, sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, BackgroundTasks, HTTPException, UploadFile, File, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

try:
    import PyPDF2
except ImportError:
    PyPDF2 = None

try:
    import docx
except ImportError:
    docx = None

# Import all logic from your original file
# Make sure main.py is in the same directory
sys.path.insert(0, os.path.dirname(__file__))
from main import (
    mcp,
    fetch_user_story,
    run_agent_pipeline,
    create_test_plan,
    create_test_suite,
    create_all_test_cases,
    link_testplan_to_story,
    extract_frames_as_base64,
    agent_bug_analyst,
    create_bug,
    AZURE_ORG,
    AZURE_PROJECT,
    STORIES_FILE,
    BUGS_FILE,
    _org_url,
    strip_html,
)

# =============================================================================
# SETUP
# =============================================================================

app = FastAPI(
    title="AI TestForge API",
    description="AI TestForge — Azure DevOps Test Plan Generator",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],        # tighten in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for d in ("input", "output", "logs"):
    os.makedirs(d, exist_ok=True)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("testforge.api")

# =============================================================================
# SQLITE CONFIG STORE — persists project configs across restarts
# =============================================================================

DB_PATH = os.path.join(os.path.dirname(__file__), "testforge_config.db")

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            sub TEXT DEFAULT '',
            ado_org TEXT DEFAULT '',
            ado_project TEXT DEFAULT '',
            ado_pat TEXT DEFAULT '',
            openai_key TEXT DEFAULT '',
            kb_file TEXT DEFAULT '',
            kb_size TEXT DEFAULT '',
            inp_file TEXT DEFAULT '',
            inp_size TEXT DEFAULT '',
            inp_meta TEXT DEFAULT '',
            videos TEXT DEFAULT '[]',
            env_saved INTEGER DEFAULT 0,
            status TEXT DEFAULT 'err',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS run_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            date TEXT NOT NULL,
            story TEXT DEFAULT '',
            title TEXT DEFAULT '',
            plan_id TEXT DEFAULT '',
            suite_id TEXT DEFAULT '',
            cases INTEGER DEFAULT 0,
            pos INTEGER DEFAULT 0,
            neg INTEGER DEFAULT 0,
            status TEXT DEFAULT 'done',
            url TEXT DEFAULT '',
            mode TEXT DEFAULT 'story',
            score INTEGER DEFAULT 70,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (project_id) REFERENCES projects(id)
        )
    """)
    conn.commit()
    conn.close()
    log.info("SQLite config DB initialized at %s", DB_PATH)

init_db()

# =============================================================================
# IN-MEMORY JOB STORE
# Each job has: id, status, progress, logs, results, created_at
# =============================================================================

jobs: Dict[str, Dict] = {}
results_store: List[Dict] = []

MCP_STARTED = False
MCP_LOCK    = threading.Lock()


def ensure_mcp():
    """Start MCP server once, lazily, thread-safe."""
    global MCP_STARTED
    with MCP_LOCK:
        if not MCP_STARTED:
            mcp.start()
            MCP_STARTED = True


def new_job(mode: str, items: list) -> str:
    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "id":         job_id,
        "mode":       mode,
        "status":     "queued",     # queued | running | done | error
        "progress":   0,            # 0-100
        "total":      len(items),
        "done":       0,
        "logs":       [],           # list of log strings
        "results":    [],
        "created_at": datetime.utcnow().isoformat(),
        "error":      None,
    }
    return job_id


def job_log(job_id: str, msg: str, level: str = "info"):
    ts  = datetime.now().strftime("%H:%M:%S")
    entry = {"ts": ts, "level": level, "msg": msg}
    jobs[job_id]["logs"].append(entry)
    log.info("[%s] %s", job_id[:8], msg)


def job_progress(job_id: str, done: int, total: int):
    jobs[job_id]["done"]     = done
    jobs[job_id]["total"]    = total
    jobs[job_id]["progress"] = int((done / total) * 100) if total else 0


def _normalize_pipeline_output(pipeline_output: Any) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Normalize run_agent_pipeline output across old/new return shapes."""
    if isinstance(pipeline_output, tuple):
        test_cases = pipeline_output[0] if len(pipeline_output) > 0 else {}
        review_report = pipeline_output[1] if len(pipeline_output) > 1 else {}
        return (
            test_cases if isinstance(test_cases, dict) else {},
            review_report if isinstance(review_report, dict) else {},
        )

    if isinstance(pipeline_output, dict):
        # Backward compatibility with older pipeline versions that returned only test_cases.
        return pipeline_output, {}

    raise TypeError(f"Unexpected run_agent_pipeline output type: {type(pipeline_output).__name__}")


# =============================================================================
# PYDANTIC MODELS
# =============================================================================

class StoryItem(BaseModel):
    id: str
    label: Optional[str] = ""


class StoriesRequest(BaseModel):
    stories: List[StoryItem]


class BugItem(BaseModel):
    story_id:   str
    video_path: str          # path relative to project root, e.g. "input/bug.mp4"


class BugsRequest(BaseModel):
    bugs: List[BugItem]


class ConfigValidateRequest(BaseModel):
    org:     Optional[str] = None
    project: Optional[str] = None
    pat:     Optional[str] = None


# =============================================================================
# BACKGROUND TASK: STORY MODE
# =============================================================================

def _run_stories_task(job_id: str, stories: List[StoryItem]):
    try:
        ensure_mcp()
        jobs[job_id]["status"] = "running"
        total = len(stories)

        for i, story_item in enumerate(stories):
            sid = story_item.id.strip().replace("US-", "").replace("us-", "")
            job_log(job_id, f"═══ Processing US-{sid} [{i+1}/{total}] ═══")

            # 1. Fetch story
            job_log(job_id, f"[MCP] Fetching US-{sid} from Azure DevOps...")
            story = fetch_user_story(sid)
            job_log(job_id, f"  Title: {story['title'][:70]}", "ok")
            job_log(job_id, f"  Iteration: {story['iteration_path']}")

            # 2. 3-agent pipeline
            job_log(job_id, "[Agent 1/3] Test Strategist — analysing requirements...")
            job_log(job_id, "[Agent 2/3] Scenario Generator — creating scenario titles...")
            job_log(job_id, "[Agent 3/3] Test Case Writer — writing steps (batched)...")
            test_cases, review_report = _normalize_pipeline_output(run_agent_pipeline(story))

            pos_count = len(test_cases.get("positive", []))
            neg_count = len(test_cases.get("negative", []))
            job_log(job_id, f"  Generated {pos_count}+ / {neg_count}- test cases", "ok")
            if review_report:
                score = review_report.get("quality_score", "N/A")
                job_log(job_id, f"  Self-critique quality score: {score}/100")

            # 3. Create in ADO
            job_log(job_id, "[MCP] Creating Test Plan...")
            plan_id, root_suite_id = create_test_plan(story)
            job_log(job_id, f"  Plan ID: {plan_id}", "ok")

            job_log(job_id, "[MCP] Creating Test Suite (requirement-based)...")
            suite_id = create_test_suite(plan_id, root_suite_id, story)
            job_log(job_id, f"  Suite ID: {suite_id}", "ok")

            job_log(job_id, f"[MCP] Creating {pos_count + neg_count} Test Cases...")
            case_ids = create_all_test_cases(plan_id, suite_id, test_cases, story["id"])
            job_log(job_id, f"  {len(case_ids)} test cases added to suite", "ok")

            job_log(job_id, "[MCP] Linking Test Plan → User Story (TestedBy)...")
            link_testplan_to_story(plan_id, story["id"])
            job_log(job_id, "  Linked", "ok")

            url = (
                f"https://dev.azure.com/{AZURE_ORG}/{AZURE_PROJECT}"
                f"/_testManagement/define?planId={plan_id}"
            )

            result = {
                "story_id":       story["id"],
                "story_title":    story["title"],
                "plan_id":        plan_id,
                "suite_id":       suite_id,
                "case_ids":       case_ids,
                "positive_count": pos_count,
                "negative_count": neg_count,
                "test_cases":     test_cases,
                "review_report":  review_report,
                "url":            url,
                "mode":           "story",
                "job_id":         job_id,
                "created_at":     datetime.utcnow().isoformat(),
            }
            jobs[job_id]["results"].append(result)
            results_store.append(result)

            job_log(job_id, f"✓ DONE! {url}", "ok")
            job_progress(job_id, i + 1, total)

        jobs[job_id]["status"]   = "done"
        jobs[job_id]["progress"] = 100
        job_log(job_id, f"═══ All {total} plan(s) complete ═══", "ok")

        _save_results_csv(jobs[job_id]["results"], "stories")

    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"]  = str(e)
        job_log(job_id, f"ERROR: {e}", "error")
        log.exception("Story task failed")


# =============================================================================
# BACKGROUND TASK: BUG MODE
# =============================================================================

def _run_bugs_task(job_id: str, bugs: List[BugItem]):
    try:
        ensure_mcp()
        jobs[job_id]["status"] = "running"
        total = len(bugs)

        for i, bug_item in enumerate(bugs):
            sid        = bug_item.story_id.strip().replace("US-", "")
            video_path = bug_item.video_path

            job_log(job_id, f"═══ Bug for US-{sid} | {video_path} [{i+1}/{total}] ═══")

            # 1. Fetch story
            job_log(job_id, f"[MCP] Fetching US-{sid}...")
            story = fetch_user_story(sid)
            job_log(job_id, f"  Title: {story['title'][:70]}", "ok")

            # 2. Extract video frames
            job_log(job_id, f"  Extracting frames from {video_path}...")
            frames_b64 = extract_frames_as_base64(video_path)
            job_log(job_id, f"  {len(frames_b64)} frames extracted", "ok")

            # 3. Bug analyst
            job_log(job_id, "[Bug Analyst] Sending frames to GPT-4o Vision...")
            bug_report = agent_bug_analyst(story, frames_b64)
            job_log(job_id, f"  Bug: {bug_report.get('bug_title','')[:60]}", "ok")
            job_log(job_id, f"  Severity: {bug_report.get('severity','?')} | Priority: {bug_report.get('priority','?')}")

            # 4. Create bug work item
            job_log(job_id, "[MCP] Creating Bug work item in ADO...")
            bug_info = create_bug(bug_report, sid)
            job_log(job_id, f"  Bug ID: {bug_info['bug_id']}", "ok")

            # 5. 3-agent pipeline
            job_log(job_id, "[Agent 1/3] Test Strategist...")
            job_log(job_id, "[Agent 2/3] Scenario Generator...")
            job_log(job_id, "[Agent 3/3] Test Case Writer (batched)...")
            test_cases, review_report = _normalize_pipeline_output(run_agent_pipeline(story))

            pos_count = len(test_cases.get("positive", []))
            neg_count = len(test_cases.get("negative", []))
            job_log(job_id, f"  Generated {pos_count}+ / {neg_count}- test cases", "ok")
            if review_report:
                score = review_report.get("quality_score", "N/A")
                job_log(job_id, f"  Self-critique quality score: {score}/100")

            # 6. Create plan/suite/cases
            job_log(job_id, "[MCP] Creating Test Plan...")
            plan_id, root_suite_id = create_test_plan(story)
            suite_id = create_test_suite(plan_id, root_suite_id, story)
            case_ids = create_all_test_cases(plan_id, suite_id, test_cases, story["id"])
            link_testplan_to_story(plan_id, story["id"])

            url = (
                f"https://dev.azure.com/{AZURE_ORG}/{AZURE_PROJECT}"
                f"/_testManagement/define?planId={plan_id}"
            )

            result = {
                "story_id":       story["id"],
                "story_title":    story["title"],
                "bug_id":         bug_info["bug_id"],
                "bug_url":        bug_info["bug_url"],
                "bug_title":      bug_report.get("bug_title", ""),
                "severity":       bug_report.get("severity", ""),
                "priority":       bug_report.get("priority", ""),
                "video_path":     video_path,
                "plan_id":        plan_id,
                "suite_id":       suite_id,
                "case_ids":       case_ids,
                "positive_count": pos_count,
                "negative_count": neg_count,
                "test_cases":     test_cases,
                "review_report":  review_report,
                "url":            url,
                "mode":           "bug",
                "job_id":         job_id,
                "created_at":     datetime.utcnow().isoformat(),
            }
            jobs[job_id]["results"].append(result)
            results_store.append(result)

            job_log(job_id, f"✓ Bug #{bug_info['bug_id']} + Plan #{plan_id} created", "ok")
            job_log(job_id, f"  Test Plan: {url}")
            job_progress(job_id, i + 1, total)

        jobs[job_id]["status"]   = "done"
        jobs[job_id]["progress"] = 100
        job_log(job_id, f"═══ All {total} bug(s) processed ═══", "ok")

        _save_results_csv(jobs[job_id]["results"], "bugs")

    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"]  = str(e)
        job_log(job_id, f"ERROR: {e}", "error")
        log.exception("Bug task failed")


# =============================================================================
# CSV SAVE HELPER
# =============================================================================

def _save_results_csv(results: list, mode: str):
    ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = Path("output") / f"results_{mode}_{ts}.csv"
    if not results:
        return
    fieldnames = list(results[0].keys())
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in results:
            row = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in r.items()}
            w.writerow(row)
    log.info("CSV saved: %s", out)


# =============================================================================
# ROUTES
# =============================================================================

@app.get("/health")
def health():
    return {
        "status":      "ok",
        "mcp_running": MCP_STARTED,
        "jobs_count":  len(jobs),
        "results_count": len(results_store),
    }


@app.get("/api/dashboard/stats", summary="Dashboard statistics")
def dashboard_stats():
    """Aggregated stats for the dashboard view."""
    conn = get_db()
    # Total runs
    runs = conn.execute("SELECT COUNT(*) as cnt FROM run_history").fetchone()
    total_runs = runs["cnt"] if runs else 0
    # Total cases
    cases_row = conn.execute("SELECT COALESCE(SUM(cases),0) as total, COALESCE(SUM(pos),0) as pos, COALESCE(SUM(neg),0) as neg FROM run_history").fetchone()
    total_cases = cases_row["total"] if cases_row else 0
    total_pos = cases_row["pos"] if cases_row else 0
    total_neg = cases_row["neg"] if cases_row else 0
    # Plans count
    plans = conn.execute("SELECT COUNT(DISTINCT plan_id) as cnt FROM run_history WHERE plan_id != ''").fetchone()
    total_plans = plans["cnt"] if plans else 0
    # Recent runs
    recent = conn.execute("SELECT * FROM run_history ORDER BY created_at DESC LIMIT 10").fetchall()
    recent_runs = [dict(r) for r in recent]
    # Run history for trend (last 7 days)
    trend = conn.execute("""
        SELECT date, SUM(cases) as cases, SUM(pos) as accepted, SUM(neg) as rejected
        FROM run_history WHERE date >= date('now', '-7 days')
        GROUP BY date ORDER BY date
    """).fetchall()
    trend_data = [dict(t) for t in trend]
    conn.close()

    # Pending reviews
    pending_count = sum(1 for r in pending_reviews.values() if r["status"] == "ready")
    accepted_in_reviews = 0
    rejected_in_reviews = 0
    for r in pending_reviews.values():
        for res in r.get("results", []):
            accepted_in_reviews += res.get("accepted_count", 0)
            rejected_in_reviews += res.get("rejected_count", 0)

    return {
        "user_stories": len(jobs),  # approximation
        "test_cases_generated": total_cases,
        "test_plans": total_plans,
        "bugs_logged": 0,  # filled from frontend
        "test_suites": total_plans,  # 1:1 with plans
        "total_pos": total_pos,
        "total_neg": total_neg,
        "pending_reviews": pending_count,
        "accepted_in_reviews": accepted_in_reviews,
        "rejected_in_reviews": rejected_in_reviews,
        "recent_runs": recent_runs,
        "trend": trend_data,
        "total_runs": total_runs,
    }


@app.get("/", include_in_schema=False)
def serve_ui():
    html_path = os.path.join(os.path.dirname(__file__), "index.html")
    with open(html_path, "r", encoding="utf-8") as f:
        content = f.read()
    return HTMLResponse(content)


# ── SESSION CONFIG (lightweight save/load for current session) ────────────────

@app.get("/api/session/config", summary="Load saved session config")
def get_session_config():
    """Returns the most recently saved config (or from .env defaults)."""
    conn = get_db()
    row = conn.execute("SELECT * FROM projects ORDER BY updated_at DESC LIMIT 1").fetchone()
    conn.close()
    if row:
        p = dict(row)
        return {"exists": True, "config": {
            "id": p["id"], "ado_org": p["ado_org"], "ado_project": p["ado_project"],
            "ado_pat": p["ado_pat"], "openai_key": p["openai_key"],
            "kb_file": p["kb_file"], "kb_size": p["kb_size"],
        }}
    # Fallback: return from .env
    return {"exists": False, "config": {
        "ado_org": AZURE_ORG, "ado_project": AZURE_PROJECT, "ado_pat": "", "openai_key": "",
        "kb_file": "", "kb_size": "",
    }}


class SessionConfigSave(BaseModel):
    ado_org: str = ""
    ado_project: str = ""
    ado_pat: str = ""
    openai_key: str = ""
    kb_file: str = ""
    kb_size: str = ""


@app.post("/api/session/config", summary="Save session config")
def save_session_config(cfg: SessionConfigSave):
    """Saves the current session's config to SQLite for persistence across refreshes."""
    conn = get_db()
    pid = "default"
    conn.execute("""
        INSERT INTO projects (id, name, ado_org, ado_project, ado_pat, openai_key, kb_file, kb_size, env_saved, status, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 'ok', CURRENT_TIMESTAMP)
        ON CONFLICT(id) DO UPDATE SET
            ado_org=excluded.ado_org, ado_project=excluded.ado_project,
            ado_pat=excluded.ado_pat, openai_key=excluded.openai_key,
            kb_file=excluded.kb_file, kb_size=excluded.kb_size,
            env_saved=1, status='ok', updated_at=CURRENT_TIMESTAMP
    """, (pid, "Default", cfg.ado_org, cfg.ado_project, cfg.ado_pat, cfg.openai_key, cfg.kb_file, cfg.kb_size))
    conn.commit()
    conn.close()
    return {"saved": True}


# =============================================================================
# PROJECT CONFIG CRUD (SQLite)
# =============================================================================

class ProjectConfig(BaseModel):
    id: str
    name: str
    sub: str = ""
    ado_org: str = ""
    ado_project: str = ""
    ado_pat: str = ""
    openai_key: str = ""
    kb_file: str = ""
    kb_size: str = ""
    inp_file: str = ""
    inp_size: str = ""
    inp_meta: str = ""
    videos: List[Dict] = []
    env_saved: bool = False
    status: str = "err"


@app.get("/api/configs", summary="Get all saved project configs")
def get_configs():
    conn = get_db()
    rows = conn.execute("SELECT * FROM projects ORDER BY created_at").fetchall()
    conn.close()
    projects = []
    for r in rows:
        p = dict(r)
        p["videos"] = json.loads(p.get("videos") or "[]")
        p["env_saved"] = bool(p.get("env_saved"))
        projects.append(p)
    return {"projects": projects}


@app.post("/api/configs", summary="Save or update a project config")
def save_config(cfg: ProjectConfig):
    conn = get_db()
    videos_json = json.dumps(cfg.videos)
    conn.execute("""
        INSERT INTO projects (id, name, sub, ado_org, ado_project, ado_pat, openai_key,
                              kb_file, kb_size, inp_file, inp_size, inp_meta, videos,
                              env_saved, status, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(id) DO UPDATE SET
            name=excluded.name, sub=excluded.sub,
            ado_org=excluded.ado_org, ado_project=excluded.ado_project,
            ado_pat=excluded.ado_pat, openai_key=excluded.openai_key,
            kb_file=excluded.kb_file, kb_size=excluded.kb_size,
            inp_file=excluded.inp_file, inp_size=excluded.inp_size,
            inp_meta=excluded.inp_meta, videos=excluded.videos,
            env_saved=excluded.env_saved, status=excluded.status,
            updated_at=CURRENT_TIMESTAMP
    """, (cfg.id, cfg.name, cfg.sub, cfg.ado_org, cfg.ado_project, cfg.ado_pat,
          cfg.openai_key, cfg.kb_file, cfg.kb_size, cfg.inp_file, cfg.inp_size,
          cfg.inp_meta, videos_json, int(cfg.env_saved), cfg.status))
    conn.commit()
    conn.close()
    return {"saved": True, "id": cfg.id}


@app.delete("/api/configs/{project_id}", summary="Delete a project config")
def delete_config(project_id: str):
    conn = get_db()
    conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    conn.commit()
    conn.close()
    return {"deleted": True, "id": project_id}


# ── RUN HISTORY (SQLite) ────────────────────────────────────────────────────

class RunHistoryEntry(BaseModel):
    project_id: str
    date: str
    story: str = ""
    title: str = ""
    plan_id: str = ""
    suite_id: str = ""
    cases: int = 0
    pos: int = 0
    neg: int = 0
    status: str = "done"
    url: str = ""
    mode: str = "story"
    score: int = 70


@app.get("/api/history/{project_id}", summary="Get run history for a project")
def get_history(project_id: str):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM run_history WHERE project_id = ? ORDER BY created_at DESC LIMIT 50",
        (project_id,)
    ).fetchall()
    conn.close()
    return {"history": [dict(r) for r in rows]}


@app.get("/api/history", summary="Get all run history")
def get_all_history():
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM run_history ORDER BY created_at DESC LIMIT 100"
    ).fetchall()
    conn.close()
    return {"history": [dict(r) for r in rows]}


@app.post("/api/history", summary="Save a run history entry")
def save_history(entry: RunHistoryEntry):
    conn = get_db()
    conn.execute("""
        INSERT INTO run_history (project_id, date, story, title, plan_id, suite_id,
                                 cases, pos, neg, status, url, mode, score)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (entry.project_id, entry.date, entry.story, entry.title, entry.plan_id,
          entry.suite_id, entry.cases, entry.pos, entry.neg, entry.status,
          entry.url, entry.mode, entry.score))
    conn.commit()
    conn.close()
    return {"saved": True}


# ── STORY MODE ──────────────────────────────────────────────────────────────

@app.post("/api/stories/run", summary="Generate test plans from User Story IDs")
def run_stories(req: StoriesRequest, background_tasks: BackgroundTasks):
    """
    Accepts a list of story IDs, creates a background job, returns job_id.

    Request body:
    ```json
    {
      "stories": [
        {"id": "27272", "label": "Chart label bug"},
        {"id": "27300"}
      ]
    }
    ```
    """
    if not req.stories:
        raise HTTPException(400, "No stories provided")

    job_id = new_job("stories", req.stories)
    background_tasks.add_task(_run_stories_task, job_id, req.stories)

    return {
        "job_id":  job_id,
        "status":  "queued",
        "total":   len(req.stories),
        "message": f"Job queued — poll GET /api/jobs/{job_id} for status",
    }


# ── BUG MODE ─────────────────────────────────────────────────────────────────

@app.post("/api/bugs/run", summary="Analyse bug videos and generate test plans")
def run_bugs(req: BugsRequest, background_tasks: BackgroundTasks):
    """
    Accepts story_id + video_path pairs. Videos must already be on disk
    (use POST /api/upload/video first if uploading from UI).

    Request body:
    ```json
    {
      "bugs": [
        {"story_id": "27272", "video_path": "input/recording.mp4"}
      ]
    }
    ```
    """
    if not req.bugs:
        raise HTTPException(400, "No bug entries provided")

    for bug in req.bugs:
        if not Path(bug.video_path).is_file():
            raise HTTPException(400, f"Video not found: {bug.video_path}")

    job_id = new_job("bugs", req.bugs)
    background_tasks.add_task(_run_bugs_task, job_id, req.bugs)

    return {
        "job_id":  job_id,
        "status":  "queued",
        "total":   len(req.bugs),
        "message": f"Job queued — poll GET /api/jobs/{job_id} for status",
    }


# ── JOB STATUS ───────────────────────────────────────────────────────────────

@app.get("/api/jobs/{job_id}", summary="Get job status and progress")
def get_job(job_id: str):
    """
    Returns current status, progress (0-100), log tail, and results if done.

    Statuses: queued | running | done | error
    """
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, f"Job {job_id} not found")

    return {
        "id":         job["id"],
        "mode":       job["mode"],
        "status":     job["status"],
        "progress":   job["progress"],
        "done":       job["done"],
        "total":      job["total"],
        "log_tail":   job["logs"][-30:],   # last 30 log lines
        "results":    job["results"],
        "error":      job["error"],
        "created_at": job["created_at"],
    }


@app.get("/api/jobs", summary="List all jobs")
def list_jobs():
    return [
        {
            "id":       j["id"],
            "mode":     j["mode"],
            "status":   j["status"],
            "progress": j["progress"],
            "total":    j["total"],
            "created_at": j["created_at"],
        }
        for j in sorted(jobs.values(), key=lambda x: x["created_at"], reverse=True)
    ]


# ── SERVER-SENT EVENTS (live log stream) ─────────────────────────────────────

@app.get("/api/jobs/{job_id}/logs", summary="Stream live logs via SSE")
async def stream_logs(job_id: str):
    """
    Returns a Server-Sent Events stream.
    Connect from JS:
      const es = new EventSource('/api/jobs/{job_id}/logs');
      es.onmessage = e => console.log(JSON.parse(e.data));
    """
    if job_id not in jobs:
        raise HTTPException(404, f"Job {job_id} not found")

    async def event_generator():
        sent = 0
        while True:
            job  = jobs.get(job_id, {})
            logs = job.get("logs", [])

            # Send any new log lines
            while sent < len(logs):
                entry = logs[sent]
                yield f"data: {json.dumps(entry)}\n\n"
                sent += 1

            # Send progress ping every 0.5s
            yield f"data: {json.dumps({'type':'progress','progress':job.get('progress',0),'status':job.get('status','')})}\n\n"

            if job.get("status") in ("done", "error"):
                yield f"data: {json.dumps({'type':'end','status':job['status']})}\n\n"
                break

            await asyncio.sleep(0.5)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ── RESULTS ──────────────────────────────────────────────────────────────────

@app.get("/api/results", summary="Get all results across all completed jobs")
def get_all_results(mode: Optional[str] = None):
    data = results_store
    if mode:
        data = [r for r in data if r.get("mode") == mode]
    return {"count": len(data), "results": data}


@app.get("/api/results/{job_id}", summary="Get results for a specific job")
def get_job_results(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, f"Job {job_id} not found")
    return {"job_id": job_id, "results": job["results"]}


# ── VIDEO UPLOAD ──────────────────────────────────────────────────────────────

@app.post("/api/upload/video", summary="Upload a bug recording video")
async def upload_video(file: UploadFile = File(...)):
    """
    Saves uploaded video to the input/ folder.
    Returns the path to use in /api/bugs/run requests.

    Accepted types: video/mp4, video/avi, video/mov, video/mkv
    """
    allowed = {"video/mp4", "video/avi", "video/quicktime", "video/x-matroska", "video/x-msvideo"}
    if file.content_type not in allowed:
        raise HTTPException(400, f"Unsupported file type: {file.content_type}. Use MP4, AVI, MOV, or MKV.")

    safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", file.filename)
    dest      = Path("input") / safe_name

    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)

    size_mb = dest.stat().st_size / (1024 * 1024)
    log.info("Video uploaded: %s (%.1f MB)", dest, size_mb)

    return {
        "filename":   safe_name,
        "video_path": str(dest),
        "size_mb":    round(size_mb, 2),
        "message":    f"Use video_path='{dest}' in /api/bugs/run",
    }


# ── CONFIG VALIDATE ───────────────────────────────────────────────────────────

@app.post("/api/config/validate", summary="Validate Azure DevOps connection")
def validate_config(req: ConfigValidateRequest):
    """
    Attempts to start MCP and list tools to verify credentials.
    Pass overrides OR leave blank to use values from config/.env
    """
    try:
        ensure_mcp()
        tools = mcp.list_tools()
        return {
            "status":    "ok",
            "org":       AZURE_ORG,
            "project":   AZURE_PROJECT,
            "mcp_tools": len(tools),
            "tools":     [t.get("name") for t in tools],
        }
    except Exception as e:
        raise HTTPException(500, f"ADO connection failed: {e}")


# ── MCP TOOLS LIST ────────────────────────────────────────────────────────────

@app.get("/api/tools", summary="List all available ADO MCP tools")
def list_tools():
    try:
        ensure_mcp()
        tools = mcp.list_tools()
        return {"count": len(tools), "tools": tools}
    except Exception as e:
        raise HTTPException(500, str(e))


# ── FILE UPLOADS (for multi-project support) ─────────────────────────────────

# ── KNOWLEDGE BASE MULTI-FORMAT UPLOAD ────────────────────────────────────────

def _parse_pdf(file_bytes: bytes) -> str:
    """Extract text from PDF bytes."""
    if PyPDF2 is None:
        raise HTTPException(400, "PyPDF2 not installed. Run: pip install PyPDF2")
    import io
    reader = PyPDF2.PdfReader(io.BytesIO(file_bytes))
    text_parts = []
    for page in reader.pages:
        text_parts.append(page.extract_text() or "")
    return "\n".join(text_parts)


def _parse_docx(file_bytes: bytes) -> str:
    """Extract text from DOCX bytes."""
    if docx is None:
        raise HTTPException(400, "python-docx not installed. Run: pip install python-docx")
    import io
    doc = docx.Document(io.BytesIO(file_bytes))
    return "\n".join([para.text for para in doc.paragraphs])


def _text_to_knowledge_json(text: str, filename: str) -> dict:
    """Convert plain text content into knowledge base JSON structure."""
    lines = [l.strip() for l in text.strip().split("\n") if l.strip()]
    
    kb = {
        "app_name": filename.rsplit(".", 1)[0],
        "app_description": "",
        "source_file": filename,
        "raw_content": text[:5000],
        "navigation": {},
        "key_fields": {},
        "known_issues": [],
        "extra_context": "\n".join(lines),
    }
    
    # Try to extract structured sections from text
    current_section = "description"
    section_content = []
    
    for line in lines:
        lower = line.lower()
        if any(kw in lower for kw in ["navigation", "nav flow", "user flow"]):
            if section_content:
                kb["app_description"] = " ".join(section_content)
            current_section = "navigation"
            section_content = []
        elif any(kw in lower for kw in ["known issue", "bug", "defect"]):
            current_section = "known_issues"
            section_content = []
        elif any(kw in lower for kw in ["field", "input", "form"]):
            current_section = "fields"
            section_content = []
        else:
            section_content.append(line)
    
    if current_section == "description" and section_content:
        kb["app_description"] = " ".join(section_content[:3])
    
    return kb


@app.post("/api/upload/knowledge-base-multi", summary="Upload knowledge base (JSON, PDF, TXT, DOCX)")
async def upload_knowledge_base_multi(file: UploadFile = File(...)):
    """
    Upload a knowledge base file in any supported format.
    Supported: .json, .pdf, .txt, .md, .docx
    Returns the parsed content and saves as knowledge_base.json.
    """
    content = await file.read()
    filename = file.filename or "unknown"
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    
    if ext == "json":
        try:
            data = json.loads(content)
        except json.JSONDecodeError as e:
            raise HTTPException(400, f"Invalid JSON: {e}")
    elif ext == "pdf":
        text = _parse_pdf(content)
        if not text.strip():
            raise HTTPException(400, "Could not extract text from PDF. The file may be image-based.")
        data = _text_to_knowledge_json(text, filename)
    elif ext == "docx":
        text = _parse_docx(content)
        if not text.strip():
            raise HTTPException(400, "Could not extract text from DOCX.")
        data = _text_to_knowledge_json(text, filename)
    elif ext in ("txt", "md", "text"):
        text = content.decode("utf-8", errors="replace")
        data = _text_to_knowledge_json(text, filename)
    else:
        raise HTTPException(400, f"Unsupported file type: .{ext}. Use .json, .pdf, .txt, .md, or .docx")
    
    # Save as knowledge_base.json
    with open("knowledge_base.json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    
    log.info("Knowledge base updated from %s (%d bytes)", filename, len(content))
    
    return {
        "status": "ok",
        "filename": filename,
        "format": ext,
        "size": len(content),
        "sections": list(data.keys()),
        "preview": {k: (str(v)[:100] + "..." if len(str(v)) > 100 else str(v)) for k, v in list(data.items())[:5]},
        "message": f"Knowledge base loaded from {filename}",
    }


# ── PREVIEW/ACCEPT WORKFLOW ───────────────────────────────────────────────────

# In-memory store for pending test case reviews
pending_reviews: Dict[str, Dict] = {}


class GeneratePreviewRequest(BaseModel):
    stories: List[StoryItem]


class AcceptTestCaseRequest(BaseModel):
    review_id: str
    case_index: int
    case_type: str  # "positive" or "negative"
    accepted: bool


class AcceptAllRequest(BaseModel):
    review_id: str


class CreateInAdoRequest(BaseModel):
    review_id: str


@app.post("/api/generate/preview", summary="Generate test cases for preview (no ADO creation)")
def generate_preview(req: GeneratePreviewRequest, background_tasks: BackgroundTasks):
    """
    Runs the AI pipeline to generate test cases but does NOT create them in ADO.
    Returns a review_id. Poll GET /api/review/{review_id} for results.
    """
    if not req.stories:
        raise HTTPException(400, "No stories provided")
    
    review_id = str(uuid.uuid4())
    pending_reviews[review_id] = {
        "id": review_id,
        "status": "generating",  # generating | ready | accepted | created | error
        "stories": [s.dict() for s in req.stories],
        "results": [],
        "logs": [],
        "created_at": datetime.utcnow().isoformat(),
        "error": None,
    }
    
    background_tasks.add_task(_generate_preview_task, review_id, req.stories)
    
    return {
        "review_id": review_id,
        "status": "generating",
        "message": "AI is generating test cases. Poll GET /api/review/{review_id} for results.",
    }


def _generate_preview_task(review_id: str, stories: List[StoryItem]):
    """Background task: generate test cases without creating in ADO."""
    try:
        ensure_mcp()
        review = pending_reviews[review_id]
        
        for i, story_item in enumerate(stories):
            sid = story_item.id.strip().replace("US-", "").replace("us-", "")
            review["logs"].append(f"Fetching US-{sid}...")
            
            story = fetch_user_story(sid)
            review["logs"].append(f"Title: {story['title']}")
            review["logs"].append("Running AI pipeline (3 agents)...")
            
            test_cases, review_report = _normalize_pipeline_output(run_agent_pipeline(story))
            
            pos_cases = test_cases.get("positive", [])
            neg_cases = test_cases.get("negative", [])
            
            # Mark all cases as pending acceptance
            for idx, tc in enumerate(pos_cases):
                tc["_index"] = idx
                tc["_accepted"] = None  # None = pending, True = accepted, False = rejected
            for idx, tc in enumerate(neg_cases):
                tc["_index"] = idx
                tc["_accepted"] = None
            
            result = {
                "story_id": story["id"],
                "story_title": story["title"],
                "iteration_path": story.get("iteration_path", ""),
                "test_cases": test_cases,
                "review_report": review_report,
                "positive_count": len(pos_cases),
                "negative_count": len(neg_cases),
                "accepted_count": 0,
                "rejected_count": 0,
            }
            review["results"].append(result)
            review["logs"].append(f"Generated {len(pos_cases)}+ / {len(neg_cases)}- test cases for US-{sid}")
        
        review["status"] = "ready"
        review["logs"].append("All test cases generated. Ready for review.")
        
    except Exception as e:
        pending_reviews[review_id]["status"] = "error"
        pending_reviews[review_id]["error"] = str(e)
        pending_reviews[review_id]["logs"].append(f"ERROR: {e}")
        log.exception("Preview generation failed")


@app.get("/api/review/{review_id}", summary="Get review status and test cases")
def get_review(review_id: str):
    """Returns current review status with all generated test cases and their acceptance state."""
    review = pending_reviews.get(review_id)
    if not review:
        raise HTTPException(404, f"Review {review_id} not found")
    return review


@app.post("/api/review/accept-case", summary="Accept or reject a single test case")
def accept_test_case(req: AcceptTestCaseRequest):
    """Accept or reject a single test case in the review."""
    review = pending_reviews.get(req.review_id)
    if not review:
        raise HTTPException(404, f"Review {req.review_id} not found")
    if review["status"] not in ("ready", "accepted"):
        raise HTTPException(400, f"Review is in '{review['status']}' state. Cannot modify.")
    
    # Find the test case
    for result in review["results"]:
        cases = result["test_cases"].get(req.case_type, [])
        if req.case_index < len(cases):
            old_val = cases[req.case_index].get("_accepted")
            cases[req.case_index]["_accepted"] = req.accepted
            
            # Update counts
            if old_val is None:
                if req.accepted:
                    result["accepted_count"] += 1
                else:
                    result["rejected_count"] += 1
            elif old_val and not req.accepted:
                result["accepted_count"] -= 1
                result["rejected_count"] += 1
            elif not old_val and req.accepted:
                result["rejected_count"] -= 1
                result["accepted_count"] += 1
            
            return {
                "status": "ok",
                "case_index": req.case_index,
                "case_type": req.case_type,
                "accepted": req.accepted,
                "total_accepted": result["accepted_count"],
                "total_rejected": result["rejected_count"],
            }
    
    raise HTTPException(404, "Test case not found at given index")


@app.post("/api/review/accept-all", summary="Accept all test cases in a review")
def accept_all_cases(req: AcceptAllRequest):
    """Mark all test cases as accepted."""
    review = pending_reviews.get(req.review_id)
    if not review:
        raise HTTPException(404, f"Review {req.review_id} not found")
    if review["status"] not in ("ready", "accepted"):
        raise HTTPException(400, f"Review is in '{review['status']}' state. Cannot modify.")
    
    total_accepted = 0
    for result in review["results"]:
        for case_type in ("positive", "negative"):
            cases = result["test_cases"].get(case_type, [])
            for tc in cases:
                tc["_accepted"] = True
                total_accepted += 1
        result["accepted_count"] = result["positive_count"] + result["negative_count"]
        result["rejected_count"] = 0
    
    review["status"] = "accepted"
    review["logs"].append(f"All {total_accepted} test cases accepted.")
    
    return {"status": "ok", "total_accepted": total_accepted}


@app.post("/api/review/create-in-ado", summary="Create accepted test cases in Azure DevOps")
def create_in_ado(req: CreateInAdoRequest, background_tasks: BackgroundTasks):
    """
    Creates Test Plan, Test Suite, and accepted Test Cases in Azure DevOps.
    Only accepted cases are created. Returns job info.
    """
    review = pending_reviews.get(req.review_id)
    if not review:
        raise HTTPException(404, f"Review {req.review_id} not found")
    
    # Check that at least one test case is accepted
    has_accepted = False
    for result in review["results"]:
        for case_type in ("positive", "negative"):
            for tc in result["test_cases"].get(case_type, []):
                if tc.get("_accepted"):
                    has_accepted = True
                    break
            if has_accepted:
                break
        if has_accepted:
            break
    
    if not has_accepted:
        raise HTTPException(400, "No test cases accepted. Accept at least one test case before creating in ADO.")
    
    review["status"] = "creating"
    review["logs"].append("Creating Test Plan, Suite, and Cases in Azure DevOps...")
    
    background_tasks.add_task(_create_in_ado_task, req.review_id)
    
    return {
        "review_id": req.review_id,
        "status": "creating",
        "message": "Creating in Azure DevOps. Poll GET /api/review/{review_id} for status.",
    }


def _create_in_ado_task(review_id: str):
    """Background task: create accepted test cases in ADO."""
    try:
        ensure_mcp()
        review = pending_reviews[review_id]
        
        for result in review["results"]:
            sid = result["story_id"]
            story = fetch_user_story(str(sid))
            
            # Filter accepted test cases only
            accepted_cases = {"positive": [], "negative": []}
            for case_type in ("positive", "negative"):
                for tc in result["test_cases"].get(case_type, []):
                    if tc.get("_accepted"):
                        # Remove internal tracking fields
                        clean_tc = {k: v for k, v in tc.items() if not k.startswith("_")}
                        accepted_cases[case_type].append(clean_tc)
            
            review["logs"].append(f"Creating Test Plan for US-{sid}...")
            plan_id, root_suite_id = create_test_plan(story)
            review["logs"].append(f"Test Plan created: #{plan_id}")
            
            review["logs"].append(f"Creating Test Suite for US-{sid}...")
            suite_id = create_test_suite(plan_id, root_suite_id, story)
            review["logs"].append(f"Test Suite created: #{suite_id}")
            
            total_cases = len(accepted_cases["positive"]) + len(accepted_cases["negative"])
            review["logs"].append(f"Creating {total_cases} accepted test cases...")
            case_ids = create_all_test_cases(plan_id, suite_id, accepted_cases, story["id"])
            review["logs"].append(f"{len(case_ids)} test cases created in ADO")
            
            review["logs"].append("Linking Test Plan to User Story...")
            link_testplan_to_story(plan_id, story["id"])
            
            url = f"https://dev.azure.com/{AZURE_ORG}/{AZURE_PROJECT}/_testManagement/define?planId={plan_id}"
            
            result["plan_id"] = plan_id
            result["suite_id"] = suite_id
            result["case_ids"] = case_ids
            result["url"] = url
            result["ado_created"] = True
            
            review["logs"].append(f"✓ Done! {url}")
        
        review["status"] = "created"
        review["logs"].append("All accepted test cases successfully created in Azure DevOps!")
        
    except Exception as e:
        pending_reviews[review_id]["status"] = "error"
        pending_reviews[review_id]["error"] = str(e)
        pending_reviews[review_id]["logs"].append(f"ERROR: {e}")
        log.exception("ADO creation failed")


@app.get("/api/reviews", summary="List all pending reviews")
def list_reviews():
    """Returns all reviews with their current status."""
    return [
        {
            "id": r["id"],
            "status": r["status"],
            "stories": r["stories"],
            "created_at": r["created_at"],
            "total_cases": sum(res["positive_count"] + res["negative_count"] for res in r["results"]),
            "accepted": sum(res["accepted_count"] for res in r["results"]),
        }
        for r in sorted(pending_reviews.values(), key=lambda x: x["created_at"], reverse=True)
    ]


@app.post("/api/upload/knowledge-base", summary="Upload knowledge_base.json")
async def upload_knowledge_base(file: UploadFile = File(...)):
    """Upload a knowledge_base.json file for a different project/app."""
    if not file.filename.endswith(".json"):
        raise HTTPException(400, "File must be .json")
    content = await file.read()
    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"Invalid JSON: {e}")
    with open("knowledge_base.json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    log.info("Knowledge base updated (%d bytes)", len(content))
    return {"status": "ok", "message": "knowledge_base.json updated", "size": len(content)}


@app.post("/api/upload/input-config", summary="Upload input.json")
async def upload_input_config(file: UploadFile = File(...)):
    """Upload an input.json with story IDs and bug entries."""
    if not file.filename.endswith(".json"):
        raise HTTPException(400, "File must be .json")
    content = await file.read()
    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"Invalid JSON: {e}")
    with open("input.json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    log.info("Input config updated (%d bytes)", len(content))
    return {"status": "ok", "message": "input.json updated", "stories": len(data.get("stories", [])), "bugs": len(data.get("bugs", []))}


@app.get("/api/config/current", summary="Get current configuration")
def get_current_config():
    """Returns current project config (non-sensitive)."""
    kb_exists = Path("knowledge_base.json").is_file()
    input_exists = Path("input.json").is_file()
    env_exists = Path("config/.env").is_file()

    kb_info = {}
    if kb_exists:
        try:
            with open("knowledge_base.json", "r", encoding="utf-8") as f:
                kb = json.load(f)
            kb_info = {"builders": list(kb.get("builders", {}).keys()) if isinstance(kb.get("builders"), dict) else []}
        except Exception:
            kb_info = {"error": "Could not parse"}

    input_info = {}
    if input_exists:
        try:
            with open("input.json", "r", encoding="utf-8") as f:
                inp = json.load(f)
            input_info = {"stories": len(inp.get("stories", [])), "bugs": len(inp.get("bugs", []))}
        except Exception:
            input_info = {"error": "Could not parse"}

    return {
        "org": AZURE_ORG,
        "project": AZURE_PROJECT,
        "knowledge_base": {"exists": kb_exists, **kb_info},
        "input": {"exists": input_exists, **input_info},
        "env": {"exists": env_exists},
        "mcp_running": MCP_STARTED,
    }


# ── LIST ALL USER STORIES FROM ADO ────────────────────────────────────────────

@app.get("/api/stories/list", summary="Fetch all user stories from Azure DevOps")
def list_user_stories(state: Optional[str] = None, iteration: Optional[str] = None, top: int = 200):
    """
    Queries Azure DevOps for User Stories in the project.
    Returns story ID, title, state, description, acceptance criteria.
    
    Query params:
      - state: Filter by state (e.g. "New", "Active", "Closed")
      - iteration: Filter by iteration path
      - top: Max results (default 200)
    """
    try:
        ensure_mcp()
        
        # Build WIQL query
        conditions = [
            "[System.WorkItemType] = 'User Story'",
            f"[System.TeamProject] = '{AZURE_PROJECT}'",
        ]
        if state:
            conditions.append(f"[System.State] = '{state}'")
        if iteration:
            conditions.append(f"[System.IterationPath] UNDER '{iteration}'")
        
        wiql = "SELECT [System.Id] FROM WorkItems WHERE " + " AND ".join(conditions) + " ORDER BY [System.Id] DESC"
        
        # Use wit_query_by_wiql to run WIQL and get work item IDs
        query_result = mcp.call_tool("wit_query_by_wiql", {
            "organizationUrl": _org_url(),
            "project": AZURE_PROJECT,
            "wiql": wiql,
        })
        
        # The result may have a 'raw' key containing JSON string
        if isinstance(query_result, dict) and "raw" in query_result:
            query_result = _parse_mcp_raw(query_result["raw"], "{")
        
        # Extract IDs from query result
        work_items = query_result.get("workItems", query_result.get("rows", []))
        if isinstance(work_items, list):
            ids = [item.get("id", item.get("System.Id")) for item in work_items if item]
        else:
            ids = []
        
        ids = [i for i in ids if i is not None][:top]
        
        if not ids:
            return {"count": 0, "stories": []}
        
        # Batch fetch details using wit_get_work_items_batch_by_ids (much faster)
        stories = []
        batch_size = 50
        for batch_start in range(0, len(ids), batch_size):
            batch_ids = ids[batch_start:batch_start + batch_size]
            try:
                batch_result = mcp.call_tool("wit_get_work_items_batch_by_ids", {
                    "organizationUrl": _org_url(),
                    "project": AZURE_PROJECT,
                    "ids": [int(i) for i in batch_ids],
                })
                
                # Parse raw response if needed
                if isinstance(batch_result, dict) and "raw" in batch_result:
                    batch_result = _parse_mcp_raw(batch_result["raw"], "[")
                
                items_list = batch_result if isinstance(batch_result, list) else (batch_result.get("value", batch_result.get("workItems", [])) if isinstance(batch_result, dict) else [])
                
                for item in items_list:
                    fields = item.get("fields", item)
                    story_id = str(fields.get("System.Id", item.get("id", "")))
                    title = fields.get("System.Title", "")
                    story_state = fields.get("System.State", "")
                    description = fields.get("System.Description", "")
                    acceptance = fields.get("Microsoft.VSTS.Common.AcceptanceCriteria", "")
                    iteration_path = fields.get("System.IterationPath", "")
                    assigned_to = fields.get("System.AssignedTo", "")
                    if isinstance(assigned_to, dict):
                        assigned_to = assigned_to.get("displayName", "")
                    
                    has_description = bool(description and description.strip() and description.strip() != "<div></div>")
                    has_acceptance = bool(acceptance and acceptance.strip() and acceptance.strip() != "<div></div>")
                    
                    stories.append({
                        "id": story_id,
                        "title": title,
                        "state": story_state,
                        "description": strip_html(description) if description else "",
                        "acceptance_criteria": strip_html(acceptance) if acceptance else "",
                        "has_description": has_description,
                        "has_acceptance_criteria": has_acceptance,
                        "iteration_path": iteration_path,
                        "assigned_to": assigned_to,
                    })
            except Exception as e:
                log.warning("Batch fetch failed, falling back to individual: %s", e)
                # Fallback: fetch individually
                for wid in batch_ids:
                    try:
                        result = mcp.call_tool("wit_get_work_item", {
                            "organizationUrl": _org_url(),
                            "project": AZURE_PROJECT,
                            "id": int(wid),
                        })
                        fields = result.get("fields", result)
                        story_id = str(fields.get("System.Id", wid))
                        title = fields.get("System.Title", "")
                        story_state = fields.get("System.State", "")
                        description = fields.get("System.Description", "")
                        acceptance = fields.get("Microsoft.VSTS.Common.AcceptanceCriteria", "")
                        iteration_path = fields.get("System.IterationPath", "")
                        assigned_to = fields.get("System.AssignedTo", "")
                        if isinstance(assigned_to, dict):
                            assigned_to = assigned_to.get("displayName", "")
                        has_description = bool(description and description.strip() and description.strip() != "<div></div>")
                        has_acceptance = bool(acceptance and acceptance.strip() and acceptance.strip() != "<div></div>")
                        stories.append({
                            "id": story_id, "title": title, "state": story_state,
                            "description": strip_html(description) if description else "",
                            "acceptance_criteria": strip_html(acceptance) if acceptance else "",
                            "has_description": has_description, "has_acceptance_criteria": has_acceptance,
                            "iteration_path": iteration_path, "assigned_to": assigned_to,
                        })
                    except Exception:
                        continue
        
        return {"count": len(stories), "stories": stories}
    
    except Exception as e:
        log.exception("Failed to list user stories")
        raise HTTPException(500, f"Failed to fetch stories: {e}")


@app.get("/api/stories/fetch/{story_id}", summary="Fetch a single user story details")
def fetch_story_details(story_id: str):
    """Fetch detailed info for a single user story."""
    try:
        ensure_mcp()
        story = fetch_user_story(story_id.strip().replace("US-", "").replace("us-", ""))
        
        # Get raw fields for full detail
        result = mcp.call_tool("wit_get_work_item", {
            "organizationUrl": _org_url(),
            "project": AZURE_PROJECT,
            "id": int(story_id.strip().replace("US-", "").replace("us-", "")),
        })
        fields = result.get("fields", result)
        description = fields.get("System.Description", "")
        acceptance = fields.get("Microsoft.VSTS.Common.AcceptanceCriteria", "")
        
        return {
            "id": story["id"],
            "title": story["title"],
            "requirements": story["requirements"],
            "iteration_path": story["iteration_path"],
            "area_path": story["area_path"],
            "description_raw": strip_html(description) if description else "",
            "acceptance_criteria_raw": strip_html(acceptance) if acceptance else "",
            "has_description": bool(description and description.strip() and description.strip() != "<div></div>"),
            "has_acceptance_criteria": bool(acceptance and acceptance.strip() and acceptance.strip() != "<div></div>"),
        }
    except Exception as e:
        raise HTTPException(500, f"Failed to fetch story {story_id}: {e}")


# ── UPDATE STORY DESCRIPTION ──────────────────────────────────────────────────

class UpdateDescriptionRequest(BaseModel):
    story_id: str
    description: str = ""
    acceptance_criteria: str = ""


@app.post("/api/stories/update-description", summary="Update user story description and acceptance criteria")
def update_story_description(req: UpdateDescriptionRequest):
    """
    Updates Description and/or Acceptance Criteria for a user story.
    Use when a story has no description — upload text that gets added to both fields.
    """
    try:
        ensure_mcp()
        sid = req.story_id.strip().replace("US-", "").replace("us-", "")
        
        # Build update fields
        fields_to_update = []
        if req.description:
            fields_to_update.append({
                "op": "replace",
                "path": "/fields/System.Description",
                "value": req.description,
            })
        if req.acceptance_criteria:
            fields_to_update.append({
                "op": "replace",
                "path": "/fields/Microsoft.VSTS.Common.AcceptanceCriteria",
                "value": req.acceptance_criteria,
            })
        
        if not fields_to_update:
            raise HTTPException(400, "Provide at least description or acceptance_criteria")
        
        # Use wit_update_work_item MCP tool
        result = mcp.call_tool("wit_update_work_item", {
            "organizationUrl": _org_url(),
            "project": AZURE_PROJECT,
            "id": int(sid),
            "fields": fields_to_update,
        })
        
        log.info("Updated description for US-%s", sid)
        return {
            "status": "ok",
            "story_id": sid,
            "updated_fields": ["description"] if req.description else [] + (["acceptance_criteria"] if req.acceptance_criteria else []),
            "message": f"US-{sid} description updated",
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Failed to update story: {e}")


# ── OUTPUT FILES / DOWNLOAD ───────────────────────────────────────────────────

@app.get("/api/output/files", summary="List all output result files")
def list_output_files():
    """Returns all files in the output/ directory with metadata."""
    output_dir = Path("output")
    if not output_dir.exists():
        return {"files": []}
    
    files = []
    for f in sorted(output_dir.iterdir(), reverse=True):
        if f.is_file() and not f.name.startswith("."):
            stat = f.stat()
            size_kb = round(stat.st_size / 1024, 1)
            files.append({
                "filename": f.name,
                "size": f"{size_kb} KB",
                "size_bytes": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                "download_url": f"/api/output/download/{f.name}",
            })
    
    return {"count": len(files), "files": files}


@app.get("/api/output/download/{filename}", summary="Download an output file")
def download_output_file(filename: str):
    """Download a specific output file (CSV, JSON, etc.)."""
    # Sanitize filename to prevent path traversal
    safe_name = Path(filename).name
    file_path = Path("output") / safe_name
    
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(404, f"File not found: {filename}")
    
    # Determine content type
    ext = safe_name.rsplit(".", 1)[-1].lower() if "." in safe_name else ""
    content_types = {"csv": "text/csv", "json": "application/json", "txt": "text/plain"}
    content_type = content_types.get(ext, "application/octet-stream")
    
    def file_iterator():
        with open(file_path, "rb") as f:
            while chunk := f.read(8192):
                yield chunk
    
    return StreamingResponse(
        file_iterator(),
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{safe_name}"'},
    )


# ── RUN DETAILS (enhanced) ───────────────────────────────────────────────────

@app.get("/api/runs/details", summary="Get detailed run history with outputs")
def get_run_details(limit: int = 50):
    """Returns all completed jobs with their full results and output file links."""
    completed_jobs = [
        {
            "id": j["id"],
            "mode": j["mode"],
            "status": j["status"],
            "progress": j["progress"],
            "total": j["total"],
            "done": j["done"],
            "results": j["results"],
            "logs": j["logs"][-20:],
            "created_at": j["created_at"],
            "error": j["error"],
        }
        for j in sorted(jobs.values(), key=lambda x: x["created_at"], reverse=True)[:limit]
    ]
    
    # Also include output files
    output_files = []
    output_dir = Path("output")
    if output_dir.exists():
        for f in sorted(output_dir.iterdir(), reverse=True):
            if f.is_file() and not f.name.startswith("."):
                output_files.append({
                    "filename": f.name,
                    "size": f"{round(f.stat().st_size/1024, 1)} KB",
                    "download_url": f"/api/output/download/{f.name}",
                })
    
    return {
        "jobs": completed_jobs,
        "output_files": output_files,
        "total_results": len(results_store),
    }


# ── STARTUP / SHUTDOWN ────────────────────────────────────────────────────────

def _parse_mcp_raw(raw_text: str, container: str = "{") -> Any:
    """Parse MCP raw response text that may contain safety wrapper markers.
    container: '{' for object, '[' for array."""
    close = "}" if container == "{" else "]"
    find_start = "\n" + container
    json_start = raw_text.find(find_start)
    if json_start < 0:
        json_start = raw_text.find(container)
    else:
        json_start += 1
    
    if json_start < 0:
        return {} if container == "{" else []
    
    json_str = raw_text[json_start:]
    trailing = json_str.rfind("\n<<")
    if trailing > 0:
        json_str = json_str[:trailing]
    json_end = json_str.rfind(close) + 1
    if json_end > 0:
        json_str = json_str[:json_end]
    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        # Fallback
        try:
            full = raw_text[raw_text.find(container):raw_text.rfind(close) + 1]
            return json.loads(full)
        except (json.JSONDecodeError, ValueError):
            return {} if container == "{" else []


# ── BUGS LISTING / FILTER / CSV DOWNLOAD ──────────────────────────────────────

@app.get("/api/bugs/list", summary="List bugs from Azure DevOps with filters")
def list_bugs(
    state: Optional[str] = None,
    priority: Optional[str] = None,
    tag: Optional[str] = None,
    top: int = 200,
):
    """
    Lists Bug work items from ADO with optional filters.
    - state: New, Active, Resolved, Closed
    - priority: 1, 2, 3, 4
    - tag: filter by tag (e.g. 'Leakage')
    """
    try:
        ensure_mcp()
        
        conditions = [
            "[System.WorkItemType] = 'Bug'",
            f"[System.TeamProject] = '{AZURE_PROJECT}'",
        ]
        if state:
            conditions.append(f"[System.State] = '{state}'")
        if priority:
            conditions.append(f"[Microsoft.VSTS.Common.Priority] = {priority}")
        if tag:
            conditions.append(f"[System.Tags] CONTAINS '{tag}'")
        
        wiql = "SELECT [System.Id] FROM WorkItems WHERE " + " AND ".join(conditions) + " ORDER BY [System.Id] DESC"
        
        query_result = mcp.call_tool("wit_query_by_wiql", {
            "organizationUrl": _org_url(),
            "project": AZURE_PROJECT,
            "wiql": wiql,
        })
        
        if isinstance(query_result, dict) and "raw" in query_result:
            query_result = _parse_mcp_raw(query_result["raw"], "{")
        
        work_items = query_result.get("workItems", query_result.get("rows", []))
        ids = [item.get("id") for item in work_items if item and item.get("id")][:top]
        
        if not ids:
            return {"count": 0, "bugs": []}
        
        # Batch fetch
        bugs = []
        batch_size = 50
        for batch_start in range(0, len(ids), batch_size):
            batch_ids = ids[batch_start:batch_start + batch_size]
            try:
                batch_result = mcp.call_tool("wit_get_work_items_batch_by_ids", {
                    "organizationUrl": _org_url(),
                    "project": AZURE_PROJECT,
                    "ids": [int(i) for i in batch_ids],
                })
                
                if isinstance(batch_result, dict) and "raw" in batch_result:
                    batch_result = _parse_mcp_raw(batch_result["raw"], "[")
                
                items_list = batch_result if isinstance(batch_result, list) else (
                    batch_result.get("value", batch_result.get("workItems", [])) if isinstance(batch_result, dict) else []
                )
                
                for item in items_list:
                    fields = item.get("fields", item)
                    bug_id = str(fields.get("System.Id", item.get("id", "")))
                    title = fields.get("System.Title", "")
                    bug_state = fields.get("System.State", "")
                    bug_priority = str(fields.get("Microsoft.VSTS.Common.Priority", ""))
                    severity = fields.get("Microsoft.VSTS.Common.Severity", "")
                    tags = fields.get("System.Tags", "")
                    assigned_to = fields.get("System.AssignedTo", "")
                    if isinstance(assigned_to, dict):
                        assigned_to = assigned_to.get("displayName", "")
                    iteration_path = fields.get("System.IterationPath", "")
                    area_path = fields.get("System.AreaPath", "")
                    created_date = fields.get("System.CreatedDate", "")
                    parent_id = str(fields.get("System.Parent", "")) if fields.get("System.Parent") else ""
                    
                    bugs.append({
                        "id": bug_id,
                        "title": title,
                        "state": bug_state,
                        "priority": bug_priority,
                        "severity": severity,
                        "tags": tags,
                        "assigned_to": assigned_to,
                        "iteration_path": iteration_path,
                        "area_path": area_path,
                        "created_date": created_date[:10] if created_date else "",
                        "parent_story_id": parent_id,
                        "is_leakage": "leakage" in (tags or "").lower(),
                    })
            except Exception as e:
                log.warning("Batch bug fetch failed, falling back: %s", e)
                for wid in batch_ids:
                    try:
                        result = mcp.call_tool("wit_get_work_item", {
                            "organizationUrl": _org_url(),
                            "project": AZURE_PROJECT,
                            "id": int(wid),
                        })
                        fields = result.get("fields", result)
                        bug_id = str(fields.get("System.Id", wid))
                        title = fields.get("System.Title", "")
                        bug_state = fields.get("System.State", "")
                        bug_priority = str(fields.get("Microsoft.VSTS.Common.Priority", ""))
                        severity = fields.get("Microsoft.VSTS.Common.Severity", "")
                        tags = fields.get("System.Tags", "")
                        assigned_to = fields.get("System.AssignedTo", "")
                        if isinstance(assigned_to, dict):
                            assigned_to = assigned_to.get("displayName", "")
                        iteration_path = fields.get("System.IterationPath", "")
                        area_path = fields.get("System.AreaPath", "")
                        created_date = fields.get("System.CreatedDate", "")
                        parent_id = str(fields.get("System.Parent", "")) if fields.get("System.Parent") else ""
                        bugs.append({
                            "id": bug_id, "title": title, "state": bug_state,
                            "priority": bug_priority, "severity": severity, "tags": tags,
                            "assigned_to": assigned_to, "iteration_path": iteration_path,
                            "area_path": area_path, "created_date": created_date[:10] if created_date else "",
                            "parent_story_id": parent_id,
                            "is_leakage": "leakage" in (tags or "").lower(),
                        })
                    except Exception:
                        continue
        
        return {"count": len(bugs), "bugs": bugs}
    
    except Exception as e:
        log.exception("Failed to list bugs")
        raise HTTPException(500, f"Failed to fetch bugs: {e}")


@app.get("/api/bugs/download", summary="Download bugs as CSV")
def download_bugs_csv(
    state: Optional[str] = None,
    priority: Optional[str] = None,
    tag: Optional[str] = None,
    top: int = 500,
):
    """Download filtered bugs as a CSV file."""
    result = list_bugs(state=state, priority=priority, tag=tag, top=top)
    bugs = result["bugs"]
    
    import io
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "Title", "State", "Priority", "Severity", "Tags", "Assigned To",
                     "Iteration Path", "Area Path", "Created Date", "Parent Story ID", "Is Leakage"])
    for b in bugs:
        writer.writerow([
            b["id"], b["title"], b["state"], b["priority"], b["severity"],
            b["tags"], b["assigned_to"], b["iteration_path"], b["area_path"],
            b["created_date"], b["parent_story_id"], "Yes" if b["is_leakage"] else "No",
        ])
    
    csv_content = output.getvalue()
    
    # Save to output dir too
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    tag_suffix = f"_{tag}" if tag else ""
    filename = f"bugs{tag_suffix}_{ts}.csv"
    output_path = Path("output") / filename
    output_path.write_text(csv_content, encoding="utf-8")
    
    return StreamingResponse(
        iter([csv_content]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.on_event("startup")
async def on_startup():
    log.info("TestForge API started. MCP will start on first request.")


@app.on_event("shutdown")
async def on_shutdown():
    if MCP_STARTED:
        mcp.stop()
        log.info("MCP server stopped.")