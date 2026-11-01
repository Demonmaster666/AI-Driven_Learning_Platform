# app.py
import os
import json
import time
from flask import Flask, render_template, request, redirect, url_for, flash, session, send_file
from firebase_utils import init_firebase
from google.generativeai import client as genai_client
import google.generativeai as genai
from gtts import gTTS
import tempfile
from werkzeug.utils import secure_filename
from firebase_admin import firestore
import markdown2
from flask import jsonify
import re
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
import io
from dotenv import load_dotenv
load_dotenv()  # loads .env

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "super-secret-change-me")  # change in production

# Initialize Firebase / Firestore
try:
    db = init_firebase(app)
except Exception as e:
    print("Firebase init error:", e)
    db = None

# Configure Gemini API
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
else:
    print("Warning: GEMINI_API_KEY not set in env")


# --- Gemini AI Response Helper ---
def get_gemini_response(prompt: str):
    """
    Generates a Gemini AI response with a fixed system instruction.
    """
    try:
        system_instruction = """
        You are EduLink — a helpful, knowledgeable AI assistant built for students.
        Always explain clearly, in a friendly but concise tone.
        If a question is not academic, answer politely and redirect toward learning topics.
        Avoid repetition. Support Markdown formatting in your replies.
        """

        full_prompt = f"{system_instruction}\n\nUser: {prompt}\nAI:"
        model = genai.GenerativeModel("gemini-2.5-flash")
        response = model.generate_content(full_prompt)
        return response.text.strip() if response and response.text else "(No response)"
    except Exception as e:
        print("Gemini API Error:", e)
        return "⚠️ Sorry, I couldn’t generate a response right now."


# utility to require login
def login_required(f):
    from functools import wraps
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"):
            flash("You must be logged in to access that page.", "warning")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper

@app.route("/")
def index():
    if session.get("logged_in"):
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))

# --- AUTH: login / register ---
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if not db:
            flash("Server firebase not initialized", "danger")
            return render_template("login.html")
        doc = db.collection("users").document(username).get()
        if doc.exists:
            d = doc.to_dict()
            if d.get("password") == password:
                session["logged_in"] = True
                session["username"] = username
                session["role"] = d.get("role", "student")
                flash("Logged in", "success")
                return redirect(url_for("dashboard"))
        flash("Invalid credentials", "danger")
    return render_template("login.html")

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password")
        role = request.form.get("role", "student")
        if not username or not password:
            flash("Please fill all fields", "warning")
            return render_template("register.html")
        user_ref = db.collection("users").document(username)
        if user_ref.get().exists:
            flash("Username already exists", "danger")
            return render_template("register.html")
        user_ref.set({
            "password": password,
            "role": role,
            "created_at": firestore.SERVER_TIMESTAMP
        })
        flash("Registration successful — please log in", "success")
        return redirect(url_for("login"))
    return render_template("register.html")

@app.route("/logout")
def logout():
    session.clear()
    flash("Logged out", "info")
    return redirect(url_for("login"))

# --- Dashboard ---
@app.route("/dashboard")
@login_required
def dashboard():
    return render_template("dashboard.html", username=session.get("username"), role=session.get("role"))

# --- Profile ---
@app.route("/profile")
@login_required
def profile():
    username = session.get("username")
    return render_template("profile.html", username=username, role=session.get("role"))

# --- EduLink: AI Chat ---
@app.route("/edulink")
@login_required
def edulink():
    username = session.get("username")
    user_ref = db.collection("chat_sessions").document(username)
    data = user_ref.get().to_dict() or {}

    # Ensure default session exists
    if not data:
        data["1"] = {"title": "Session 1", "messages": []}
        user_ref.set(data)

    # Get selected session ID from query (?sid=)
    session_id = request.args.get("sid", session.get("current_session", "1"))
    session["current_session"] = session_id 

    # Always guarantee that this session exists
    if session_id not in data:
        data[session_id] = {"title": f"Session {session_id}", "messages": []}
        user_ref.set(data)

    chat_history = data.get(session_id, {}).get("messages", [])
    return render_template("edulink.html", chat_history=chat_history, sessions=data)



# --- API route for streaming AI response ---
@app.route("/edulink/stream", methods=["POST"])
@login_required
def edulink_stream():
    username = session.get("username")
    user_ref = db.collection("chat_sessions").document(username)
    data = user_ref.get().to_dict() or {}
    session_id = session.get("current_session", "1")

    prompt = request.json.get("prompt", "")
    if not prompt:
        return jsonify({"text": ""})

    reply = get_gemini_response(prompt)

    if session_id not in data:
        data[session_id] = {"title": f"Session {session_id}", "messages": []}

    data[session_id]["messages"].append({"role": "user", "content": prompt})
    data[session_id]["messages"].append({"role": "assistant", "content": reply})

    user_ref.set(data)
    return jsonify({"text": reply})



# --- Voice output (TTS) ---
@app.route("/edulink/tts", methods=["POST"])
@login_required
def edulink_tts():
    text = request.form.get("text", "")
    if not text:
        return jsonify({"error": "No text provided"}), 400

    # 🧹 Clean text: remove markdown, HTML, and special chars
    clean_text = re.sub(r"<.*?>", "", text)         # remove HTML tags
    clean_text = re.sub(r"\*\*|__|\*|_", "", clean_text)  # remove markdown
    clean_text = re.sub(r"\s+", " ", clean_text).strip()  # normalize spaces

    try:
        tts = gTTS(text=clean_text, lang="en")
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
        tts.save(tmp.name)
        return send_file(tmp.name, mimetype="audio/mpeg")
    except Exception as e:
        return jsonify({"error": str(e)}), 500
from googletrans import Translator


@app.route("/edulink/translate", methods=["POST"])
@login_required
def edulink_translate():
    text = request.form.get("text", "")
    target_lang = request.form.get("lang", "en")

    if not text or not target_lang:
        return jsonify({"error": "Missing text or target language"}), 400

    try:
        model = genai.GenerativeModel("gemini-2.5-flash-lite-preview-09-2025")
        prompt = f"Translate the following text into {target_lang}:\n\n{text}"
        response = model.generate_content(prompt)

        # ✅ Correct: response.text (not coroutine)
        translated = response.text.strip() if response and hasattr(response, "text") else None
        if not translated:
            raise ValueError("Empty translation")

        return jsonify({"translated": translated})

    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/edulink/new")
@login_required
def edulink_new():
    username = session.get("username")
    user_ref = db.collection("chat_sessions").document(username)
    data = user_ref.get().to_dict() or {}

    # Create a new session ID (next number)
    new_id = str(len(data) + 1)
    data[new_id] = {"title": f"Session {new_id}", "messages": []}

    # Save to Firestore
    user_ref.set(data)

    # Store new session in user session
    session["current_session"] = new_id

    # Redirect to the new session
    return redirect(url_for("edulink", sid=new_id))

# ---- Multi-session support ----
@app.route("/edulink/new_session")
@login_required
def new_session():
    username = session.get("username")
    user_ref = db.collection("chat_sessions").document(username)
    data = user_ref.get().to_dict() or {}
    session_id = str(len(data) + 1)
    data[session_id] = {"title": f"Session {session_id}", "messages": []}
    user_ref.set(data)
    session["current_session"] = session_id
    return redirect(url_for("edulink"))

@app.route("/edulink/load/<session_id>")
@login_required
def load_session(session_id):
    username = session.get("username")
    user_ref = db.collection("chat_sessions").document(username)
    data = user_ref.get().to_dict() or {}
    session["current_session"] = session_id
    messages = data.get(session_id, {}).get("messages", [])
    return render_template("edulink.html", chat_history=messages, sessions=data)

# ---- PDF Download ----
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
import io

@app.route("/download_pdf")
@login_required
def download_pdf():
    username = session.get("username")
    user_ref = db.collection("chat_sessions").document(username)
    data = user_ref.get().to_dict() or {}
    session_id = session.get("current_session", "1")
    chat_data = data.get(session_id, {}).get("messages", [])

    buffer = io.BytesIO()
    p = canvas.Canvas(buffer, pagesize=letter)
    width, height = letter
    y = height - 50
    p.setFont("Helvetica", 11)

    for msg in chat_data:
        role = msg['role'].capitalize()
        text = msg['content']
        for line in text.split('\n'):
            p.drawString(50, y, f"{role}: {line}")
            y -= 15
            if y < 50:
                p.showPage()
                p.setFont("Helvetica", 11)
                y = height - 50
        y -= 10

    p.save()
    buffer.seek(0)
    return send_file(buffer, as_attachment=True, download_name="EduLink_Chat.pdf", mimetype='application/pdf')



# --- Quiz generation ---
@app.route("/quiz", methods=["GET", "POST"])
@login_required
def quiz():
    if request.method == "POST":
        subject = request.form.get("subject", "").strip()
        topic = request.form.get("topic", "").strip()
        num_questions = int(request.form.get("num_questions", 5))
        MODEL = "gemini-2.5-flash"

        if not subject or not topic:
            flash("Please provide both subject and topic.", "warning")
            return redirect(url_for("quiz"))

        try:
            model = genai.GenerativeModel(MODEL)
            prompt = (
                f"Generate a {num_questions}-question multiple-choice quiz on {subject} ({topic}). "
                "Respond ONLY in JSON with this structure:\n\n"
                "{\n"
                '  \"quiz\": [\n'
                '    {\"question\": \"...\", \"options\": [\"A\", \"B\", \"C\", \"D\"], \"answer\": \"A\"}\n'
                "  ]\n"
                "}"
            )

            response = model.generate_content(
                prompt,
                generation_config={
                    "response_mime_type": "application/json",
                    "temperature": 0.4,
                },
            )

            text = (response.text or "").strip()
            quiz_json = json.loads(text)
            quiz = quiz_json.get("quiz", [])

            if not quiz:
                raise ValueError("Empty quiz generated.")

        except Exception as e:
            flash(f"Quiz generation failed: {e}", "danger")
            quiz = None

        # Initialize quiz session
        session["quiz"] = quiz
        session["current_q"] = 0
        session["user_answers"] = [None] * len(quiz)
        session["score"] = 0
        return redirect(url_for("quiz"))

    # GET
    quiz = session.get("quiz")
    current = session.get("current_q", 0)

    # if no quiz generated
    if not quiz:
        return render_template("quiz.html", quiz=None)

    # if quiz completed
    if current >= len(quiz):
        return redirect(url_for("quiz_result"))

    q = quiz[current]
    return render_template("quiz.html", quiz=quiz, current=current, q=q)



@app.route("/quiz/answer", methods=["POST"])
@login_required
def quiz_answer():
    idx = int(request.form.get("index"))
    choice = request.form.get("choice")
    quiz = session.get("quiz", [])
    user_answers = session.get("user_answers", [None] * len(quiz))
    score = session.get("score", 0)

    if quiz and 0 <= idx < len(quiz):
        user_answers[idx] = choice
        correct = quiz[idx].get("answer")

        if choice and choice.strip().lower() == str(correct).strip().lower():
            score += 1

        session["user_answers"] = user_answers
        session["score"] = score
        session["current_q"] = idx + 1

    return redirect(url_for("quiz"))
@app.route("/quiz/new")
@login_required
def quiz_new():
    # Clear previous quiz session
    session.pop("quiz", None)
    session.pop("current_q", None)
    session.pop("user_answers", None)
    session.pop("score", None)
    flash("Start a new quiz!", "info")
    return redirect(url_for("quiz"))

@app.route("/quiz/result")
@login_required
def quiz_result():
    quiz = session.get("quiz", [])
    user_answers = session.get("user_answers", [])
    score = session.get("score", 0)

    results = []
    for i, q in enumerate(quiz):
        ua = user_answers[i] if i < len(user_answers) else None
        correct = q.get("answer")
        results.append({
            "question": q.get("question"),
            "options": q.get("options"),
            "user_answer": ua,
            "correct_answer": correct,
            "is_correct": ua == correct
        })

    total = len(quiz)
    wrong = total - score

    return render_template(
        "quiz_result.html",
        score=score,
        total=total,
        wrong=wrong,
        results=results
    )


# --- Community forum ---
@app.route("/forum", methods=["GET", "POST"])
@login_required
def forum():
    if not db:
        flash("Firestore not initialized", "danger")
        return render_template("forum.html", posts=[])

    if request.method == "POST":
        # Check if this is a new post or a comment
        post_id = request.form.get("post_id")
        comment_text = request.form.get("comment")
        parent_comment_id = request.form.get("parent_id")

        # Create new post
        if not post_id and not comment_text:
            title = request.form.get("title", "").strip()
            content = request.form.get("content", "").strip()
            if title and content:
                post_ref = db.collection("forum_posts").document()
                post_ref.set({
                    "title": title,
                    "content": content,
                    "author": session.get("username"),
                    "role": session.get("role"),
                    "timestamp": firestore.SERVER_TIMESTAMP,
                    "likes": 0
                })
                flash("Post published!", "success")
                return redirect(url_for("forum"))
            else:
                flash("Title and content required", "warning")

        # Create new comment
        elif post_id and comment_text:
            comment_ref = db.collection("forum_posts").document(post_id).collection("comments").document()
            comment_ref.set({
                "author": session.get("username"),
                "text": comment_text.strip(),
                "parent_id": parent_comment_id or None,
                "timestamp": firestore.SERVER_TIMESTAMP
            })
            flash("Comment added!", "success")
            return redirect(url_for("forum"))

    # Fetch posts
    posts_query = db.collection("forum_posts").order_by("timestamp", direction=firestore.Query.DESCENDING).stream()
    posts = []
    for doc in posts_query:
        post = doc.to_dict()
        post["id"] = doc.id

        # Fetch comments for each post
        comments_query = (
            db.collection("forum_posts")
            .document(doc.id)
            .collection("comments")
            .order_by("timestamp")
            .stream()
        )
        comments = []
        for c in comments_query:
            d = c.to_dict()
            d["id"] = c.id
            comments.append(d)
        post["comments"] = comments
        posts.append(post)

    return render_template("forum.html", posts=posts)


@app.route("/forum/like/<post_id>", methods=["POST"])
@login_required
def forum_like(post_id):
    db.collection("forum_posts").document(post_id).update({"likes": firestore.Increment(1)})
    return redirect(url_for("forum"))
# --- Book Explorer ---
import requests

@app.route("/books", methods=["GET", "POST"])
@login_required
def books():
    query = request.form.get("query", "").strip()
    genre = request.args.get("genre", "")
    books = []

    # Use Google Books API
    api_url = "https://www.googleapis.com/books/v1/volumes"
    params = {
        "q": query or f"subject:{genre}" or "bestseller",
        "maxResults": 12
    }

    try:
        response = requests.get(api_url, params=params)
        data = response.json()

        if "items" in data:
            for item in data["items"]:
                info = item.get("volumeInfo", {})
                books.append({
                    "title": info.get("title", "Unknown Title"),
                    "authors": ", ".join(info.get("authors", ["Unknown Author"])),
                    "thumbnail": info.get("imageLinks", {}).get("thumbnail", ""),
                    "description": info.get("description", "No description available."),
                    "rating": info.get("averageRating", "N/A"),
                    "categories": ", ".join(info.get("categories", []))
                })
    except Exception as e:
        print("Book API error:", e)

    genres = ["Fiction", "Science", "Technology", "History", "Romance", "Fantasy", "Biography", "Mystery", "Self-help"]

    return render_template("books.html", books=books, genres=genres, query=query)

# --- AI Media Summarizer ---
@app.route("/media_summarizer", methods=["GET", "POST"])
@login_required
def media_summarizer():
    summary = None
    error = None

    if request.method == "POST":
        media_type = request.form.get("media_type")
        video_url = request.form.get("video_url", "").strip()
        file = request.files.get("file")

        try:
            model = genai.GenerativeModel("gemini-2.5-flash-lite-preview-09-2025")

            # 🧠 AI System Instruction
            system_instruction = (
                "You are an intelligent media summarizer. Your job is to summarize the given video, audio, or image "
                "content in clear, structured points that highlight key details, context, and insights in organized manner."
                "If the input is a YouTube link, generate a concise summary of its content as if you had watched it. "
                "Keep tone educational, clear, and accurate."
            )

            prompt = system_instruction

            # 🔹 Case 1: YouTube video summarization
            if media_type == "video" and video_url:
                prompt += f"\n\nUser provided YouTube video link:\n{video_url}\nPlease summarize the key insights."

            # 🔹 Case 2: File upload (audio/image)
            elif media_type in ["audio", "image"] and file:
                file_path = os.path.join(tempfile.gettempdir(), secure_filename(file.filename))
                file.save(file_path)
                uploaded_file = genai.upload_file(file_path)
                response = model.generate_content(
                    [prompt, uploaded_file],
                    generation_config={"temperature": 0.4}
                )
                summary = response.text.strip()
                os.remove(file_path)
                return render_template("media_summarizer.html", summary=summary)

            else:
                error = "Please provide a YouTube URL or upload a file."

            # 🔹 Generate for text input (video URL)
            response = model.generate_content(prompt)
            summary = response.text.strip()

        except Exception as e:
            error = f"Error: {e}"

    return render_template("media_summarizer.html", summary=summary, error=error)


# start app
if __name__ == "__main__":
    app.run(debug=True)
