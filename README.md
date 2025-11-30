# 🤖 Repo-Ranger: Enterprise Technical Debt Agent

![Status](https://img.shields.io/badge/Status-Production%20Ready-success) ![Track](https://img.shields.io/badge/Track-Enterprise%20Agents-blue) ![Docker](https://img.shields.io/badge/Deployment-Docker-blue) ![Powered By](https://img.shields.io/badge/Powered%20By-Gemini%201.5%20Pro-orange)

**Repo-Ranger** is an autonomous multi-agent system designed to eliminate technical debt in enterprise software. Unlike simple chat assistants, Repo-Ranger ingests entire repositories, diagnoses architectural issues, and **autonomously writes refactored code** directly to the file system.

---

## 📺 Project Demo
[![Watch the Demo](https://img.youtube.com/vi/PASTE_YOUR_YOUTUBE_LINK_HERE/0.jpg)](https://youtu.be/VLJZkNAol-A).

*(Click the image above to watch the walkthrough)*

---

## 🚀 The Problem
Modern engineering teams spend up to **40% of their time** on maintenance and technical debt.
* **Context Switching:** Developers waste hours understanding legacy codebases before they can fix them.
* **Manual Refactoring:** Fixing linting errors, adding docstrings, and updating deprecated patterns is repetitive and slow.
* **Siloed Tools:** Chatbots don't know the full codebase; Linters find errors but don't fix them.

## 💡 The Solution
Repo-Ranger Pro automates the "Identify -> Plan -> Fix" loop:
1.  **Ingests** full repositories using Gemini's **2M token context window**.
2.  **Analyzes** structural weaknesses using a reasoning model (**Analyst Agent**).
3.  **Refactors** code autonomously using a tool-equipped **Developer Agent**.
4.  **Persists** context across sessions using a robust multi-thread memory system.

---

## ⚙️ Technical Architecture

This project demonstrates the **"Rule of 3"** core agentic concepts required for the Capstone:

### 1. 🤖 Multi-Agent System (Sequential)
* **Analyst Agent (Gemini 2.5 Flash):** Scans the ingested codebase and produces a Markdown-formatted architectural audit, identifying the most critical file to fix.
* **Developer Agent (Gemini 2.5 Pro):** Receives the audit plan and executes specific code changes to resolve the issues.

### 2. 🛠️ Custom Tools (Function Calling)
* The agents are not just text generators. The Developer Agent uses a custom `save_code_tool` (defined in Python) to physically write the refactored files to the disk, enabling a true "Human-in-the-loop" workflow where the user can download the result.

### 3. 🧠 Advanced Memory & State Management
* **Session Persistence:** Implements a dual-layer memory system. The Backend (FastAPI) stores history in RAM, while the Frontend uses LocalStorage to persist the Session ID. This ensures chat history and analysis context survives page reloads.
* **Context Injection:** Solves "LLM Amnesia" by injecting the analysis state into the system prompt of every new chat turn.

### 4. 🐳 Enterprise Deployment (Bonus)
* **Dockerized:** The application is fully containerized, ensuring it runs reliably on Linux/Windows.
* **Security:** Includes input validation to prevent command injection and isolates file operations within the container.

---

## 📸 Screenshots

### 1. The Dashboard (Multi-Chat & Analysis)
*The Analyst Agent diagnosing architectural issues while the Sidebar manages session history.*
![Dashboard](<img width="1901" height="964" alt="Screenshot 2025-11-28 182658" src="https://github.com/user-attachments/assets/aa0778f2-c911-4493-b4c4-f793fc3c9a28" />
)

### 2. Autonomous Refactoring (Success State)
*The Developer Agent successfully rewriting the code and providing a download link.*
![Success](<img width="1892" height="936" alt="Screenshot 2025-11-28 183345" src="https://github.com/user-attachments/assets/0d4d430b-12b1-428f-89e9-3f6c6de3ee28" />
)

### 3. Docker Deployment Proof
*Terminal logs showing the application successfully building and running inside a Linux container.*
![Terminal](<img width="1562" height="380" alt="Screenshot 2025-11-28 204648" src="https://github.com/user-attachments/assets/5cb9d35c-f0a8-4492-9398-51ae1d3b0bf4" />
)

---

## 🛠️ Installation & Usage

### Option 1: Run with Docker (Recommended)
This ensures the environment is exactly as intended.

```bash
# 1. Build the image
docker build -t repo-ranger .

# 2. Run the container
# Replace YOUR_API_KEY with your actual Google Gemini API Key
docker run -p 8000:8000 -e GEMINI_API_KEY="YOUR_API_KEY" repo-ranger
