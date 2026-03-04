// small, clear frontend logic to call our new endpoints
const uploadForm = document.getElementById("uploadAndAskForm");
const pdfFile = document.getElementById("pdfFile");
const initialPrompt = document.getElementById("initialPrompt");
const chatForm = document.getElementById("chatForm");
const chatInput = document.getElementById("chatInput");
const chatBox = document.getElementById("chatBox");
const summaryBox = document.getElementById("summaryBox");
const termsBox = document.getElementById("termsBox");
const modal = document.getElementById("modal");
const modalTitle = document.getElementById("modalTitle");
const modalBody = document.getElementById("modalBody");
const modalClose = document.getElementById("modalClose");

let CURRENT_SESSION = null;

// helpers
function addMessage(who, html) {
  const d = document.createElement("div");
  d.className = `msg ${who==="user"?"user":"bot"}`;
  d.innerHTML = html;
  chatBox.appendChild(d);
  chatBox.scrollTop = chatBox.scrollHeight;
}
function setSummary(text) {
  summaryBox.innerText = text || "No summary available.";
}
function setTerms(terms) {
  termsBox.innerHTML = "";
  if (!terms || terms.length===0) {
    termsBox.innerHTML = "<div class='muted'>No complex terms detected.</div>";
    return;
  }
  terms.forEach(t => {
    const chip = document.createElement("div");
    chip.className = "term-chip";
    chip.innerText = t.term;
    chip.onclick = () => showModal(t.term, t.explanation);
    termsBox.appendChild(chip);
  });
}
function showModal(title, body) {
  modalTitle.innerText = title;
  modalBody.innerText = body;
  modal.classList.remove("hidden");
}
modalClose.onclick = () => modal.classList.add("hidden");

// upload+ask form
uploadForm.addEventListener("submit", async (e)=> {
  e.preventDefault();
  if (!pdfFile.files[0]) return alert("Please pick a PDF file");
  const prompt = initialPrompt.value.trim() || "Give me a simple summary and then answer briefly.";
  addMessage("user", `<strong>Question:</strong> ${escapeHtml(prompt)}`);

  const fd = new FormData();
  fd.append("file", pdfFile.files[0]);
  fd.append("prompt", prompt);

  addMessage("bot", "Reading PDF and preparing summary...");

  try {
    const res = await fetch("/api/upload_and_ask", { method:"POST", body: fd });
    const data = await res.json();
    if (data.error) {
      addMessage("bot", `<span style="color:#c33">Error: ${escapeHtml(data.error)}</span>`);
      return;
    }
    CURRENT_SESSION = data.session_id;
    // replace last bot message with summary+answer
    // simple: append summary & answer
    addMessage("bot", `<strong>First answer:</strong><div class="muted">${escapeHtml(data.first_answer)}</div>`);
    setSummary(data.summary);
    setTerms(data.term_explanations || []);
  } catch (err) {
    addMessage("bot", `<span style="color:#c33">Network error: ${escapeHtml(err.message)}</span>`);
  }
});

// follow-up chat
chatForm.addEventListener("submit", async (e)=> {
  e.preventDefault();
  const q = chatInput.value.trim();
  if (!q) return;
  if (!CURRENT_SESSION) { addMessage("bot", "Please upload a PDF first."); return; }
  addMessage("user", escapeHtml(q));
  chatInput.value = "";

  addMessage("bot", "Thinking...");

  const fd = new FormData();
  fd.append("session_id", CURRENT_SESSION);
  fd.append("message", q);

  try {
    const res = await fetch("/api/ask", {method:"POST", headers: {'Content-Type':'application/json'}, body: JSON.stringify({ session_id: CURRENT_SESSION, message: q })});
    const data = await res.json();
    if (data.error) {
      addMessage("bot", `<span style="color:#c33">Error: ${escapeHtml(data.error)}</span>`);
      return;
    }
    addMessage("bot", `<div class="muted">${escapeHtml(data.answer)}</div>`);
  } catch (err) {
    addMessage("bot", `<span style="color:#c33">Network error: ${escapeHtml(err.message)}</span>`);
  }
});

// small util
function escapeHtml(s){ return (s||"").replace(/[&<>'"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":"&#39;",'"':'&quot;'}[c])); }
