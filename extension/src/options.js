import { getBackend, setBackend } from "./config.js";

const input = document.getElementById("backend");
const status = (t) => { document.getElementById("status").textContent = t; };
getBackend().then((b) => { input.value = b; });

document.getElementById("save").onclick = async () => {
  try {
    const origin = new URL(input.value.trim()).origin;
    // Loopback is in the manifest; any other backend needs a host permission,
    // which Chrome only grants from a user gesture like this click.
    const loopback = /^https?:\/\/(127\.0\.0\.1|localhost)(:\d+)?$/.test(origin);
    if (!loopback && !(await chrome.permissions.request({ origins: [origin + "/*"] }))) {
      status("Chrome did not grant access to that address, so it was not saved.");
      return;
    }
    input.value = await setBackend(origin);
    status("Saved.");
  } catch (err) {
    status(err.message);
  }
};
