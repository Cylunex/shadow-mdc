type Props = {
  handle?: string | null;
  url?: string | null;
  className?: string;
};

/** Only render a clickable X link when a verified handle (and preferably url) is present. */
export function XHandleLink({ handle, url, className }: Props) {
  if (!handle) return null;
  const bare = handle.replace(/^@/, "").trim();
  if (!bare) return null;
  const href = url || `https://x.com/${bare}`;
  const label = handle.startsWith("@") ? handle : `@${bare}`;
  return (
    <a className={className ?? "x-handle-link"} href={href} target="_blank" rel="noreferrer noopener">
      {label}
    </a>
  );
}
