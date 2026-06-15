const msalInstance = new msal.PublicClientApplication(msalConfig);

let currentAccount = null;

// In-memory chat history for THIS session only. Intentionally NOT persisted to
// localStorage / sessionStorage / cookies — a page refresh clears it by design.
const chatHistory = [];

async function initialize() {
  await msalInstance.initialize();

  const response = await msalInstance.handleRedirectPromise();
  if (response) {
    currentAccount = response.account;
    updateUI();
  } else {
    const accounts = msalInstance.getAllAccounts();
    if (accounts.length > 0) {
      currentAccount = accounts[0];
      updateUI();
    }
  }
}

async function signIn() {
  try {
    const response = await msalInstance.loginPopup({ scopes: agentCoreScopes });
    currentAccount = response.account;
    updateUI();
  } catch (err) {
    showError("Sign-in failed: " + err.message);
  }
}

function signOut() {
  msalInstance.logoutPopup({ account: currentAccount }).then(() => {
    currentAccount = null;
    updateUI();
  }).catch(err => showError("Sign-out failed: " + err.message));
}

async function getToken() {
  const request = { scopes: agentCoreScopes, account: currentAccount };
  try {
    const response = await msalInstance.acquireTokenSilent(request);
    return response.accessToken;
  } catch (err) {
    if (err instanceof msal.InteractionRequiredAuthError) {
      const response = await msalInstance.acquireTokenPopup(request);
      return response.accessToken;
    }
    throw err;
  }
}

async function invokeAgent(message) {
  // Show the user's message immediately, then a placeholder agent bubble.
  appendUserMessage(message);
  const placeholder = appendAgentPlaceholder();

  try {
    const token = await getToken();

    const res = await fetch(agentCoreEndpoint, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + token
      },
      body: JSON.stringify({ message })
    });

    if (!res.ok) {
      const text = await res.text();
      throw new Error(`HTTP ${res.status}: ${text}`);
    }

    const data = await res.json();
    renderAgentResponse(placeholder, data);
  } catch (err) {
    renderAgentError(placeholder, "Agent invocation failed: " + err.message);
  }
}

/* ------------------------------------------------------------------ *
 * Rendering pipeline: status icon + markdown render + sanitize +
 * <thinking> handling, appended to the scroll box.
 * ------------------------------------------------------------------ */

// Derive a Bootstrap-Icons status glyph + colour from the agent's `status` field.
function statusBadge(status) {
  const s = String(status || "").toLowerCase();
  if (s === "success" || s === "ok" || s === "succeeded") {
    return { icon: "bi-check-circle-fill", cls: "text-success", label: status || "success" };
  }
  if (s === "error" || s === "failure" || s === "failed" || s === "fail") {
    return { icon: "bi-x-circle-fill", cls: "text-danger", label: status || "error" };
  }
  return { icon: "bi-info-circle-fill", cls: "text-secondary", label: status || "unknown" };
}

// Split out any <thinking>…</thinking> reasoning from the visible answer.
// Robust to: no block, multiple blocks, and an unclosed/malformed tag. Never
// leaks a raw <thinking> tag into the visible answer.
function extractThinking(text) {
  let answer = String(text == null ? "" : text);
  const blocks = [];

  // 1) Well-formed (possibly multiple) blocks.
  answer = answer.replace(/<thinking>([\s\S]*?)<\/thinking>/gi, (_m, inner) => {
    const t = inner.trim();
    if (t) blocks.push(t);
    return "";
  });

  // 2) An unclosed <thinking> with no matching </thinking>: treat the remainder
  //    as reasoning and drop it from the answer.
  const openIdx = answer.search(/<thinking>/i);
  if (openIdx !== -1) {
    const t = answer.slice(openIdx).replace(/<\/?thinking>/gi, "").trim();
    if (t) blocks.push(t);
    answer = answer.slice(0, openIdx);
  }

  // 3) Belt-and-suspenders: strip any stray opening/closing tags left over.
  answer = answer.replace(/<\/?thinking>/gi, "").trim();

  return {
    reasoning: blocks.length ? blocks.join("\n\n---\n\n") : null,
    answer
  };
}

// Render markdown to HTML and ALWAYS sanitize before it touches the DOM.
// Model-generated text is untrusted input — never inject it un-sanitized.
function renderMarkdownSafe(md) {
  const rawHtml = marked.parse(String(md == null ? "" : md), { gfm: true, breaks: true });
  return DOMPurify.sanitize(rawHtml);
}

function nowTime() {
  return new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function scrollToLatest() {
  const box = document.getElementById("chat-history");
  if (box) box.scrollTop = box.scrollHeight;
}

/* ------------------------------------------------------------------ *
 * Message input auto-grow.
 * The #message-input is a <textarea rows="1"> that LOOKS like a single
 * line by default and grows with content (newlines or wrapping) up to a
 * cap, after which an internal scrollbar appears. Keep this constant in
 * sync with the `max-height` on #message-input in index.html.
 * ------------------------------------------------------------------ */
const MAX_INPUT_HEIGHT = 140; // px — ~5–6 rows before the internal scrollbar kicks in

// Resize the textarea to fit its content, clamped to MAX_INPUT_HEIGHT.
function autoGrowInput(el) {
  if (!el) return;
  el.style.height = "auto"; // reset so scrollHeight reflects the true content height
  const cs = getComputedStyle(el);
  // box-sizing is border-box (Bootstrap default); scrollHeight excludes the
  // border, so add it back to avoid a 1–2px phantom scrollbar.
  const borderY = parseFloat(cs.borderTopWidth) + parseFloat(cs.borderBottomWidth);
  const needed = el.scrollHeight + borderY;
  if (needed > MAX_INPUT_HEIGHT) {
    el.style.height = MAX_INPUT_HEIGHT + "px";
    el.style.overflowY = "auto"; // content exceeds the cap → show internal scrollbar
  } else {
    el.style.height = needed + "px";
    el.style.overflowY = "hidden";
  }
}

// Collapse the textarea back to a single visual line (used after a send).
function resetInputHeight(el) {
  if (!el) return;
  el.style.height = "auto";
  el.style.overflowY = "hidden";
}

// Insert a newline at the caret and keep the caret after it. Needed for
// Ctrl+Enter, which (unlike Shift+Enter) does NOT insert a newline natively.
function insertNewlineAtCaret(el) {
  const start = el.selectionStart;
  const end = el.selectionEnd;
  const v = el.value;
  el.value = v.slice(0, start) + "\n" + v.slice(end);
  el.selectionStart = el.selectionEnd = start + 1;
}

// Append a right-aligned user bubble.
function appendUserMessage(text) {
  chatHistory.push({ role: "user", text, ts: Date.now() });
  const list = document.getElementById("chat-history-list");
  const li = document.createElement("li");
  li.className = "clearfix mine";
  li.innerHTML =
    '<div class="message-data">' +
      '<span class="message-data-time">' + escapeHtml(nowTime()) + "</span>" +
      '<i class="bi bi-person-circle avatar-glyph"></i>' +
    "</div>" +
    '<div class="message my-message"></div>';
  // User text is plain text — set via textContent (never innerHTML).
  li.querySelector(".my-message").textContent = text;
  list.appendChild(li);
  scrollToLatest();
}

// Append a left-aligned agent bubble in a "thinking" state; returns the bubble
// element so the response can replace its contents in place.
function appendAgentPlaceholder() {
  const list = document.getElementById("chat-history-list");
  const li = document.createElement("li");
  li.className = "clearfix other";
  li.innerHTML =
    '<div class="message-data">' +
      '<i class="bi bi-robot avatar-glyph"></i>' +
      '<span class="message-data-time">' + escapeHtml(nowTime()) + "</span>" +
    "</div>" +
    '<div class="message other-message">' +
      '<span class="text-muted"><span class="spinner-grow spinner-grow-sm"></span> Thinking…</span>' +
    "</div>";
  list.appendChild(li);
  scrollToLatest();
  return li.querySelector(".other-message");
}

// Fill an agent bubble with the formatted response.
function renderAgentResponse(bubble, data) {
  // Response-shape fallback: agent returns { status, response }. Older/error
  // shapes used reply / message / error — support them all (fixes the cosmetic
  // bug where the SPA read data.reply ?? data.message but the agent sends response).
  const responseText =
    data.response ?? data.reply ?? data.message ?? data.error ??
    JSON.stringify(data, null, 2);

  const status = data.status || (data.error ? "error" : "success");
  const badge = statusBadge(status);

  const { reasoning, answer } = extractThinking(responseText);

  const headerHtml =
    '<i class="bi ' + badge.icon + " status-badge " + badge.cls + '" title="' +
    escapeHtml(badge.label) + '"></i>' +
    '<span class="text-muted" style="font-size:0.8rem;">' + escapeHtml(badge.label) + "</span>";

  // Answer is model-generated → markdown render + sanitize.
  const answerHtml = renderMarkdownSafe(answer || "_(empty response)_");

  let reasoningHtml = "";
  if (reasoning) {
    reasoningHtml =
      '<details class="reasoning">' +
        "<summary>Show reasoning</summary>" +
        '<div class="reasoning-body">' + renderMarkdownSafe(reasoning) + "</div>" +
      "</details>";
  }

  bubble.innerHTML =
    '<div class="mb-2">' + headerHtml + "</div>" +
    '<div class="answer-body">' + answerHtml + "</div>" +
    reasoningHtml;

  chatHistory.push({ role: "agent", status, text: responseText, ts: Date.now() });
  scrollToLatest();
}

// Fill an agent bubble with an error state.
function renderAgentError(bubble, msg) {
  bubble.innerHTML =
    '<i class="bi bi-x-circle-fill status-badge text-danger"></i>' +
    '<span class="text-danger"></span>';
  bubble.querySelector(".text-danger").textContent = msg;
  chatHistory.push({ role: "agent", status: "error", text: msg, ts: Date.now() });
  scrollToLatest();
}

function updateUI() {
  const signedIn = !!currentAccount;

  document.getElementById("btn-signin").style.display = signedIn ? "none" : "inline-block";
  document.getElementById("btn-signout").style.display = signedIn ? "inline-block" : "none";
  document.getElementById("chat-section").style.display = signedIn ? "block" : "none";

  const userInfo = document.getElementById("user-info");
  if (signedIn) {
    userInfo.textContent = "Signed in as: " + (currentAccount.name || currentAccount.username);
    clearAuthAlert();
  } else {
    userInfo.textContent = "";
    // No persistence: clear the in-memory + DOM history on sign-out.
    chatHistory.length = 0;
    const list = document.getElementById("chat-history-list");
    if (list) list.innerHTML = "";
  }
}

// Sign-in / sign-out failures surface in a dismissible alert above the chat.
function showError(msg) {
  const alert = document.getElementById("auth-alert");
  if (alert) {
    alert.textContent = "⚠️ " + msg;
    alert.classList.remove("d-none");
  }
}

function clearAuthAlert() {
  const alert = document.getElementById("auth-alert");
  if (alert) {
    alert.textContent = "";
    alert.classList.add("d-none");
  }
}

document.addEventListener("DOMContentLoaded", () => {
  initialize();

  document.getElementById("btn-signin").addEventListener("click", signIn);
  document.getElementById("btn-signout").addEventListener("click", signOut);

  const messageInput = document.getElementById("message-input");

  document.getElementById("btn-send").addEventListener("click", () => {
    const msg = messageInput.value.trim();
    if (msg) {
      messageInput.value = "";
      resetInputHeight(messageInput); // collapse back to a single line after send
      invokeAgent(msg);
    }
  });

  // Auto-grow as the user types (handles newlines AND long wrapping lines).
  messageInput.addEventListener("input", () => autoGrowInput(messageInput));

  // Key contract:
  //   Enter (no modifier)        → send
  //   Ctrl+Enter / Shift+Enter   → insert a newline (do NOT send)
  messageInput.addEventListener("keydown", e => {
    if (e.key !== "Enter") return;

    if (e.ctrlKey || e.shiftKey) {
      // Newline, not send. Shift+Enter inserts a newline natively (the `input`
      // event then auto-grows). Ctrl+Enter has no native newline, so insert it
      // manually and grow explicitly.
      if (e.ctrlKey && !e.shiftKey) {
        e.preventDefault();
        insertNewlineAtCaret(messageInput);
        autoGrowInput(messageInput);
      }
      return;
    }

    // Plain Enter → send.
    e.preventDefault();
    document.getElementById("btn-send").click();
  });
});
