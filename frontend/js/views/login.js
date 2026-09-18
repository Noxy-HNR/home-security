// Sign-in screen (one household password).

import { api } from "../api.js";
import { html, icon, setHTML } from "../util.js";

export function mountLogin(root, { demo = false, onSuccess }) {
  setHTML(root, html`
    <div class="login">
      <form class="card" novalidate>
        <h1>${icon("shieldCheck")}Home Security</h1>
        <label class="small muted" for="pw">Dashboard password</label>
        <input class="input" id="pw" name="password" type="password" autocomplete="current-password" required autofocus>
        <p class="error" role="alert"></p>
        <button class="btn primary" type="submit">Sign in</button>
        ${demo ? html`<p class="small muted">Demo mode: nothing here is real. The password is the <span class="mono">DASHBOARD_PASSWORD</span> you started it with.</p>` : ""}
      </form>
    </div>`);
  const form = root.querySelector("form");
  const error = root.querySelector(".error");
  const button = form.querySelector("button");
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const password = form.password.value;
    if (!password) { error.textContent = "Enter the password."; return; }
    button.classList.add("busy");
    button.disabled = true;
    try {
      await api("/api/auth/login", { method: "POST", body: { password } });
      onSuccess();
    } catch (err) {
      error.textContent = err.status === 401 ? "Wrong password." : err.message;
      form.password.select();
    } finally {
      button.classList.remove("busy");
      button.disabled = false;
    }
  });
  root.querySelector("#pw").focus();
}
