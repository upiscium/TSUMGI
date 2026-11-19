"use strict";
// First-run password setup. Messages are only ever set as text.

const form = document.getElementById("setup-form");
const errorBox = document.getElementById("setup-error");
const button = document.getElementById("setup-button");

function fail(message) {
  errorBox.textContent = message;
  errorBox.hidden = false;
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  errorBox.hidden = true;
  const password = document.getElementById("setup-pass").value;
  if (password !== document.getElementById("setup-pass2").value) {
    fail("パスワードが一致しません");
    return;
  }
  button.disabled = true;
  try {
    const response = await fetch("/admin/api/setup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({
        code: document.getElementById("setup-code").value,
        username: document.getElementById("setup-user").value,
        password,
      }),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      fail(body.error || `設定に失敗しました (${response.status})`);
      return;
    }
    form.hidden = true;
    document.getElementById("setup-done").hidden = false;
    setTimeout(() => location.assign("/admin"), 1500);
  } catch (error) {
    fail("Control に接続できません");
  } finally {
    button.disabled = false;
  }
});
