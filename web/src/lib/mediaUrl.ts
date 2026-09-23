import { appUrl } from "../api";

/** Resolve a list/detail media path; null when empty. */
export function mediaUrl(path: string | null | undefined): string | null {
  if (!path) return null;
  return appUrl(path);
}

/** Hide broken <img> (404 poster, hotlink failure) — empty shell, never a fake avatar. */
export function hideBrokenImage(event: { currentTarget: HTMLImageElement }): void {
  const img = event.currentTarget;
  img.style.display = "none";
  img.removeAttribute("src");
  img.onerror = null;
}
