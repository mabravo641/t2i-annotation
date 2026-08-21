import { dom } from "./dom.js";

export function showStatus(message, isError = false) {
  dom.statusBanner.textContent = message;
  dom.statusBanner.style.color = isError ? "#b42318" : "#03543f";
}
