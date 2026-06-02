# =============================================================================
# FILE: main_fixed.py  (ENHANCED — 5 LLM Improvements Applied)
#
# IMPROVEMENTS OVER ORIGINAL:
# ─────────────────────────────────────────────────────────────────────────────
# 1. AGENT 1 RETRY LOOP
#    - Validates that >= 8 categories are returned
#    - Re-prompts up to MAX_STRATEGY_RETRIES times if under the minimum
#    - Logs a warning if retries are exhausted but continues with what it has
#
# 2. DYNAMIC BATCH SIZE IN AGENT 3
#    - Calculates average hint complexity from Agent 2 output
#    - Simple scenarios → larger batches (up to 15)
#    - Complex scenarios (long hints) → smaller batches (down to 6)
#    - Avoids unnecessary API calls while keeping quality high
#
# 3. AGENT 4 — SELF-CRITIQUE / QUALITY REVIEWER
#    - Runs after Agent 3 and before ADO creation
#    - Flags duplicate test cases (same action sequence)
#    - Flags missing coverage gaps (categories with no negative cases)
#    - Returns a cleaned deduplicated list + a review report
#    - agent_self_critique() is optional — set ENABLE_SELF_CRITIQUE=false to skip
#
# 4. TWO-PASS BUG ANALYST
#    - Pass 1: Describe each frame individually (what is visible/happening)
#    - Pass 2: Synthesize frame descriptions into a structured bug report
#    - More accurate than one-shot for complex or subtle bugs
#    - Falls back to one-shot if frame count is low (<=3)
#
# 5. RAG-STYLE KNOWLEDGE BASE
#    - KB sections are embedded as tagged chunks at load time
#    - build_knowledge_context() now accepts a list of relevant_keys
#    - Each agent specifies which KB sections it actually needs
#    - Only relevant chunks are injected → smaller, more focused prompts
#    - retrieve_kb_sections() does keyword-based section selection
# =============================================================================

import os, sys, re, json, csv, base64, logging, argparse, subprocess, threading, time
from datetime import datetime
from dotenv import load_dotenv
from openai import AzureOpenAI

try:
    import cv2
except ImportError:
    print("ERROR: opencv not installed. Run: pip install opencv-python")
    sys.exit(1)

# =============================================================================
# SECTION 1: CONFIGURATION
# =============================================================================
load_dotenv(dotenv_path=os.path.join("config", ".env"))

AZURE_OPENAI_ENDPOINT    = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_API_KEY     = os.getenv("AZURE_OPENAI_API_KEY")
AZURE_OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-01")
AZURE_OPENAI_DEPLOYMENT  = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")

AZURE_ORG      = os.getenv("AZURE_DEVOPS_ORG")
AZURE_PROJECT  = os.getenv("AZURE_DEVOPS_PROJECT")
AZURE_PAT      = os.getenv("AZURE_DEVOPS_PAT")
ITERATION_PATH = os.getenv("ITERATION_PATH", "")
AREA_PATH      = os.getenv("AREA_PATH", "")

STORIES_FILE        = os.getenv("STORIES_FILE",        "stories_input.json")
BUGS_FILE           = os.getenv("BUGS_FILE",            "bugs_input.json")
KNOWLEDGE_BASE_FILE = os.getenv("KNOWLEDGE_BASE_FILE",  "knowledge_base.json")

VIDEO_FRAME_COUNT   = int(os.getenv("VIDEO_FRAME_COUNT",   "10"))
VIDEO_FRAME_QUALITY = int(os.getenv("VIDEO_FRAME_QUALITY", "85"))

# ── Improvement toggles ────────────────────────────────────────────────────
MAX_STRATEGY_RETRIES = int(os.getenv("MAX_STRATEGY_RETRIES", "3"))   # Improvement 1
MIN_STRATEGY_CATEGORIES = int(os.getenv("MIN_STRATEGY_CATEGORIES", "8"))  # Improvement 1
ENABLE_SELF_CRITIQUE = os.getenv("ENABLE_SELF_CRITIQUE", "true").lower() == "true"  # Improvement 3
TWO_PASS_BUG_MIN_FRAMES = int(os.getenv("TWO_PASS_BUG_MIN_FRAMES", "4"))  # Improvement 4

# =============================================================================
# SECTION 2: DIRECTORY & LOGGING SETUP
# =============================================================================
for d in ("input", "output", "logs"):
    os.makedirs(d, exist_ok=True)

log_filename = os.path.join(
    "logs", "run_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".log"
)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[
        logging.FileHandler(log_filename, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# =============================================================================
# SECTION 3: PROJECT KNOWLEDGE BASE
# =============================================================================

PROJECT_KNOWLEDGE: dict = {}

# ── IMPROVEMENT 5: RAG-style KB chunk registry ─────────────────────────────
# Each section key maps to a human label and the KB dict key(s) it covers.
# retrieve_kb_sections() uses this to select only what each agent needs.
KB_SECTION_MAP = {
    "auth":        {"label": "Authentication / Login flow",  "keys": ["authentication", "login_flow", "test_phone", "test_otp", "builders"]},
    "navigation":  {"label": "Navigation paths",             "keys": ["navigation"]},
    "fields":      {"label": "Key UI fields",                "keys": ["key_fields"]},
    "known":       {"label": "Known issues / regressions",   "keys": ["known_issues"]},
    "tech":        {"label": "Tech stack",                   "keys": ["tech_stack"]},
    "extra":       {"label": "Extra context",                "keys": ["extra_context"]},
    "app":         {"label": "App name / URL",               "keys": ["app_name", "app_url", "builders"]},
    # ── NEW: 3 added sections ──────────────────────────────────────────────
    "errors":      {"label": "Exact UI error messages",      "keys": ["error_messages"]},
    "validation":  {"label": "Validation rules and limits",  "keys": ["validation_rules"]},
    "testdata":    {"label": "Ready-to-use test data values","keys": ["test_data"]},
    # ── FIX: test_conventions was in KB but not wired ─────────────────────
    "conventions": {"label": "Test naming conventions",      "keys": ["test_conventions"]},
}

# Which sections each agent actually needs (RAG selection per agent)
# test_writer  → gets errors + validation + testdata + conventions (writes exact steps)
# scenarios    → gets validation + testdata + conventions (writes scenario hints)
# strategist   → gets errors + known + extra (plans coverage categories)
# bug_analyst  → gets errors + navigation (writes repro steps)
# self_critique→ gets validation (checks boundary coverage gaps)
AGENT_KB_NEEDS = {
    "strategist":    ["app", "auth", "known", "extra", "errors"],
    "scenarios":     ["app", "auth", "navigation", "fields", "known", "validation", "testdata", "conventions"],
    "test_writer":   ["app", "auth", "navigation", "fields", "errors", "validation", "testdata", "conventions"],
    "bug_analyst":   ["app", "auth", "navigation", "errors"],
    "self_critique": ["app", "validation", "conventions"],
}


def load_knowledge_base(filepath: str = None) -> dict:
    fp = filepath or KNOWLEDGE_BASE_FILE
    if not os.path.isfile(fp):
        log.warning("  Knowledge base not found: " + fp)
        return {}

    with open(fp, encoding="utf-8") as f:
        kb = json.load(f)

    log.info("  Knowledge base loaded: " + fp)
    builders = kb.get("builders", {})
    if builders:
        for name, info in builders.items():
            log.info("    Builder [" + name + "]: " + info.get("url", "no url"))
    else:
        log.info("    App URL: " + kb.get("app_url", "not set"))
    return kb


def get_builder_url(kb: dict, builder: str) -> str:
    builders = kb.get("builders", {})
    if builders and builder in builders:
        return builders[builder].get("url", kb.get("app_url", "https://app.example.com"))
    return kb.get("app_url", "https://app.example.com")


def get_builder_login_flow(kb: dict, builder: str) -> str:
    login_flows = kb.get("login_flow", {})
    if isinstance(login_flows, dict) and builder in login_flows:
        return login_flows[builder]
    nav = kb.get("navigation", {})
    return nav.get("login", "Open app URL → Enter phone → Click Send OTP → Enter OTP → Click Verify OTP")


# ── IMPROVEMENT 5: RAG section retrieval ──────────────────────────────────
def retrieve_kb_sections(kb: dict, agent_role: str, builder: str = "form_builder") -> list[str]:
    """
    Returns only the KB chunk strings relevant to the given agent_role.
    Replaces the old build_knowledge_context() monolithic dump.
    Each agent gets a focused context — no irrelevant noise.
    """
    if not kb:
        return ["(No project knowledge base — generating generic test cases)"]

    needed_sections = AGENT_KB_NEEDS.get(agent_role, list(KB_SECTION_MAP.keys()))
    chunks = []

    for section_key in needed_sections:
        section_meta = KB_SECTION_MAP.get(section_key, {})
        kb_keys      = section_meta.get("keys", [])
        label        = section_meta.get("label", section_key)

        chunk_lines = ["--- " + label + " ---"]
        found_any   = False

        for k in kb_keys:
            val = kb.get(k)
            if val is None:
                continue
            found_any = True

            if k == "builders":
                # Only emit the builder the story is using
                if isinstance(val, dict) and builder in val:
                    chunk_lines.append(
                        "Builder [" + builder + "]: " + val[builder].get("url", "")
                    )
            elif k == "login_flow":
                if isinstance(val, dict) and builder in val:
                    chunk_lines.append("Login flow (" + builder + "): " + val[builder])
                elif isinstance(val, str):
                    chunk_lines.append("Login flow: " + val)
            elif k == "authentication":
                auth = val
                chunk_lines.append("Test phone: " + auth.get("test_phone", "+91 9876543210"))
                chunk_lines.append("Test OTP:   " + auth.get("test_otp",   "123456"))
                chunk_lines.append("Method: Phone + OTP only (no email, no password)")
            elif k in ("test_phone", "test_otp", "app_name", "app_url", "tech_stack", "extra_context"):
                chunk_lines.append(k + ": " + str(val))
            elif k == "navigation":
                if isinstance(val, dict):
                    for nav_k, nav_v in val.items():
                        chunk_lines.append("  " + nav_k + ": " + nav_v)
            elif k == "key_fields":
                if isinstance(val, dict):
                    for fk, fv in val.items():
                        chunk_lines.append("  " + fk + ": " + fv)
            elif k == "known_issues":
                if isinstance(val, list):
                    for issue in val:
                        chunk_lines.append("  - " + issue)

            # ── NEW: error_messages ────────────────────────────────────────
            elif k == "error_messages":
                if isinstance(val, dict):
                    for category, msgs in val.items():
                        chunk_lines.append("  [" + category + "]")
                        if isinstance(msgs, dict):
                            for msg_key, msg_text in msgs.items():
                                chunk_lines.append("    " + msg_key + ': "' + msg_text + '"')

            # ── NEW: validation_rules ──────────────────────────────────────
            elif k == "validation_rules":
                if isinstance(val, dict):
                    for field_name, rules in val.items():
                        chunk_lines.append("  [" + field_name + "]")
                        if isinstance(rules, dict):
                            for rule_k, rule_v in rules.items():
                                chunk_lines.append("    " + rule_k + ": " + str(rule_v))

            # ── NEW: test_data ─────────────────────────────────────────────
            elif k == "test_data":
                if isinstance(val, dict):
                    for data_group, items in val.items():
                        chunk_lines.append("  [" + data_group + "]")
                        if isinstance(items, dict):
                            for item_k, item_v in items.items():
                                chunk_lines.append("    " + item_k + ": " + str(item_v))

            # ── FIX: test_conventions ──────────────────────────────────────
            elif k == "test_conventions":
                if isinstance(val, dict):
                    for conv_k, conv_v in val.items():
                        if isinstance(conv_v, list):
                            chunk_lines.append("  " + conv_k + ": " + ", ".join(str(i) for i in conv_v))
                        else:
                            chunk_lines.append("  " + conv_k + ": " + str(conv_v))

        if found_any:
            chunks.append("\n".join(chunk_lines))

    return chunks


def build_knowledge_context(kb: dict, builder: str = "form_builder", agent_role: str = "test_writer") -> str:
    """
    IMPROVEMENT 5: Returns only relevant KB sections for the given agent_role.
    Previously returned the entire KB as one big string for every agent.
    """
    if not kb:
        return "(No project knowledge base — generating generic test cases)"

    sections = retrieve_kb_sections(kb, agent_role, builder)
    return "=== PROJECT KNOWLEDGE (relevant sections) ===\n" + "\n\n".join(sections) + "\n=== END ==="


# =============================================================================
# SECTION 4: HELPER UTILITIES
# =============================================================================

def _org_url():
    o = AZURE_ORG.strip()
    return o if o.startswith("http") else "https://dev.azure.com/" + o


def _parse_raw_text(tool_name, text):
    result = {"raw": text}
    m = (re.search(r"(?i)\bid\s*[:\-]?\s*(\d{4,8})", text) or
         re.search(r"#(\d{4,8})", text) or
         re.search(r"\b(\d{4,8})\b", text))
    if m:
        result["id"] = int(m.group(1))
    log.info("      Parsed from text -> id: " + str(result.get("id", "NOT FOUND")))
    return result


def strip_html(html):
    if not html:
        return ""
    html = re.sub(r"<br\s*/?>",          "\n", html, flags=re.IGNORECASE)
    html = re.sub(r"</p>|</div>|</li>", "\n", html, flags=re.IGNORECASE)
    html = re.sub(r"<[^>]+>",            "",  html)
    html = (html.replace("&nbsp;", " ").replace("&lt;",  "<")
                .replace("&gt;",   ">").replace("&amp;", "&")
                .replace("&quot;", '"').replace("&#39;", "'"))
    return re.sub(r"\n{3,}", "\n\n", html).strip()


def escape_xml(text: str) -> str:
    return (str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&apos;"))


# =============================================================================
# SECTION 5: ADO MCP CLIENT
# =============================================================================

class AdoMcpClient:
    def __init__(self):
        self.process    = None
        self._req_id    = 0
        self._lock      = threading.Lock()
        self._responses = {}

    def start(self):
        log.info("  Starting ADO MCP server ...")
        env = os.environ.copy()
        env["AZURE_DEVOPS_EXT_PAT"]    = AZURE_PAT
        env["AZURE_DEVOPS_AUTH_TOKEN"] = AZURE_PAT
        env["AZURE_DEVOPS_PAT"]        = AZURE_PAT

        is_windows = sys.platform == "win32"
        cmd = [
            "npx.cmd" if is_windows else "npx",
            "-y", "@azure-devops/mcp", AZURE_ORG,
            "-d", "work-items", "test-plans", "core",
        ]
        log.info("      CMD: " + " ".join(cmd))
        self.process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, text=True, bufsize=1, shell=False,
        )
        threading.Thread(target=self._read_responses, daemon=True).start()
        threading.Thread(target=self._read_stderr,    daemon=True).start()
        time.sleep(2.0)

        if self.process.poll() is not None:
            raise RuntimeError("ADO MCP process exited immediately.")
        self._initialize()
        log.info("  ADO MCP server ready.")

    def stop(self):
        if self.process:
            self.process.terminate()
            log.info("  ADO MCP server stopped.")

    def _next_id(self):
        with self._lock:
            self._req_id += 1
            return self._req_id

    def _send(self, method, params=None):
        req_id = self._next_id()
        msg = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}
        self._responses[req_id] = None
        self.process.stdin.write(json.dumps(msg) + "\n")
        self.process.stdin.flush()

        deadline = time.time() + 60
        while time.time() < deadline:
            if self._responses.get(req_id) is not None:
                resp = self._responses.pop(req_id)
                if "error" in resp:
                    raise RuntimeError("MCP error: " + str(resp["error"]))
                return resp.get("result", {})
            time.sleep(0.05)
        raise TimeoutError("MCP timeout waiting for: " + method)

    def _read_stderr(self):
        for line in self.process.stderr:
            line = line.strip()
            if line:
                log.info("      [MCP] " + line)

    def _read_responses(self):
        for line in self.process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                data   = json.loads(line)
                req_id = data.get("id")
                if req_id is not None:
                    self._responses[req_id] = data
            except json.JSONDecodeError:
                pass

    def _initialize(self):
        self._send("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities":    {},
            "clientInfo":      {"name": "ado-test-plan-gen", "version": "2.0"},
        })
        notif = {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
        self.process.stdin.write(json.dumps(notif) + "\n")
        self.process.stdin.flush()
        time.sleep(1.0)

    def list_tools(self):
        return self._send("tools/list").get("tools", [])

    def call_tool(self, tool_name, arguments):
        log.info("      MCP tool: " + tool_name)
        result = self._send("tools/call", {"name": tool_name, "arguments": arguments})

        if result.get("isError"):
            content  = result.get("content", [])
            err_text = next((i["text"] for i in content if i.get("type") == "text"), str(result))
            raise RuntimeError("MCP tool error [" + tool_name + "]: " + err_text)

        content = result.get("content", [])
        if content:
            for item in content:
                if item.get("type") == "text":
                    text = item["text"].strip()
                    if not text:
                        continue
                    log.info("      RAW TEXT: " + text[:500])
                    try:
                        return json.loads(text)
                    except json.JSONDecodeError:
                        return _parse_raw_text(tool_name, text)
            return {"content": content}
        return result


mcp = AdoMcpClient()

# =============================================================================
# SECTION 6: AZURE OPENAI CLIENT
# =============================================================================

client = AzureOpenAI(
    azure_endpoint=AZURE_OPENAI_ENDPOINT,
    api_key=AZURE_OPENAI_API_KEY,
    api_version=AZURE_OPENAI_API_VERSION,
)


def call_openai_json(system_prompt, user_prompt, label):
    log.info("      [" + label + "] calling " + AZURE_OPENAI_DEPLOYMENT + " ...")
    resp = client.chat.completions.create(
        model=AZURE_OPENAI_DEPLOYMENT,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ],
        temperature=0.3,
        response_format={"type": "json_object"},
    )
    return json.loads(resp.choices[0].message.content.strip())


# =============================================================================
# SECTION 7: FETCH USER STORY FROM AZURE DEVOPS
# =============================================================================

def fetch_user_story(story_id):
    log.info("  Fetching US-" + story_id + " via ADO MCP ...")
    result = mcp.call_tool("wit_get_work_item", {
        "organizationUrl": _org_url(),
        "project":         AZURE_PROJECT,
        "id":              int(story_id),
    })
    fields = result.get("fields", result)

    title          = fields.get("System.Title", "User Story " + story_id)
    reqs           = (fields.get("Microsoft.VSTS.Common.AcceptanceCriteria", "") or
                      fields.get("System.Description", ""))
    iteration_path = fields.get("System.IterationPath", AZURE_PROJECT)
    area_path      = fields.get("System.AreaPath",      AZURE_PROJECT)

    log.info("  Fetched: "   + title[:70])
    log.info("  Iteration: " + iteration_path)
    return {
        "id":             story_id,
        "title":          title,
        "requirements":   strip_html(reqs),
        "iteration_path": iteration_path,
        "area_path":      area_path,
    }


# =============================================================================
# SECTION 8: VIDEO FRAME EXTRACTION
# =============================================================================

def extract_frames_as_base64(video_path, frame_count=None, quality=None):
    frame_count = frame_count or VIDEO_FRAME_COUNT
    quality     = quality     or VIDEO_FRAME_QUALITY

    if not os.path.isfile(video_path):
        raise FileNotFoundError("Video not found: " + video_path)

    log.info("  Extracting " + str(frame_count) + " frames from: " + video_path)
    cap   = cv2.VideoCapture(video_path)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps   = cap.get(cv2.CAP_PROP_FPS)
    log.info("      " + str(total) + " frames | " + str(round(fps, 1)) + " fps")

    if total == 0:
        raise ValueError("Cannot read frames from: " + video_path)

    indices    = [int(i * total / frame_count) for i in range(frame_count)]
    frames_b64 = []
    for idx in indices:
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ret, frame = cap.read()
        if not ret:
            continue
        _, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
        frames_b64.append(base64.b64encode(buf).decode("utf-8"))

    cap.release()
    log.info("      " + str(len(frames_b64)) + " frames extracted")
    return frames_b64


# =============================================================================
# SECTION 9: BUG ANALYST AGENT (IMPROVEMENT 4 — TWO-PASS)
#
# IMPROVEMENT 4:
#   Original: All frames sent in one call → model guesses context from images
#   New:
#     Pass 1 — describe each frame individually (what is visible/what's wrong)
#     Pass 2 — synthesize descriptions into structured bug report JSON
#   Falls back to one-shot if frame count <= TWO_PASS_BUG_MIN_FRAMES
# =============================================================================

def _bug_analyst_one_shot(story, frames_b64, kb_ctx, app_url, phone, otp):
    """Original one-shot approach — used as fallback for short frame sets."""
    sid     = story["id"]
    title   = story["title"]
    reqs    = story.get("requirements", "").strip()
    builder = story.get("builder", "form_builder")

    json_fmt = (
        '{\n'
        '  "bug_title": "Concise, specific bug title",\n'
        '  "severity":  "Critical|High|Medium|Low",\n'
        '  "priority":  "1|2|3|4",\n'
        '  "steps_to_reproduce": [\n'
        '    "Step 1: Open ' + app_url + '",\n'
        '    "Step 2: Enter phone number ' + phone + ' → Click Send OTP → Enter OTP ' + otp + ' → Click Verify OTP",\n'
        '    "Step 3: Navigate to the affected area",\n'
        '    "Step 4: Perform the action that triggers the bug"\n'
        '  ],\n'
        '  "expected_behaviour": "What should happen",\n'
        '  "actual_behaviour":   "What actually happens (the bug)",\n'
        '  "additional_info":    "Browser, environment, frequency"\n'
        '}'
    )

    content = [{"type": "text", "text": (
        "You are a senior QA engineer analysing a bug recording.\n\n"
        + kb_ctx + "\n\n"
        "User Story: US-" + sid + " — " + title + "\n"
        "Acceptance Criteria:\n" + (reqs or "(none)") + "\n\n"
        "The following " + str(len(frames_b64)) + " frames are from the bug recording.\n"
        "Builder: " + builder + " | App URL: " + app_url + "\n"
        "IMPORTANT: Steps must start with the login flow.\n"
        "Return ONLY this JSON:\n" + json_fmt
    )}]

    for b64 in frames_b64:
        content.append({
            "type":      "image_url",
            "image_url": {"url": "data:image/jpeg;base64," + b64, "detail": "low"},
        })

    resp = client.chat.completions.create(
        model=AZURE_OPENAI_DEPLOYMENT,
        messages=[{"role": "user", "content": content}],
        temperature=0.2,
        response_format={"type": "json_object"},
        max_tokens=2000,
    )
    return json.loads(resp.choices[0].message.content.strip())


def _bug_analyst_pass1_describe(frames_b64):
    """
    IMPROVEMENT 4 — Pass 1:
    Describe each frame individually to capture what is visible and what looks wrong.
    Returns a list of frame descriptions as a single combined string.
    """
    log.info("      [Bug Pass 1] Describing " + str(len(frames_b64)) + " frames individually ...")

    descriptions = []
    for i, b64 in enumerate(frames_b64):
        content = [
            {
                "type": "text",
                "text": (
                    "You are a QA engineer reviewing a screenshot from a bug recording.\n"
                    "This is frame " + str(i + 1) + " of " + str(len(frames_b64)) + ".\n"
                    "Describe EXACTLY what you see:\n"
                    "  1. What UI elements are visible (buttons, fields, modals, errors)\n"
                    "  2. What state the application appears to be in\n"
                    "  3. Anything that looks incorrect, broken, or unexpected\n"
                    "Be specific. Use short sentences. Max 5 sentences.\n"
                    "Return ONLY JSON: {\"frame\": " + str(i + 1) + ", \"description\": \"...\"}"
                )
            },
            {
                "type":      "image_url",
                "image_url": {"url": "data:image/jpeg;base64," + b64, "detail": "low"},
            }
        ]
        try:
            resp = client.chat.completions.create(
                model=AZURE_OPENAI_DEPLOYMENT,
                messages=[{"role": "user", "content": content}],
                temperature=0.1,
                response_format={"type": "json_object"},
                max_tokens=300,
            )
            parsed = json.loads(resp.choices[0].message.content.strip())
            desc   = parsed.get("description", "No description")
            descriptions.append("Frame " + str(i + 1) + ": " + desc)
            log.info("        Frame " + str(i + 1) + " described")
        except Exception as e:
            log.warning("        Frame " + str(i + 1) + " description failed: " + str(e))
            descriptions.append("Frame " + str(i + 1) + ": (description unavailable)")

    return "\n".join(descriptions)


def _bug_analyst_pass2_synthesize(story, frame_descriptions, kb_ctx, app_url, phone, otp):
    """
    IMPROVEMENT 4 — Pass 2:
    Synthesize all frame descriptions into a structured bug report.
    No images in this call — only the text descriptions from Pass 1.
    """
    sid     = story["id"]
    title   = story["title"]
    reqs    = story.get("requirements", "").strip()
    builder = story.get("builder", "form_builder")

    log.info("      [Bug Pass 2] Synthesizing bug report from frame descriptions ...")

    system = (
        "You are a senior QA engineer writing a formal bug report.\n"
        + kb_ctx + "\n\n"
        "User Story: US-" + sid + " — " + title + "\n"
        "Acceptance Criteria:\n" + (reqs or "(none)") + "\n\n"
        "Below are per-frame observations from a bug recording.\n"
        "Synthesize these into a precise, actionable bug report.\n"
        "Steps to reproduce MUST start with the login flow:\n"
        "  Step 1: Open " + app_url + "\n"
        "  Step 2: Enter phone " + phone + " → Send OTP → Enter " + otp + " → Verify OTP\n"
        "  Then describe the specific steps that trigger the bug.\n"
        "Return ONLY valid JSON."
    )

    user = (
        "Frame-by-frame observations:\n" + frame_descriptions + "\n\n"
        "Builder: " + builder + " | URL: " + app_url + "\n\n"
        "Return this JSON:\n"
        '{\n'
        '  "bug_title": "Concise, specific bug title (not generic)",\n'
        '  "severity":  "Critical|High|Medium|Low",\n'
        '  "priority":  "1|2|3|4",\n'
        '  "steps_to_reproduce": ["Step 1: Open ' + app_url + '", "Step 2: ...", "..."],\n'
        '  "expected_behaviour": "What should happen according to acceptance criteria",\n'
        '  "actual_behaviour":   "What actually happens — the specific defect observed",\n'
        '  "additional_info":    "Which frames show the issue, frequency, environment"\n'
        '}'
    )

    return call_openai_json(system, user, "Bug-Synthesize")


def agent_bug_analyst(story, frames_b64):
    """
    IMPROVEMENT 4: Two-pass bug analysis.
    Pass 1 → per-frame descriptions (vision calls)
    Pass 2 → structured bug report (text-only synthesis)
    Falls back to one-shot for very short frame sets.
    """
    sid     = story["id"]
    builder = story.get("builder", "form_builder")
    kb      = PROJECT_KNOWLEDGE
    kb_ctx  = build_knowledge_context(kb, builder, agent_role="bug_analyst")
    app_url = get_builder_url(kb, builder)
    phone   = kb.get("test_phone") or kb.get("authentication", {}).get("test_phone", "+91 9876543210")
    otp     = kb.get("test_otp")   or kb.get("authentication", {}).get("test_otp",   "123456")

    log.info("  [Bug Analyst] US-" + sid + " | " + str(len(frames_b64)) + " frames | builder: " + builder)

    if len(frames_b64) <= TWO_PASS_BUG_MIN_FRAMES:
        log.info("      Frame count <= " + str(TWO_PASS_BUG_MIN_FRAMES) + " → using one-shot mode")
        result = _bug_analyst_one_shot(story, frames_b64, kb_ctx, app_url, phone, otp)
    else:
        log.info("      Frame count > " + str(TWO_PASS_BUG_MIN_FRAMES) + " → using two-pass mode")
        frame_descriptions = _bug_analyst_pass1_describe(frames_b64)
        result = _bug_analyst_pass2_synthesize(story, frame_descriptions, kb_ctx, app_url, phone, otp)

    log.info("      Bug: "      + result.get("bug_title", "")[:70])
    log.info("      Severity: " + result.get("severity",  ""))
    log.info("      Priority: " + result.get("priority",  ""))
    return result


# =============================================================================
# SECTION 10: CREATE BUG IN ADO
# =============================================================================

def create_bug(bug_report, story_id):
    log.info("  [MCP] Creating Bug: " + bug_report["bug_title"][:60])

    steps_html = "".join("<li>" + s + "</li>" for s in bug_report.get("steps_to_reproduce", []))
    desc_html  = (
        "<h3>Steps to Reproduce</h3><ol>" + steps_html + "</ol>"
        "<h3>Expected Behaviour</h3><p>" + bug_report.get("expected_behaviour", "") + "</p>"
        "<h3>Actual Behaviour</h3><p>"   + bug_report.get("actual_behaviour",   "") + "</p>"
        "<h3>Additional Info</h3><p>"    + bug_report.get("additional_info",    "") + "</p>"
        "<p><i>Auto-generated by TestForge. Linked to US-" + story_id + ".</i></p>"
    )

    smap = {"critical": "1 - Critical", "high": "2 - High",
            "medium":   "3 - Medium",   "low":  "4 - Low"}
    severity_val = smap.get(bug_report.get("severity", "medium").lower(), "3 - Medium")
    priority_val = int(bug_report.get("priority", 2))

    patch = [
        {"name": "System.Title",                   "value": bug_report["bug_title"]},
        {"name": "System.Description",             "value": desc_html},
        {"name": "Microsoft.VSTS.TCM.ReproSteps",  "value": "<ol>" + steps_html + "</ol>"},
        {"name": "Microsoft.VSTS.Common.Severity", "value": severity_val},
        {"name": "Microsoft.VSTS.Common.Priority", "value": str(priority_val)},
    ]
    if AREA_PATH:      patch.append({"name": "System.AreaPath",      "value": AREA_PATH})
    if ITERATION_PATH: patch.append({"name": "System.IterationPath", "value": ITERATION_PATH})

    result = mcp.call_tool("wit_create_work_item", {
        "organizationUrl": _org_url(),
        "project":         AZURE_PROJECT,
        "workItemType":    "Bug",
        "fields":          patch,
    })

    bug_id  = str(result.get("id", ""))
    bug_url = (result.get("_links", {}).get("html", {}).get("href") or
               ("https://dev.azure.com/" + AZURE_ORG + "/" + AZURE_PROJECT +
                "/_workitems/edit/" + bug_id if bug_id else ""))

    if bug_id:
        try:
            mcp.call_tool("wit_work_items_link", {
                "organizationUrl": _org_url(),
                "project":         AZURE_PROJECT,
                "updates": [{
                    "id":       int(bug_id),
                    "linkToId": int(story_id),
                    "linkType": "System.LinkTypes.Hierarchy-Reverse",
                    "comment":  "Auto-linked by TestForge Bug Analyser",
                }],
            })
        except Exception as e:
            log.warning("      Link bug->story failed (non-fatal): " + str(e))

    log.info("      Bug ID: " + bug_id)
    log.info("      URL:    " + bug_url)
    return {"bug_id": bug_id, "bug_url": bug_url}


# =============================================================================
# SECTION 11: AGENT 1 — TEST STRATEGIST (IMPROVEMENT 1 — RETRY LOOP)
#
# IMPROVEMENT 1:
#   Original: One call — if < 8 categories returned, silently continues
#   New:
#     - After each call, validate len(categories) >= MIN_STRATEGY_CATEGORIES
#     - If not, re-prompt with an explicit correction message (up to MAX_STRATEGY_RETRIES)
#     - Logs a warning if retries exhausted but continues
#     - Each retry adds the previous attempt's category count to the prompt
#       so GPT-4o understands what it produced vs what was needed
# =============================================================================

def agent_test_strategist(story):
    sid     = story["id"]
    title   = story["title"]
    reqs    = story.get("requirements", "").strip()
    builder = story.get("builder", "form_builder")
    kb_ctx  = build_knowledge_context(PROJECT_KNOWLEDGE, builder, agent_role="strategist")

    log.info("  [Agent 1/3] Test Strategist - US-" + sid + " [" + builder + "]")

    system = (
        "You are a senior QA Test Strategist with deep knowledge of the application.\n\n"
        + kb_ctx + "\n\n"
        "Your job: Generate COMPREHENSIVE test coverage with AT LEAST " + str(MIN_STRATEGY_CATEGORIES) + " categories.\n"
        "The application being tested is: " + builder.replace("_", " ").title() + "\n\n"
        "REQUIRED CATEGORIES — you must include ALL of these:\n"
        "  1. Functional (core happy path flows)\n"
        "  2. Negative / Error handling (invalid inputs, error states)\n"
        "  3. UI/UX (visual elements, button states, field labels, layout)\n"
        "  4. Boundary (min/max values, empty fields, special characters)\n"
        "  5. Integration (data flow between Form Builder and Dashboard Builder)\n"
        "  6. Regression (known bugs, previously fixed issues)\n"
        "  7. Session / Authentication (session persistence, logout, token expiry)\n"
        "  8. Performance / Responsiveness (page load, large data sets)\n"
        "You may add more specific categories based on the user story requirements.\n"
        "Each category: 3-5 positive test cases, 2-3 negative test cases.\n"
        "Return ONLY valid JSON."
    )

    base_user = (
        "User Story: US-" + sid + "\n"
        "Title: " + title + "\n"
        "Builder: " + builder + "\n"
        "Acceptance Criteria:\n" + (reqs or "(none — infer from title and project context)") + "\n\n"
        "Generate at least " + str(MIN_STRATEGY_CATEGORIES) + " categories. Aim for 35-50 total test cases.\n\n"
        'Return:\n'
        '{"strategy_summary":"...","categories":['
        '{"name":"...","description":"...","positive_count":4,"negative_count":2}'
        '],"total_positive":35,"total_negative":15}'
    )

    result          = None
    last_cat_count  = 0

    # ── IMPROVEMENT 1: Retry loop ──────────────────────────────────────────
    for attempt in range(1, MAX_STRATEGY_RETRIES + 1):
        if attempt == 1:
            user_prompt = base_user
        else:
            # Correction prompt: tell GPT-4o exactly how many it produced and how many are needed
            user_prompt = (
                base_user + "\n\n"
                "CORRECTION (attempt " + str(attempt) + "/" + str(MAX_STRATEGY_RETRIES) + "):\n"
                "Your previous response returned only " + str(last_cat_count) + " categories.\n"
                "You MUST return at least " + str(MIN_STRATEGY_CATEGORIES) + " categories.\n"
                "Add the missing categories now. Do not repeat categories already generated.\n"
                "Missing required categories (if not yet included):\n"
                "  - Functional, Negative/Error, UI/UX, Boundary,\n"
                "    Integration, Regression, Session/Auth, Performance\n"
            )

        log.info("      Strategy attempt " + str(attempt) + "/" + str(MAX_STRATEGY_RETRIES))
        result = call_openai_json(system, user_prompt, "Test-Strategist-Attempt-" + str(attempt))

        last_cat_count = len(result.get("categories", []))
        log.info("      Categories returned: " + str(last_cat_count))

        if last_cat_count >= MIN_STRATEGY_CATEGORIES:
            log.info("      Validation passed on attempt " + str(attempt))
            break
        elif attempt < MAX_STRATEGY_RETRIES:
            log.warning("      Only " + str(last_cat_count) + "/" + str(MIN_STRATEGY_CATEGORIES)
                        + " categories — retrying ...")
        else:
            log.warning("      Retry limit reached. Proceeding with " + str(last_cat_count)
                        + " categories (below minimum of " + str(MIN_STRATEGY_CATEGORIES) + ")")

    log.info("      Final: " + str(len(result.get("categories", []))) + " categories | "
             + str(result.get("total_positive", 0)) + " pos + "
             + str(result.get("total_negative", 0)) + " neg planned")
    return result


# =============================================================================
# SECTION 12: AGENT 2 — SCENARIO GENERATOR
# =============================================================================

def agent_scenario_generator(story, strategy):
    sid     = story["id"]
    title   = story["title"]
    reqs    = story.get("requirements", "").strip()
    builder = story.get("builder", "form_builder")
    kb_ctx  = build_knowledge_context(PROJECT_KNOWLEDGE, builder, agent_role="scenarios")

    log.info("  [Agent 2/3] Scenario Generator - US-" + sid)

    cats = "\n".join(
        "- " + c["name"] + " (" + str(c["positive_count"]) + " pos, "
        + str(c["negative_count"]) + " neg): " + c["description"]
        for c in strategy.get("categories", [])
    )
    total_pos = strategy.get("total_positive", 35)
    total_neg = strategy.get("total_negative", 15)

    system = (
        "You are a QA Scenario Generator with deep knowledge of the application.\n\n"
        + kb_ctx + "\n\n"
        "Generate DETAILED and SPECIFIC scenarios using the project knowledge above.\n"
        "Builder being tested: " + builder.replace("_", " ").title() + "\n"
        "Reference real navigation paths, actual field names, and real URLs.\n"
        "Each scenario must be distinct and testable — no vague or duplicate scenarios.\n"
        "IMPORTANT: Each scenario title should clearly indicate the specific feature/field/action being tested.\n"
        "Each scenario hint should be concrete — mention specific field names, values, or conditions.\n"
        "Hint length is important: complex scenarios need longer hints (2-3 sentences).\n"
        "Return ONLY valid JSON."
    )
    user = (
        "Story: US-" + sid + " — " + title + "\n"
        "Builder: " + builder + "\n"
        "Requirements: " + (reqs or "(none)") + "\n\n"
        "Coverage categories:\n" + cats + "\n\n"
        "Generate EXACTLY the number specified per category.\n"
        "Target: " + str(total_pos) + " positive + " + str(total_neg) + " negative.\n\n"
        'Return:\n'
        '{"positive":[{"title":"...","category":"...","hint":"specific 1-3 sentence hint about what to test"}],'
        '"negative":[{"title":"...","category":"...","hint":"specific 1-3 sentence hint about what to test"}]}'
    )
    result = call_openai_json(system, user, "Scenario Generator")
    log.info("      " + str(len(result.get("positive", []))) + " pos / "
             + str(len(result.get("negative", []))) + " neg scenarios")
    return result


# =============================================================================
# SECTION 13: DYNAMIC BATCH SIZE CALCULATOR (IMPROVEMENT 2)
#
# IMPROVEMENT 2:
#   Original: Fixed batch size of 12 for every Agent 3 call
#   New:
#     - Measure average hint length across all scenarios
#     - Short hints (< 60 chars avg) → batch size 15 (fewer calls)
#     - Medium hints (60-120 chars avg) → batch size 10
#     - Long hints (> 120 chars avg) → batch size 6 (better quality focus)
#     - Logs the chosen batch size and the avg hint length for transparency
# =============================================================================

def calculate_dynamic_batch_size(scenarios: dict) -> int:
    """
    IMPROVEMENT 2: Choose batch size based on average hint complexity.
    Longer hints = more complex scenarios = smaller batch to preserve quality.
    """
    all_scenarios = scenarios.get("positive", []) + scenarios.get("negative", [])
    if not all_scenarios:
        return 12

    total_hint_len = sum(len(s.get("hint", "")) for s in all_scenarios)
    avg_hint_len   = total_hint_len / len(all_scenarios)

    if avg_hint_len < 60:
        batch_size = 15
        complexity = "simple"
    elif avg_hint_len < 120:
        batch_size = 10
        complexity = "medium"
    else:
        batch_size = 6
        complexity = "complex"

    log.info("      Dynamic batch size: " + str(batch_size)
             + " (avg hint length: " + str(round(avg_hint_len)) + " chars | complexity: " + complexity + ")")
    return batch_size


# =============================================================================
# SECTION 14: AGENT 3 — TEST CASE WRITER (IMPROVEMENT 2 APPLIED)
# =============================================================================

def agent_test_case_generator(story, scenarios):
    sid     = story["id"]
    title   = story["title"]
    reqs    = story.get("requirements", "").strip()
    builder = story.get("builder", "form_builder")
    kb      = PROJECT_KNOWLEDGE
    kb_ctx  = build_knowledge_context(kb, builder, agent_role="test_writer")

    app_url    = get_builder_url(kb, builder)
    phone      = kb.get("test_phone") or kb.get("authentication", {}).get("test_phone", "+91 9876543210")
    otp        = kb.get("test_otp")   or kb.get("authentication", {}).get("test_otp",   "123456")

    log.info("  [Agent 3/3] Test Case Writer - US-" + sid + " [" + builder + "] URL: " + app_url)

    mandatory_login_steps = (
        "MANDATORY LOGIN STEPS — Every single test case (positive AND negative) "
        "MUST begin with EXACTLY these 5 steps before any other steps:\n"
        "  Step 1 | action: Open " + app_url + "\n"
        "          | expected_result: Application loads and phone number input field is visible on the login page\n"
        "  Step 2 | action: Wait until phone number input field is visible\n"
        "          | expected_result: Phone number input field is displayed with placeholder text\n"
        "  Step 3 | action: Enter phone number as " + phone + " in the phone number input field\n"
        "          | expected_result: Phone field accepts the 10-digit number and displays " + phone + "\n"
        "  Step 4 | action: Click Send OTP button\n"
        "          | expected_result: OTP is sent to " + phone + " and 6-digit OTP input field appears on screen\n"
        "  Step 5 | action: Enter OTP as " + otp + " in the OTP input field and click Verify OTP button\n"
        "          | expected_result: Login is successful and dashboard is visible with Form Builder and Dashboard Builder in the left sidebar\n"
        "Only AFTER these 5 login steps, write the actual scenario-specific steps.\n"
        "DO NOT skip, combine, or remove any of these 5 login steps.\n"
        "DO NOT use email or password — this app uses ONLY phone number + OTP.\n"
    )

    all_scenarios = (
        [{"type": "positive", **s} for s in scenarios.get("positive", [])] +
        [{"type": "negative", **s} for s in scenarios.get("negative", [])]
    )

    # ── IMPROVEMENT 2: Dynamic batch size ─────────────────────────────────
    BATCH_SIZE = calculate_dynamic_batch_size(scenarios)

    all_positive_tcs = []
    all_negative_tcs = []

    for batch_start in range(0, len(all_scenarios), BATCH_SIZE):
        batch         = all_scenarios[batch_start : batch_start + BATCH_SIZE]
        batch_num     = (batch_start // BATCH_SIZE) + 1
        total_batches = (len(all_scenarios) + BATCH_SIZE - 1) // BATCH_SIZE
        log.info("      Batch " + str(batch_num) + "/" + str(total_batches)
                 + " (" + str(len(batch)) + " scenarios, batch_size=" + str(BATCH_SIZE) + ")")

        pos_batch = [{"title": s["title"], "hint": s.get("hint", "")}
                     for s in batch if s["type"] == "positive"]
        neg_batch = [{"title": s["title"], "hint": s.get("hint", "")}
                     for s in batch if s["type"] == "negative"]

        system = (
            "You are a senior QA Test Case Writer for Azure DevOps.\n"
            "You write EXACT, RUNNABLE test cases using real application details.\n\n"
            + kb_ctx + "\n\n"
            + mandatory_login_steps + "\n\n"
            "STEP FORMAT — each test case has a 'steps' array where EVERY step object contains:\n"
            "  step:            (integer) step number starting at 1\n"
            "  action:          (string)  EXACT action — verb-first, specific, with real values\n"
            "  expected_result: (string)  what the user sees or system does AFTER this action\n\n"
            "ACTION VERB RULES:\n"
            "  Always start with: Open, Click, Enter, Select, Navigate, Wait until,\n"
            "  Verify, Scroll, Hover, Upload, Download, Assert, Check, Expand, Refresh\n\n"
            "EXPECTED RESULT RULES:\n"
            "  GOOD: 'Error message Invalid OTP is shown below the OTP field'\n"
            "  BAD:  'Error is shown'\n\n"
            "STEP COUNT: Minimum 8 (5 login + at least 3 scenario). Maximum 12.\n"
            "TITLE FORMAT: [US-" + sid + "] <Verb> <what is tested> <context>\n"
            "Builder: " + builder.replace("_", " ").title() + " | URL: " + app_url + "\n\n"
            "Return ONLY valid JSON — no markdown, no code blocks."
        )

        example = (
            '{\n'
            '  "positive": [{\n'
            '    "title": "[US-' + sid + '] Verify successful example positive test",\n'
            '    "steps": [\n'
            '      {"step":1,"action":"Open ' + app_url + '","expected_result":"Application loads and phone number field is visible"},\n'
            '      {"step":2,"action":"Wait until phone number input field is visible","expected_result":"Phone number input field is displayed"},\n'
            '      {"step":3,"action":"Enter phone number as ' + phone + ' in the phone number input field","expected_result":"Phone field accepts the number and displays ' + phone + '"},\n'
            '      {"step":4,"action":"Click Send OTP button","expected_result":"OTP is sent and 6-digit OTP input field appears"},\n'
            '      {"step":5,"action":"Enter OTP as ' + otp + ' and click Verify OTP button","expected_result":"Login is successful and dashboard is visible"},\n'
            '      {"step":6,"action":"Click specific feature in the left sidebar","expected_result":"Feature area is displayed correctly"},\n'
            '      {"step":7,"action":"Perform the specific action being tested","expected_result":"System responds as expected"},\n'
            '      {"step":8,"action":"Verify the final expected outcome is visible","expected_result":"Success state is confirmed on screen"}\n'
            '    ]\n'
            '  }],\n'
            '  "negative": [{\n'
            '    "title": "[US-' + sid + '] Validate that example negative condition shows error",\n'
            '    "steps": [\n'
            '      {"step":1,"action":"Open ' + app_url + '","expected_result":"Application loads and phone number field is visible"},\n'
            '      {"step":2,"action":"Wait until phone number input field is visible","expected_result":"Phone number input field is displayed"},\n'
            '      {"step":3,"action":"Enter phone number as ' + phone + ' in the phone number input field","expected_result":"Phone field accepts the number"},\n'
            '      {"step":4,"action":"Click Send OTP button","expected_result":"OTP is sent and OTP input field appears"},\n'
            '      {"step":5,"action":"Enter OTP as ' + otp + ' and click Verify OTP button","expected_result":"Login is successful and dashboard is visible"},\n'
            '      {"step":6,"action":"Navigate to the feature area being tested","expected_result":"Feature area is displayed"},\n'
            '      {"step":7,"action":"Attempt the invalid or boundary action","expected_result":"System blocks or shows validation message"},\n'
            '      {"step":8,"action":"Verify the error state or blocked outcome","expected_result":"Appropriate error message is confirmed"}\n'
            '    ]\n'
            '  }]\n'
            '}'
        )

        user = (
            "Story: US-" + sid + " — " + title + "\n"
            "Builder: " + builder + " — URL: " + app_url + "\n"
            "Requirements:\n" + (reqs or "(none)") + "\n\n"
            "REMINDER — Steps 1-5 MUST be the login flow in EVERY test case:\n"
            "  Step 1: Open " + app_url + "\n"
            "  Step 2: Wait until phone number field is visible\n"
            "  Step 3: Enter phone number as " + phone + "\n"
            "  Step 4: Click Send OTP → Enter OTP " + otp + " → Click Verify OTP\n"
            "  Step 5: Wait until dashboard is visible\n\n"
            "Positive scenarios:\n" + json.dumps(pos_batch, ensure_ascii=False) + "\n\n"
            "Negative scenarios:\n" + json.dumps(neg_batch, ensure_ascii=False) + "\n\n"
            "Write test cases for ALL scenarios. Every test case: 8-12 steps.\n"
            "Every step must have BOTH 'action' and 'expected_result'.\n\n"
            "Follow this EXACT JSON structure:\n" + example
        )

        result = call_openai_json(system, user, "TC-Batch-" + str(batch_num))
        all_positive_tcs.extend(result.get("positive", []))
        all_negative_tcs.extend(result.get("negative", []))

    log.info("      Total: " + str(len(all_positive_tcs)) + " pos / "
             + str(len(all_negative_tcs)) + " neg test cases")
    return {"positive": all_positive_tcs, "negative": all_negative_tcs}


# =============================================================================
# SECTION 15: AGENT 4 — SELF-CRITIQUE / QUALITY REVIEWER (IMPROVEMENT 3)
#
# IMPROVEMENT 3:
#   A dedicated review agent that runs after Agent 3 and before ADO creation.
#
#   What it does:
#     A) Duplicate detection:
#        - Groups test cases by their step-6 action text (first scenario step)
#        - If two TCs share the same opening action AND same category, flags as duplicate
#        - Removes duplicates from the final list (keeps first occurrence)
#
#     B) Coverage gap detection via LLM:
#        - Sends all TC titles to GPT-4o and asks it to identify:
#          * Categories that have zero negative test cases
#          * Acceptance criteria points with no coverage
#          * Obvious missing scenarios given the story title
#        - Returns a review_report dict with gap findings
#
#     C) Returns cleaned test_cases + review_report
#     D) If ENABLE_SELF_CRITIQUE=false, passes through unchanged
# =============================================================================

def _deduplicate_test_cases(test_cases: dict) -> tuple[dict, int]:
    """
    IMPROVEMENT 3A: Remove duplicate test cases.
    Two TCs are considered duplicates if their step-6 action text is identical.
    Step 6 is the first post-login step — it defines what the TC is actually testing.
    Returns (cleaned_test_cases, removed_count).
    """
    def get_step6_action(tc):
        steps = tc.get("steps", [])
        if len(steps) >= 6:
            return steps[5].get("action", "").strip().lower()
        return tc.get("title", "").strip().lower()

    cleaned = {}
    total_removed = 0

    for tc_type in ("positive", "negative"):
        seen_signatures = set()
        kept            = []

        for tc in test_cases.get(tc_type, []):
            sig = get_step6_action(tc)
            if sig and sig in seen_signatures:
                log.warning("        Duplicate removed: " + tc.get("title", "")[:60])
                total_removed += 1
            else:
                seen_signatures.add(sig)
                kept.append(tc)

        cleaned[tc_type] = kept

    return cleaned, total_removed


def _llm_coverage_gap_check(story, test_cases: dict, strategy: dict) -> dict:
    """
    IMPROVEMENT 3B: Ask GPT-4o to identify coverage gaps in the generated test cases.
    Returns a structured review_report.
    """
    sid     = story["id"]
    title   = story["title"]
    reqs    = story.get("requirements", "").strip()

    pos_titles = [tc.get("title", "") for tc in test_cases.get("positive", [])]
    neg_titles = [tc.get("title", "") for tc in test_cases.get("negative", [])]
    cat_names  = [c.get("name", "") for c in strategy.get("categories", [])]

    system = (
        "You are a QA Lead reviewing a generated test suite for completeness and quality.\n"
        "Your job is to identify gaps, missing coverage, and quality issues.\n"
        "Be specific — name the exact category or AC point that is missing.\n"
        "Return ONLY valid JSON."
    )

    user = (
        "User Story: US-" + sid + " — " + title + "\n"
        "Acceptance Criteria:\n" + (reqs or "(none)") + "\n\n"
        "Planned test categories: " + ", ".join(cat_names) + "\n\n"
        "Generated positive test titles:\n" + "\n".join("  - " + t for t in pos_titles) + "\n\n"
        "Generated negative test titles:\n" + "\n".join("  - " + t for t in neg_titles) + "\n\n"
        "Review this test suite and return:\n"
        '{\n'
        '  "quality_score": 0-100,\n'
        '  "categories_missing_negatives": ["Category name if it has 0 negative TCs", ...],\n'
        '  "uncovered_ac_points": ["AC point or requirement that has no test coverage", ...],\n'
        '  "duplicate_risks": ["Titles that seem to test the same thing", ...],\n'
        '  "missing_scenarios": ["Obvious test scenario that was not generated", ...],\n'
        '  "overall_assessment": "2-3 sentence summary of test suite quality"\n'
        '}'
    )

    return call_openai_json(system, user, "Self-Critique")


def agent_self_critique(story, test_cases: dict, strategy: dict) -> tuple[dict, dict]:
    """
    IMPROVEMENT 3: Full self-critique pass.
    Returns (cleaned_test_cases, review_report).
    If ENABLE_SELF_CRITIQUE=False, returns original test_cases unchanged.
    """
    if not ENABLE_SELF_CRITIQUE:
        log.info("  [Agent 4/4] Self-critique disabled (ENABLE_SELF_CRITIQUE=false)")
        return test_cases, {}

    sid = story["id"]
    log.info("  [Agent 4/4] Self-Critique - US-" + sid)

    # Step A: Structural deduplication (fast, no LLM call)
    cleaned, removed_count = _deduplicate_test_cases(test_cases)
    log.info("      Deduplication: removed " + str(removed_count) + " duplicate(s)")
    log.info("      Remaining: " + str(len(cleaned.get("positive", []))) + " pos / "
             + str(len(cleaned.get("negative", []))) + " neg")

    # Step B: LLM coverage gap check
    try:
        review_report = _llm_coverage_gap_check(story, cleaned, strategy)
        quality_score = review_report.get("quality_score", "N/A")
        log.info("      Quality score: " + str(quality_score) + "/100")

        gaps = review_report.get("categories_missing_negatives", [])
        if gaps:
            log.warning("      Categories missing negatives: " + ", ".join(gaps))

        missing = review_report.get("missing_scenarios", [])
        if missing:
            log.warning("      Missing scenarios: " + str(len(missing)) + " identified")

        uncovered = review_report.get("uncovered_ac_points", [])
        if uncovered:
            log.warning("      Uncovered AC points: " + str(len(uncovered)))

    except Exception as e:
        log.warning("      LLM coverage check failed (non-fatal): " + str(e))
        review_report = {"error": str(e)}

    return cleaned, review_report


# =============================================================================
# SECTION 16: FULL AGENT PIPELINE
# =============================================================================

def run_agent_pipeline(story):
    """Runs all 4 agents in sequence. Returns test cases dict + review report."""
    log.info("  4-agent pipeline - US-" + story["id"] + " [" + story.get("builder", "form_builder") + "]")

    strategy   = agent_test_strategist(story)    # Agent 1 (with retry)
    scenarios  = agent_scenario_generator(story, strategy)  # Agent 2
    test_cases = agent_test_case_generator(story, scenarios) # Agent 3 (dynamic batch)
    test_cases, review_report = agent_self_critique(story, test_cases, strategy)  # Agent 4

    if review_report:
        log.info("  Review report summary: " + review_report.get("overall_assessment", "")[:100])

    return test_cases, review_report


# =============================================================================
# SECTION 17: BUILD STEPS XML
# =============================================================================

def build_steps_xml(steps: list) -> str:
    total     = len(steps)
    last_id   = total + 1
    xml_parts = ['<steps id="0" last="' + str(last_id) + '">']

    for idx, step_obj in enumerate(steps):
        step_id = idx + 2
        if isinstance(step_obj, dict):
            action   = escape_xml(step_obj.get("action",          ""))
            expected = escape_xml(step_obj.get("expected_result", ""))
        else:
            action   = escape_xml(str(step_obj))
            expected = ""

        xml_parts.append(
            '<step id="' + str(step_id) + '" type="ActionStep">'
            + '<parameterizedString isformatted="true">' + action   + '</parameterizedString>'
            + '<parameterizedString isformatted="true">' + expected + '</parameterizedString>'
            + '<description/>'
            + '</step>'
        )

    xml_parts.append('</steps>')
    return "".join(xml_parts)


# =============================================================================
# SECTION 18: CREATE TEST PLAN IN ADO
# =============================================================================

def create_test_plan(story):
    sid  = story["id"]
    name = "US-" + sid + " | " + story["title"]
    log.info("  [MCP] Creating Test Plan: " + name[:70])

    iteration = (ITERATION_PATH if ITERATION_PATH
                 else story.get("iteration_path", "") or AZURE_PROJECT)

    args = {
        "organizationUrl": _org_url(),
        "project":         AZURE_PROJECT,
        "name":            name,
        "iteration":       iteration,
    }
    area = AREA_PATH if AREA_PATH else story.get("area_path", "")
    if area:
        args["areaPath"] = area

    result = mcp.call_tool("testplan_create_test_plan", args)
    plan_id       = result.get("id") or result.get("testPlan", {}).get("id")
    root_suite_id = result.get("rootSuite", {}).get("id")

    if plan_id and not root_suite_id:
        log.info("      Fetching suites for plan " + str(plan_id) + " ...")
        try:
            sr = mcp.call_tool("testplan_list_test_suites", {
                "organizationUrl": _org_url(),
                "project":         AZURE_PROJECT,
                "planId":          int(plan_id),
            })
            suites = sr.get("value", sr.get("testSuites", []))
            if isinstance(suites, list) and suites:
                root_suite_id = suites[0].get("id")
        except Exception as e:
            log.warning("      Could not fetch suites: " + str(e))

    log.info("      Plan: " + str(plan_id) + " | Root Suite: " + str(root_suite_id))
    if not plan_id:
        raise RuntimeError("testplan_create_test_plan returned no ID. RAW: " + str(result))

    return int(plan_id), int(root_suite_id) if root_suite_id else int(plan_id)


# =============================================================================
# SECTION 19: CREATE TEST SUITE IN ADO
# =============================================================================

def create_test_suite(plan_id, root_suite_id, story):
    story_id = story["id"]
    log.info("  [MCP] Creating Test Suite for US-" + story_id)

    try:
        result = mcp.call_tool("testplan_create_test_suite", {
            "organizationUrl": _org_url(),
            "project":         AZURE_PROJECT,
            "planId":          plan_id,
            "parentSuiteId":   root_suite_id,
            "suiteType":       "requirementTestSuite",
            "requirementId":   int(story_id),
            "name":            "US-" + story_id + " Test Scenarios",
        })
        suite_id = result.get("id") or result.get("testSuite", {}).get("id")
        if suite_id:
            log.info("      Suite (requirement-based): " + str(suite_id))
            return int(suite_id)
    except Exception as e:
        log.warning("      requirementTestSuite failed, trying staticTestSuite: " + str(e))

    result = mcp.call_tool("testplan_create_test_suite", {
        "organizationUrl": _org_url(),
        "project":         AZURE_PROJECT,
        "planId":          plan_id,
        "parentSuiteId":   root_suite_id,
        "suiteType":       "staticTestSuite",
        "name":            "US-" + story_id + " Test Scenarios",
    })
    suite_id = result.get("id") or result.get("testSuite", {}).get("id")
    if not suite_id:
        log.warning("      Suite creation returned no ID - using root suite")
        return root_suite_id

    log.info("      Suite (static): " + str(suite_id))
    return int(suite_id)


# =============================================================================
# SECTION 20: CREATE INDIVIDUAL TEST CASE
# =============================================================================

def create_test_case(plan_id, suite_id, scenario, story_id):
    steps     = scenario.get("steps", [])
    steps_xml = build_steps_xml(steps)

    tc_title = scenario.get("title", "")
    if not tc_title.startswith("[US-"):
        tc_title = "[US-" + story_id + "] " + tc_title

    log.info("        Creating TC: " + tc_title[:60] + " (" + str(len(steps)) + " steps)")

    fields = [
        {"name": "System.Title",               "value": tc_title},
        {"name": "Microsoft.VSTS.TCM.Steps",   "value": steps_xml},
        {"name": "System.AreaPath",            "value": AREA_PATH      or AZURE_PROJECT},
        {"name": "System.IterationPath",       "value": ITERATION_PATH or AZURE_PROJECT},
    ]

    result = mcp.call_tool("wit_create_work_item", {
        "organizationUrl": _org_url(),
        "project":         AZURE_PROJECT,
        "workItemType":    "Test Case",
        "fields":          fields,
    })

    tc_id = result.get("id")
    if not tc_id:
        log.warning("        wit_create_work_item returned no ID — trying testplan_create_test_case")
        fallback = mcp.call_tool("testplan_create_test_case", {
            "organizationUrl": _org_url(),
            "project":         AZURE_PROJECT,
            "planId":          plan_id,
            "suiteId":         suite_id,
            "title":           tc_title,
            "steps":           steps_xml,
        })
        tc_id = fallback.get("id") or fallback.get("testCase", {}).get("id")

    return tc_id


# =============================================================================
# SECTION 21: CREATE ALL TEST CASES + ADD TO SUITE
# =============================================================================

def create_all_test_cases(plan_id, suite_id, test_cases, story_id):
    log.info("  [MCP] Creating Test Cases ...")
    case_ids      = []
    all_scenarios = test_cases.get("positive", []) + test_cases.get("negative", [])
    total         = len(all_scenarios)

    for i, sc in enumerate(all_scenarios, 1):
        try:
            tc_id = create_test_case(plan_id, suite_id, sc, story_id)
            steps = sc.get("steps", [])
            log.info("      [" + str(i) + "/" + str(total) + "] TC-" + str(tc_id)
                     + " (" + str(len(steps)) + " steps) written OK")
            if tc_id:
                case_ids.append(int(tc_id))
        except Exception as e:
            log.warning("      Could not create TC '" + sc.get("title", "")[:40] + "': " + str(e))

    log.info("      " + str(len(case_ids)) + " test cases created")

    if case_ids:
        try:
            mcp.call_tool("testplan_add_test_cases_to_suite", {
                "organizationUrl": _org_url(),
                "project":         AZURE_PROJECT,
                "planId":          plan_id,
                "suiteId":         suite_id,
                "testCaseIds":     [str(i) for i in case_ids],
            })
            log.info("      All " + str(len(case_ids)) + " IDs added to suite " + str(suite_id))
        except Exception as e:
            log.warning("      Batch add failed, trying one-by-one: " + str(e))
            for tc_id in case_ids:
                try:
                    mcp.call_tool("testplan_add_test_cases_to_suite", {
                        "organizationUrl": _org_url(),
                        "project":         AZURE_PROJECT,
                        "planId":          plan_id,
                        "suiteId":         suite_id,
                        "testCaseIds":     [str(tc_id)],
                    })
                except Exception as e2:
                    log.warning("        Could not add TC-" + str(tc_id) + ": " + str(e2))

    return case_ids


# =============================================================================
# SECTION 22: LINK TEST PLAN TO USER STORY
# =============================================================================

def link_testplan_to_story(plan_id, story_id):
    try:
        log.info("  [MCP] Linking Plan " + str(plan_id) + " → US-" + story_id)
        mcp.call_tool("wit_work_items_link", {
            "organizationUrl": _org_url(),
            "project":         AZURE_PROJECT,
            "updates": [{
                "id":       plan_id,
                "linkToId": int(story_id),
                "linkType": "Microsoft.VSTS.Common.TestedBy-Forward",
                "comment":  "Auto-linked by TestForge",
            }],
        })
        log.info("      Linked")
    except Exception as e:
        log.warning("      Link failed (non-fatal): " + str(e))


# =============================================================================
# SECTION 23: INPUT FILE LOADERS
# =============================================================================

def load_ids_from_json(filepath):
    log.info("Loading story IDs from: " + filepath)
    with open(filepath, encoding="utf-8") as f:
        data = json.load(f)

    stories = []
    for i, item in enumerate(data):
        if isinstance(item, dict):
            sid     = str(item.get("id", "")).strip().replace("US-", "").replace("us-", "")
            builder = item.get("builder", "form_builder").strip().lower()
            builder = "dashboard_builder" if "dashboard" in builder else "form_builder"
            if sid:
                stories.append({"id": sid, "builder": builder})
        else:
            sid = str(item).strip().replace("US-", "").replace("us-", "")
            if sid:
                stories.append({"id": sid, "builder": "form_builder"})

    log.info("  " + str(len(stories)) + " story(ies) loaded")
    for s in stories:
        log.info("    US-" + s["id"] + " → " + s["builder"])
    return stories


def load_bugs_from_json(filepath):
    log.info("Loading bug entries from: " + filepath)
    with open(filepath, encoding="utf-8") as f:
        data = json.load(f)
    bugs = []
    for i, item in enumerate(data):
        sid     = str(item.get("story_id",   "")).strip()
        vpath   = str(item.get("video_path", "")).strip()
        builder = item.get("builder", "form_builder").strip().lower()
        builder = "dashboard_builder" if "dashboard" in builder else "form_builder"
        if not sid:   log.warning("  Item " + str(i) + ": missing story_id");   continue
        if not vpath: log.warning("  Item " + str(i) + ": missing video_path"); continue
        bugs.append({"story_id": sid, "video_path": vpath, "builder": builder})
    log.info("  " + str(len(bugs)) + " bug entry(ies)")
    return bugs




def load_unified_input(filepath: str):
    """
    UNIFIED INPUT: Loads both stories and bugs from a single JSON file.

    Format:
    {
      "stories": [{"id": "28136", "builder": "form_builder"}, ...],
      "bugs":    [{"story_id": "28136", "video_path": "videos/bug.mp4", "builder": "form_builder"}, ...]
    }

    Run with: python main_fixed.py --input-file input.json
    This processes all stories first, then all bugs, in one command.
    """
    log.info("Loading unified input from: " + filepath)
    with open(filepath, encoding="utf-8") as f:
        data = json.load(f)

    def fix_builder(b):
        return "dashboard_builder" if "dashboard" in str(b).lower() else "form_builder"

    stories = []
    for item in data.get("stories", []):
        sid = str(item.get("id", "")).strip().replace("US-", "").replace("us-", "")
        if sid:
            stories.append({"id": sid, "builder": fix_builder(item.get("builder", ""))})

    bugs = []
    for item in data.get("bugs", []):
        sid   = str(item.get("story_id", "")).strip().replace("US-", "").replace("us-", "")
        vpath = str(item.get("video_path", "")).strip()
        if sid and vpath:
            bugs.append({"story_id": sid, "video_path": vpath, "builder": fix_builder(item.get("builder", ""))})

    log.info("  Unified input loaded: " + str(len(stories)) + " story(ies), " + str(len(bugs)) + " bug(s)")
    for s in stories: log.info("    Story  US-" + s["id"] + " → " + s["builder"])
    for b in bugs:    log.info("    Bug    US-" + b["story_id"] + " | " + b["video_path"])
    return stories, bugs


# =============================================================================
# SECTION 24: SAVE RESULTS TO CSV
# =============================================================================

def save_results(results, mode):
    ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = os.path.join("output", "results_" + mode + "_" + ts + ".csv")
    rows = []
    for r in results:
        tc  = r.get("test_cases", {})
        pos = [s.get("title", "") for s in tc.get("positive", [])]
        neg = [s.get("title", "") for s in tc.get("negative", [])]
        review = r.get("review_report", {})
        row = {
            "story_id":           r.get("story_id",    ""),
            "story_title":        r.get("story_title", ""),
            "builder":            r.get("builder",     "form_builder"),
            "plan_id":            r.get("plan_id",     ""),
            "suite_id":           r.get("suite_id",    ""),
            "test_case_ids":      ",".join(str(i) for i in r.get("case_ids", [])),
            "positive_count":     len(pos),
            "negative_count":     len(neg),
            "quality_score":      review.get("quality_score", ""),
            "coverage_gaps":      "; ".join(review.get("categories_missing_negatives", [])),
            "missing_scenarios":  "; ".join(review.get("missing_scenarios", [])),
            "positive_titles":    " | ".join(pos),
            "negative_titles":    " | ".join(neg),
            "testplan_url":       r.get("url", ""),
        }
        if "bug_id" in r:
            row["bug_id"]   = r.get("bug_id",    "")
            row["bug_url"]  = r.get("bug_url",   "")
            row["video"]    = r.get("video_path","")
        rows.append(row)

    if rows:
        with open(out, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        log.info("Results → " + out)


# =============================================================================
# SECTION 25: MODE 1 — STORY MODE
# =============================================================================

def run_story_mode(story_entry):
    story_id = story_entry["id"]
    builder  = story_entry.get("builder", "form_builder")

    log.info("\n" + "=" * 64)
    log.info("  [Mode 1] Story: US-" + story_id + " | Builder: " + builder)
    log.info("=" * 64)

    story = fetch_user_story(story_id)
    if not story:
        return {}

    story["builder"] = builder
    app_url = get_builder_url(PROJECT_KNOWLEDGE, builder)
    log.info("  Builder URL: " + app_url)

    test_cases, review_report = run_agent_pipeline(story)

    log.info("\n  Writing to Azure DevOps via ADO MCP ...")
    plan_id, root_suite_id = create_test_plan(story)
    suite_id = create_test_suite(plan_id, root_suite_id, story)
    case_ids = create_all_test_cases(plan_id, suite_id, test_cases, story["id"])
    link_testplan_to_story(plan_id, story["id"])

    url = ("https://dev.azure.com/" + AZURE_ORG + "/" + AZURE_PROJECT
           + "/_testManagement/define?planId=" + str(plan_id))
    log.info("\n  DONE! " + url)

    return {
        "story_id":     story["id"],
        "story_title":  story["title"],
        "builder":      builder,
        "plan_id":      plan_id,
        "suite_id":     suite_id,
        "case_ids":     case_ids,
        "test_cases":   test_cases,
        "review_report": review_report,
        "url":          url,
    }


# =============================================================================
# SECTION 26: MODE 2 — BUG MODE
# =============================================================================

def run_bug_mode(bug_entry):
    story_id   = bug_entry["story_id"]
    video_path = bug_entry["video_path"]
    builder    = bug_entry.get("builder", "form_builder")

    log.info("\n" + "=" * 64)
    log.info("  [Mode 2] Bug for US-" + story_id + " | Builder: " + builder + " | " + video_path)
    log.info("=" * 64)

    story = fetch_user_story(story_id)
    if not story:
        return {}

    story["builder"] = builder

    frames_b64 = extract_frames_as_base64(video_path)
    bug_report = agent_bug_analyst(story, frames_b64)  # Two-pass if frames > threshold

    log.info("\n  Creating Bug via ADO MCP ...")
    bug_info = create_bug(bug_report, story_id)

    log.info("\n  Running 4-agent test case pipeline ...")
    test_cases, review_report = run_agent_pipeline(story)

    log.info("\n  Creating Test Plan via ADO MCP ...")
    plan_id, root_suite_id = create_test_plan(story)
    suite_id = create_test_suite(plan_id, root_suite_id, story)
    case_ids = create_all_test_cases(plan_id, suite_id, test_cases, story["id"])
    link_testplan_to_story(plan_id, story["id"])

    url = ("https://dev.azure.com/" + AZURE_ORG + "/" + AZURE_PROJECT
           + "/_testManagement/define?planId=" + str(plan_id))

    log.info("\n  DONE!")
    log.info("  Bug:       " + bug_info["bug_url"])
    log.info("  Test Plan: " + url)

    return {
        "story_id":      story["id"],
        "story_title":   story["title"],
        "builder":       builder,
        "bug_id":        bug_info["bug_id"],
        "bug_url":       bug_info["bug_url"],
        "video_path":    video_path,
        "plan_id":       plan_id,
        "suite_id":      suite_id,
        "case_ids":      case_ids,
        "test_cases":    test_cases,
        "review_report": review_report,
        "url":           url,
    }


# =============================================================================
# SECTION 27: MAIN ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TestForge — ADO Test Plan Generator (Enhanced)")
    parser.add_argument("--stories-file",    type=str, default="", help="Path to stories_input.json")
    parser.add_argument("--input-file",      type=str, default="", help="Path to unified input.json (stories + bugs combined)")
    parser.add_argument("--bugs-file",       type=str, default="", help="Path to bugs_input.json")
    parser.add_argument("--knowledge-file",  type=str, default="", help="Path to knowledge_base.json")
    parser.add_argument("--list-tools",      action="store_true",  help="List ADO MCP tools and exit")
    parser.add_argument("--no-critique",     action="store_true",  help="Disable Agent 4 self-critique")
    args = parser.parse_args()

    # CLI flag overrides env var
    if args.no_critique:
        ENABLE_SELF_CRITIQUE = False

    stories_file   = args.stories_file
    bugs_file      = args.bugs_file
    unified_file   = args.input_file
    knowledge_file = args.knowledge_file or KNOWLEDGE_BASE_FILE

    PROJECT_KNOWLEDGE = load_knowledge_base(knowledge_file)

    if unified_file:
        if not os.path.isfile(unified_file):
            log.error("File not found: " + unified_file); sys.exit(1)
        unified_stories, unified_bugs = load_unified_input(unified_file)
        stories_file = unified_file   # signal that we have work to do
        # override to use unified lists directly
        import types
        _uf_stories = unified_stories
        _uf_bugs    = unified_bugs
    else:
        _uf_stories = []
        _uf_bugs    = []

    if not stories_file and not bugs_file and not args.list_tools:
        if   os.path.isfile(BUGS_FILE):    bugs_file    = BUGS_FILE
        elif os.path.isfile(STORIES_FILE): stories_file = STORIES_FILE

    if not stories_file and not bugs_file and not args.list_tools:
        print("\nUsage:")
        print("  python main_fixed.py --stories-file stories_input.json")
        print("  python main_fixed.py --bugs-file    bugs_input.json")
        print("  python main_fixed.py --no-critique  (skip Agent 4 self-critique)")
        print("  python main_fixed.py --list-tools")
        print("\nEnv overrides (in config/.env):")
        print("  MAX_STRATEGY_RETRIES=3      (Improvement 1: retry attempts for Agent 1)")
        print("  MIN_STRATEGY_CATEGORIES=8   (Improvement 1: minimum category count)")
        print("  ENABLE_SELF_CRITIQUE=true   (Improvement 3: Agent 4 on/off)")
        print("  TWO_PASS_BUG_MIN_FRAMES=4   (Improvement 4: frame threshold for two-pass)")
        sys.exit(0)

    try:
        mcp.start()
    except FileNotFoundError:
        log.error("npx not found. Install Node.js 20+ from https://nodejs.org")
        sys.exit(1)
    except Exception as e:
        log.error("Failed to start ADO MCP server: " + str(e))
        sys.exit(1)

    if args.list_tools:
        log.info("\n=== ADO MCP TOOLS ===")
        for t in sorted(mcp.list_tools(), key=lambda x: x.get("name", "")):
            log.info("  " + t["name"])
        mcp.stop()
        sys.exit(0)

    all_results = []
    mode_label  = ""

    try:
        if unified_file:
            mode_label = "unified"
            for entry in _uf_stories:
                result = run_story_mode(entry)
                if result: all_results.append(result)
            for entry in _uf_bugs:
                result = run_bug_mode(entry)
                if result: all_results.append(result)

        elif bugs_file:
            if not os.path.isfile(bugs_file):
                log.error("File not found: " + bugs_file); sys.exit(1)
            mode_label  = "bugs"
            bug_entries = load_bugs_from_json(bugs_file)
            if not bug_entries:
                log.error("No valid bug entries in " + bugs_file); sys.exit(1)
            for entry in bug_entries:
                result = run_bug_mode(entry)
                if result: all_results.append(result)

        elif stories_file:
            if not os.path.isfile(stories_file):
                log.error("File not found: " + stories_file); sys.exit(1)
            mode_label = "stories"
            story_list = load_ids_from_json(stories_file)
            if not story_list:
                log.error("No valid IDs in " + stories_file); sys.exit(1)
            for story_entry in story_list:
                result = run_story_mode(story_entry)
                if result: all_results.append(result)

    except KeyboardInterrupt:
        print("\n  [Interrupted by user]")
    except Exception as e:
        log.error("Unexpected error: " + str(e), exc_info=True)
    finally:
        mcp.stop()

    if all_results:
        save_results(all_results, mode_label)