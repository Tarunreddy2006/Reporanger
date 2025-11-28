import os
import time
import shutil
import subprocess
import uvicorn
import uuid
import stat
import google.generativeai as genai
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel
from typing import Dict, Optional
from google.generativeai.types import FunctionDeclaration, Tool, HarmCategory, HarmBlockThreshold

# ==========================================
# 1. CONFIGURATION
# ==========================================
os.environ["GEMINI_API_KEY"] = os.getenv("GEMINI_API_KEY", "")

if not os.environ["GEMINI_API_KEY"]:
    print("⚠️ WARNING: API Key not set. App will crash.")

OUTPUT_DIR = "ai_agents"
CONTEXT_FILE = "repo_context.txt"
TEMP_CLONE_DIR = "cloned_repo"
IGNORE_DIRS = {'.git', '__pycache__', 'node_modules', 'venv', 'env', '.idea', '.vscode', 'dist', 'build'}
ALLOWED_EXTENSIONS = {'.py', '.js', '.ts', '.jsx', '.tsx', '.html', '.css', '.java', '.cpp', '.md', '.json', '.sql', '.yaml', '.yml', '.sh', '.rb', '.go', '.rs', '.php', '.cs', '.swift', '.kt'}

app = FastAPI(title="Repo-Ranger Pro")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ==========================================
# 2. SESSION MEMORY
# ==========================================
SESSIONS: Dict[str, Dict] = {}

def get_session(session_id: str):
    if session_id not in SESSIONS:
        SESSIONS[session_id] = {
            "history": [], 
            "file_name": None, 
            "analysis": None,
            "generated_file": None
        }
    return SESSIONS[session_id]

# ==========================================
# 3. BACKEND LOGIC
# ==========================================

def cleanup_temp_folder(session_id):
    path = f"{TEMP_CLONE_DIR}_{session_id}"
    if os.path.exists(path):
        def on_rm_error(func, path, exc_info):
            try:
                os.chmod(path, stat.S_IWRITE)
                os.unlink(path)
            except: pass
        try:
            shutil.rmtree(path, onerror=on_rm_error)
        except Exception as e:
            print(f"Cleanup Warning: {e}")

def clone_github_repo(url: str, session_id: str):
    target_dir = f"{TEMP_CLONE_DIR}_{session_id}"
    cleanup_temp_folder(session_id)
    
    print(f"🌍 Cloning {url}...")
    if not url.startswith("https://github.com/"):
         raise ValueError("Security Error: Only 'https://github.com/' URLs are allowed.")

    try:
        result = subprocess.run(["git", "clone", url, target_dir], capture_output=True, text=True)
        if result.returncode != 0: raise Exception(f"Git Clone Failed: {result.stderr}")
        return target_dir
    except FileNotFoundError: raise Exception("Git is not installed.")

def create_context(root_path, session_id):
    if not os.path.exists(root_path): raise FileNotFoundError(f"Path not found.")
    content = ""
    file_count = 0
    MAX_SIZE = 2 * 1024 * 1024 
    current_size = 0

    for root, dirs, files in os.walk(root_path):
        dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
        for file in files:
            _, ext = os.path.splitext(file)
            if ext in ALLOWED_EXTENSIONS:
                try:
                    path = os.path.join(root, file)
                    with open(path, "r", encoding="utf-8") as f:
                        file_content = f.read()
                        text_chunk = f"\n\n--- FILE: {file} ---\n{file_content}\n--- END FILE ---\n"
                        if current_size + len(text_chunk) > MAX_SIZE: break
                        content += text_chunk
                        current_size += len(text_chunk)
                    file_count += 1
                except: pass
    
    ctx_filename = f"{CONTEXT_FILE}_{session_id}.txt"
    with open(ctx_filename, "w", encoding="utf-8") as f: f.write(content)
    return file_count, ctx_filename

def save_code_tool(filename: str, content: str):
    safe_name = os.path.basename(filename)
    path = os.path.join(OUTPUT_DIR, safe_name)
    with open(path, "w", encoding="utf-8") as f: f.write(content)
    return f"Saved to {safe_name}"

save_tool = Tool(function_declarations=[FunctionDeclaration(
    name="save_code_tool", description="Save code to file.",
    parameters={"type": "object", "properties": {"filename": {"type": "string"}, "content": {"type": "string"}}, "required": ["filename", "content"]}
)])

safety = {HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE}

# ==========================================
# 4. API ENDPOINTS
# ==========================================

class SessionRequest(BaseModel):
    session_id: str
    data: Optional[str] = None 

@app.get("/api/history/{session_id}")
async def get_history_endpoint(session_id: str):
    if session_id in SESSIONS:
        return SESSIONS[session_id]
    return {"history": [], "file_name": None, "analysis": None}

@app.post("/api/ingest")
async def ingest_endpoint(req: SessionRequest):
    try:
        session = get_session(req.session_id)
        genai.configure(api_key=os.environ["GEMINI_API_KEY"])
        
        target = req.data
        if target.startswith("http"):
            try:
                target = clone_github_repo(target, req.session_id)
            except ValueError as ve:
                raise HTTPException(status_code=400, detail=str(ve))
        
        count, ctx_path = create_context(target, req.session_id)
        repo_file = genai.upload_file(path=ctx_path, display_name=f"Context_{req.session_id}")
        
        while repo_file.state.name == "PROCESSING":
            time.sleep(1)
            repo_file = genai.get_file(repo_file.name)
        
        session["file_name"] = repo_file.name
        session["history"].append({"role": "system", "content": f"Repository ingested successfully ({count} files)."})
        
        return {"status": "success", "file_name": repo_file.name, "file_count": count}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/analyze")
async def analyze_endpoint(req: SessionRequest):
    try:
        session = get_session(req.session_id)
        if not session["file_name"]: raise HTTPException(status_code=400, detail="No repo loaded.")

        genai.configure(api_key=os.environ["GEMINI_API_KEY"])
        file_ref = genai.get_file(session["file_name"]) 
        model = genai.GenerativeModel("gemini-2.5-flash", safety_settings=safety)
        
        response = model.generate_content([file_ref, "Analyze this codebase. Identify ONE specific file that is messy or needs documentation. Explain why using Markdown."])
        
        session["analysis"] = response.text
        session["history"].append({"role": "model", "content": f"**Analysis Complete:**\n{response.text}"})

        return {"analysis": response.text}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/refactor")
async def refactor_endpoint(req: SessionRequest):
    try:
        session = get_session(req.session_id)
        if not session["analysis"]: raise HTTPException(status_code=400, detail="Run analysis first.")
        
        genai.configure(api_key=os.environ["GEMINI_API_KEY"])
        file_ref = genai.get_file(session["file_name"])
        model = genai.GenerativeModel("gemini-2.5-pro", tools=[save_tool], safety_settings=safety)
        
        prompt = f"Based on this analysis: {session['analysis']}. Refactor that specific file completely. Use 'save_code_tool' to save it."
        
        response = model.generate_content(
            [file_ref, prompt], 
            tool_config={'function_calling_config': {'mode': 'ANY'}} 
        )
        
        result_file = None
        if response.candidates and response.candidates[0].content.parts:
            for part in response.candidates[0].content.parts:
                if part.function_call and part.function_call.name == "save_code_tool":
                    fc = part.function_call
                    save_code_tool(fc.args["filename"], fc.args["content"])
                    result_file = fc.args["filename"]
        
        if result_file:
            session["generated_file"] = result_file
            session["history"].append({"role": "model", "content": f"I have refactored and saved **{result_file}**."})

        return {"status": "success", "generated_file": result_file} if result_file else {"status": "no_file", "message": response.text}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/chat")
async def chat_endpoint(req: SessionRequest):
    try:
        session = get_session(req.session_id)
        genai.configure(api_key=os.environ["GEMINI_API_KEY"])
        
        session["history"].append({"role": "user", "content": req.data})

        # --- MEMORY INJECTION ---
        system_context = "You are Repo-Ranger, an expert coding assistant.\n"
        
        if session.get("analysis"):
            system_context += f"\n--- CODEBASE ANALYSIS ---\n{session['analysis']}\n-------------------------\n"
        
        if session.get("generated_file"):
            system_context += f"\n--- LATEST ACTION ---\nYou have refactored the code and saved it to: {session['generated_file']}.\n---------------------\n"

        system_context += "\nCHAT HISTORY:\n"
        
        for msg in session["history"]: 
            if msg['role'] != 'system':
                system_context += f"{msg['role']}: {msg['content']}\n"
        system_context += f"Assistant:"

        model = genai.GenerativeModel("gemini-2.5-flash", tools=[save_tool], safety_settings=safety)
        
        inputs = [system_context]
        if session["file_name"]:
            try: inputs.insert(0, genai.get_file(session["file_name"]))
            except: pass

        response = model.generate_content(inputs, tool_config={'function_calling_config': {'mode': 'AUTO'}})
        
        reply_text = response.text or ""
        
        if response.candidates and response.candidates[0].content.parts:
            for part in response.candidates[0].content.parts:
                if part.function_call and part.function_call.name == "save_code_tool":
                    fc = part.function_call
                    save_code_tool(fc.args["filename"], fc.args["content"])
                    reply_text += f"\n\n⚡ [Tool Used] Saved changes to **{fc.args['filename']}**."

        session["history"].append({"role": "model", "content": reply_text})

        return {"reply": reply_text}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.get("/download/{filename}")
async def download_file(filename: str):
    safe_name = os.path.basename(filename)
    file_path = os.path.join(OUTPUT_DIR, safe_name)
    if os.path.exists(file_path): return FileResponse(file_path, filename=filename)
    return {"error": "File not found"}

# ==========================================
# 5. FRONTEND UI (WITH SIDEBAR)
# ==========================================

@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    return r"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Repo-Ranger Pro</title>
        <script src="https://cdn.tailwindcss.com"></script>
        <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
        <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
        <style>
             .prose h1 { font-size: 1.2em; font-weight: bold; color: #a78bfa; }
             .prose ul { list-style-type: disc; padding-left: 1.5em; }
             .prose code { background: #1f2937; padding: 0.2em 0.4em; border-radius: 4px; color: #34d399; }
             .fade-in { animation: fadeIn 0.3s ease-in; } 
             @keyframes fadeIn { from { opacity: 0; transform: translateY(5px); } to { opacity: 1; transform: translateY(0); } }
             /* Scrollbar Styling */
             ::-webkit-scrollbar { width: 8px; }
             ::-webkit-scrollbar-track { background: #111827; }
             ::-webkit-scrollbar-thumb { background: #374151; border-radius: 4px; }
             ::-webkit-scrollbar-thumb:hover { background: #4b5563; }
        </style>
    </head>
    <body class="bg-gray-900 text-white font-sans antialiased h-screen flex overflow-hidden">
        
        <div class="w-64 bg-gray-950 border-r border-gray-800 flex flex-col flex-shrink-0 transition-all duration-300">
            <div class="p-4 border-b border-gray-800 flex items-center gap-2">
                <i class="fa-solid fa-robot text-blue-500"></i>
                <span class="font-bold text-lg bg-clip-text text-transparent bg-gradient-to-r from-blue-400 to-emerald-400">Repo-Ranger</span>
            </div>
            
            <div class="p-4">
                <button onclick="createNewChat()" class="w-full bg-blue-600 hover:bg-blue-500 text-white font-medium py-2 rounded-lg transition flex items-center justify-center gap-2 shadow-lg">
                    <i class="fa-solid fa-plus"></i> New Chat
                </button>
            </div>

            <div class="flex-1 overflow-y-auto px-2 space-y-1" id="chatList">
                </div>

            <div class="p-4 border-t border-gray-800 text-xs text-gray-500 flex justify-between items-center">
                <span>Gemini 2.5 Pro</span>
                <button onclick="clearAllChats()" class="text-red-400 hover:text-red-300"><i class="fa-solid fa-trash"></i></button>
            </div>
        </div>

        <div class="flex-1 flex flex-col min-w-0">
            <div class="h-16 border-b border-gray-800 flex items-center justify-between px-6 bg-gray-900">
                <div class="flex items-center gap-3">
                    <h2 class="font-semibold text-gray-200" id="headerTitle">Current Session</h2>
                    <span id="statusBadge" class="text-xs px-2 py-1 rounded bg-gray-800 text-gray-400">Idle</span>
                </div>
            </div>

            <div class="flex-1 flex overflow-hidden">
                
                <div class="w-1/3 bg-gray-900 border-r border-gray-800 flex flex-col min-w-[300px]">
                    <div class="p-6 border-b border-gray-800">
                        <label class="text-xs font-bold text-gray-500 uppercase tracking-wider mb-2 block">Target Repository</label>
                        <input type="text" id="repoPath" placeholder="https://github.com/username/repo" 
                            class="w-full bg-gray-800 border border-gray-700 rounded-lg px-4 py-3 text-sm text-white focus:ring-2 focus:ring-blue-500 focus:outline-none mb-3 placeholder-gray-600">
                        <button onclick="startIngest()" class="w-full bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-300 font-medium py-2 rounded-lg transition flex items-center justify-center gap-2">
                            <i class="fa-solid fa-play text-emerald-500"></i> Run Agents
                        </button>
                    </div>

                    <div class="flex-1 overflow-y-auto p-6 space-y-6">
                        <div class="relative pl-4 border-l-2 border-purple-500/30">
                            <div class="text-purple-400 text-xs font-bold uppercase mb-1 flex items-center gap-2">
                                <i class="fa-solid fa-magnifying-glass"></i> Analyst Agent
                            </div>
                            <div id="analystOutput" class="text-xs text-gray-400 font-mono leading-relaxed max-h-60 overflow-y-auto prose prose-invert">
                                Waiting for input...
                            </div>
                        </div>

                        <div class="relative pl-4 border-l-2 border-emerald-500/30">
                            <div class="text-emerald-400 text-xs font-bold uppercase mb-1 flex items-center gap-2">
                                <i class="fa-solid fa-code"></i> Developer Agent
                            </div>
                            <div id="devOutput" class="text-xs text-gray-400 font-mono mb-3">Waiting for analysis...</div>
                            <a id="downloadBtn" href="#" class="hidden flex items-center justify-center gap-2 bg-emerald-600/10 text-emerald-400 border border-emerald-500/50 hover:bg-emerald-600 hover:text-white py-2 rounded-lg text-xs font-medium transition">
                                <i class="fa-solid fa-download"></i> Download File
                            </a>
                        </div>
                    </div>
                </div>

                <div class="flex-1 bg-gray-950 flex flex-col relative">
                    <div id="chatHistory" class="flex-1 overflow-y-auto p-6 space-y-6"></div>
                    
                    <div id="chatLock" class="absolute inset-0 bg-gray-950/80 backdrop-blur-sm flex flex-col items-center justify-center z-20">
                        <i class="fa-solid fa-lock text-3xl text-gray-700 mb-3"></i>
                        <p class="text-gray-500 text-sm font-medium">Agents running...</p>
                    </div>

                    <div class="p-6 bg-gray-950 border-t border-gray-800">
                        <div class="relative">
                            <input type="text" id="chatInput" placeholder="Ask follow-up questions..." 
                                class="w-full bg-gray-900 border border-gray-700 rounded-xl pl-5 pr-12 py-4 text-sm text-white focus:ring-2 focus:ring-blue-600 outline-none shadow-lg transition"
                                onkeypress="handleEnter(event)">
                            <button onclick="sendChat()" class="absolute right-3 top-3 bg-blue-600 hover:bg-blue-500 w-9 h-9 rounded-lg flex items-center justify-center text-white shadow transition transform hover:scale-105">
                                <i class="fa-solid fa-paper-plane text-xs"></i>
                            </button>
                        </div>
                    </div>
                </div>
            </div>
        </div>

        <script>
            // --- STATE MANAGEMENT ---
            let chats = JSON.parse(localStorage.getItem('rr_chats')) || [];
            let activeSessionId = null;
            let globalFileName = "";
            let globalAnalysis = "";

            // --- INIT ---
            window.onload = function() {
                renderSidebar();
                if (chats.length > 0) {
                    loadChat(chats[0].id); // Load most recent
                } else {
                    createNewChat(); // Start fresh if empty
                }
            };

            // --- SIDEBAR LOGIC ---
            function createNewChat() {
                const newId = "sess_" + Math.random().toString(36).substr(2, 9);
                const newChat = { id: newId, title: "New Chat " + (chats.length + 1) };
                chats.unshift(newChat); // Add to top
                saveChats();
                renderSidebar();
                loadChat(newId);
            }

            function loadChat(id) {
                activeSessionId = id;
                renderSidebar(); // Update active highlight
                
                // Reset UI state first
                document.getElementById('repoPath').value = "";
                document.getElementById('analystOutput').innerText = "Waiting for input...";
                document.getElementById('devOutput').innerText = "Waiting for analysis...";
                document.getElementById('downloadBtn').classList.add('hidden');
                document.getElementById('chatHistory').innerHTML = "";
                document.getElementById('chatLock').classList.remove('hidden'); // Lock until we confirm state
                document.getElementById('statusBadge').innerText = "Loading...";

                // Find title
                const chatObj = chats.find(c => c.id === id);
                document.getElementById('headerTitle').innerText = chatObj ? chatObj.title : "Session";

                // Restore from Server
                restoreHistory(id);
            }

            function renderSidebar() {
                const list = document.getElementById('chatList');
                list.innerHTML = "";
                chats.forEach(chat => {
                    const isActive = chat.id === activeSessionId;
                    const div = document.createElement('div');
                    div.className = `p-3 rounded-lg cursor-pointer text-sm font-medium transition truncate flex items-center gap-3 ${isActive ? 'bg-gray-800 text-white border-l-2 border-blue-500' : 'text-gray-400 hover:bg-gray-900 hover:text-gray-200'}`;
                    div.onclick = () => loadChat(chat.id);
                    div.innerHTML = `<i class="fa-regular fa-message text-xs opacity-50"></i> ${chat.title}`;
                    list.appendChild(div);
                });
            }

            function saveChats() {
                localStorage.setItem('rr_chats', JSON.stringify(chats));
            }

            function clearAllChats() {
                if(confirm("Delete all history?")) {
                    localStorage.removeItem('rr_chats');
                    location.reload();
                }
            }

            function updateChatTitle(id, title) {
                const chat = chats.find(c => c.id === id);
                if (chat) {
                    chat.title = title;
                    saveChats();
                    renderSidebar();
                    document.getElementById('headerTitle').innerText = title;
                }
            }

            // --- SERVER COMMUNICATION ---
            async function restoreHistory(id) {
                try {
                    const res = await fetch(`/api/history/${id}`);
                    const data = await res.json();

                    // Restore Chat Bubbles
                    if(data.history && data.history.length > 0) {
                        data.history.forEach(msg => {
                            if(msg.role === 'user') addUserMessage(msg.content);
                            else if (msg.role === 'model' || msg.role === 'system') addBotMessage(msg.content);
                        });
                        
                        // If we have history, unlock chat
                        document.getElementById('chatLock').classList.add('hidden');
                    } else {
                        // Brand new chat
                        document.getElementById('chatLock').classList.add('hidden');
                        addBotMessage("Ready! Paste a GitHub URL on the left to begin.");
                    }

                    // Restore Panel State
                    if(data.file_name) {
                        globalFileName = data.file_name;
                        document.getElementById('statusBadge').innerText = "Context Loaded";
                        document.getElementById('statusBadge').className = "text-xs px-2 py-1 rounded bg-emerald-900 text-emerald-400";
                    } else {
                        document.getElementById('statusBadge').innerText = "Idle";
                    }

                    if(data.analysis) {
                        globalAnalysis = data.analysis;
                        document.getElementById('analystOutput').innerHTML = marked.parse(data.analysis);
                    }

                    if(data.generated_file) {
                        document.getElementById('devOutput').innerHTML = `<span class="text-emerald-400">Generated <b>${data.generated_file}</b></span>`;
                        const btn = document.getElementById('downloadBtn');
                        btn.href = `/download/${data.generated_file}`;
                        btn.classList.remove('hidden');
                    }

                } catch(e) { 
                    console.log("Error restoring history", e);
                    document.getElementById('chatLock').classList.add('hidden');
                }
            }

            // --- ACTIONS ---
            async function startIngest() {
                const path = document.getElementById('repoPath').value;
                if(!path) return alert("Please enter a path!");

                // Update Title
                const repoName = path.split('/').pop().replace('.git', '');
                updateChatTitle(activeSessionId, repoName);

                document.getElementById('statusBadge').innerText = "Ingesting...";
                document.getElementById('chatLock').classList.remove('hidden'); // Lock chat during process

                try {
                    const res = await fetch('/api/ingest', { 
                        method: 'POST', headers: {'Content-Type': 'application/json'}, 
                        body: JSON.stringify({session_id: activeSessionId, data: path}) 
                    });
                    const data = await res.json();
                    
                    if(data.status === 'success') {
                        document.getElementById('statusBadge').innerText = "Analyzing...";
                        startAnalyze();
                    } else { 
                        alert("Error: " + data.detail); 
                        document.getElementById('chatLock').classList.add('hidden');
                    }
                } catch(e) { alert("Network Error"); document.getElementById('chatLock').classList.add('hidden'); }
            }

            async function startAnalyze() {
                document.getElementById('analystOutput').innerHTML = '<span class="animate-pulse text-purple-400">Analyzing structure...</span>';
                
                const res = await fetch('/api/analyze', { 
                    method: 'POST', headers: {'Content-Type': 'application/json'}, 
                    body: JSON.stringify({session_id: activeSessionId}) 
                });
                const data = await res.json();
                
                if(data.analysis) {
                    globalAnalysis = data.analysis;
                    document.getElementById('analystOutput').innerHTML = marked.parse(data.analysis);
                    addBotMessage("Analysis complete. Reviewing suggestions...");
                    document.getElementById('statusBadge').innerText = "Refactoring...";
                    startRefactor();
                } else { 
                    document.getElementById('analystOutput').innerText = "Analysis failed.";
                    document.getElementById('chatLock').classList.add('hidden');
                }
            }

            async function startRefactor() {
                document.getElementById('devOutput').innerHTML = '<span class="animate-pulse text-emerald-400">Writing code...</span>';
                
                const res = await fetch('/api/refactor', { 
                    method: 'POST', headers: {'Content-Type': 'application/json'}, 
                    body: JSON.stringify({session_id: activeSessionId}) 
                });
                const data = await res.json();
                
                if(data.status === 'success') {
                    document.getElementById('devOutput').innerHTML = `<span class="text-emerald-400">Generated <b>${data.generated_file}</b></span>`;
                    const btn = document.getElementById('downloadBtn');
                    btn.href = `/download/${data.generated_file}`;
                    btn.classList.remove('hidden');
                    
                    document.getElementById('chatLock').classList.add('hidden');
                    document.getElementById('statusBadge').innerText = "Complete";
                    addBotMessage(`I've refactored the code and saved ${data.generated_file}. You can now ask questions about the changes.`);
                } else { 
                    document.getElementById('devOutput').innerHTML = `<span class="text-red-400">${data.message}</span>`;
                    document.getElementById('chatLock').classList.add('hidden');
                }
            }

            // --- CHAT HELPERS ---
            function handleEnter(e) { if(e.key === 'Enter') sendChat(); }
            
            function addUserMessage(msg) {
                const div = document.createElement('div');
                div.className = "flex gap-4 flex-row-reverse fade-in";
                div.innerHTML = `<div class="w-8 h-8 rounded-full bg-gray-700 flex items-center justify-center shrink-0 text-xs font-bold">U</div><div class="bg-blue-600 p-3 rounded-2xl rounded-tr-none max-w-xl text-sm text-white shadow-md">${msg}</div>`;
                document.getElementById('chatHistory').appendChild(div);
                scrollToBottom();
            }

            function addBotMessage(msg) {
                const div = document.createElement('div');
                div.className = "flex gap-4 fade-in";
                const safeMsg = marked.parse(msg); 
                div.innerHTML = `<div class="w-8 h-8 rounded-full bg-gradient-to-br from-blue-500 to-purple-600 flex items-center justify-center shrink-0 shadow-lg text-xs text-white"><i class="fa-solid fa-robot"></i></div><div class="bg-gray-800/80 p-4 rounded-2xl rounded-tl-none max-w-xl text-sm text-gray-200 shadow-md border border-gray-700/50 whitespace-pre-wrap prose prose-invert">${safeMsg}</div>`;
                document.getElementById('chatHistory').appendChild(div);
                scrollToBottom();
            }

            function scrollToBottom() { const d = document.getElementById('chatHistory'); d.scrollTop = d.scrollHeight; }

            async function sendChat() {
                const input = document.getElementById('chatInput');
                const msg = input.value.trim();
                if(!msg) return;
                addUserMessage(msg);
                input.value = "";
                
                const typingId = "typing-" + Date.now();
                const typingDiv = document.createElement('div');
                typingDiv.id = typingId;
                typingDiv.innerHTML = `<div class="text-gray-500 italic text-xs ml-12 animate-pulse">Repo-Ranger is thinking...</div>`;
                document.getElementById('chatHistory').appendChild(typingDiv);
                scrollToBottom();

                try {
                    const res = await fetch('/api/chat', { 
                        method: 'POST', headers: {'Content-Type': 'application/json'}, 
                        body: JSON.stringify({session_id: activeSessionId, data: msg}) 
                    });
                    const data = await res.json();
                    document.getElementById(typingId).remove();
                    addBotMessage(data.reply);
                } catch(e) {
                    document.getElementById(typingId).remove();
                    addBotMessage("Connection error.");
                }
            }
        </script>
    </body>
    </html>
    """

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=False)