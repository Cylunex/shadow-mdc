/**
 * Copy text safely across both HTTPS/localhost Secure Contexts and LAN HTTP
 * deployments (`http://192.168.x.x`), where `navigator.clipboard` is undefined.
 */
export async function copyToClipboard(value: string): Promise<boolean> {
  if (!value) return false;
  try {
    if (typeof navigator !== "undefined" && navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
      await navigator.clipboard.writeText(value);
      return true;
    }
  } catch {
    // Fall back to execCommand when Clipboard API rejects or is blocked.
  }
  if (typeof document === "undefined" || !document.body) return false;
  const textarea = document.createElement("textarea");
  textarea.value = value;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.top = "-9999px";
  textarea.style.left = "-9999px";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);
  const selection = document.getSelection();
  const savedRange = selection && selection.rangeCount > 0 ? selection.getRangeAt(0) : null;
  try {
    textarea.focus();
    textarea.select();
    textarea.setSelectionRange(0, value.length);
    return typeof document.execCommand === "function" && document.execCommand("copy");
  } catch {
    return false;
  } finally {
    textarea.remove();
    if (selection && savedRange) {
      try {
        selection.removeAllRanges();
        selection.addRange(savedRange);
      } catch {
        // Ignore selection restoration errors.
      }
    }
  }
}
