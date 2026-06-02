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

import os, sys, re, json, csv, uuid, base64, logging, asyncio, threading, time, shutil
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, BackgroundTasks, HTTPException, UploadFile, File, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel
import cv2

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
)

# =============================================================================
# SETUP
# =============================================================================

app = FastAPI(
    title="TestForge API",
    description="Azure DevOps Test Plan Generator — FastAPI backend",
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


# ── STARTUP / SHUTDOWN ────────────────────────────────────────────────────────

@app.on_event("startup")
async def on_startup():
    log.info("TestForge API started. MCP will start on first request.")


@app.on_event("shutdown")
async def on_shutdown():
    if MCP_STARTED:
        mcp.stop()
        log.info("MCP server stopped.")
