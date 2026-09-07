type Props = {
  handle?: string | null;
  url?: string | null;
  className?: string;
};

export function XHandleLink({ handle, url, className }: Props) {
  if (!handle && !url) return null;
  const href = url || (handle ? `https://x.com/${handle.replace(/^@/, "")}` : null);
  if (!href) return null;
  const label = handle ? (handle.startsWith("@") ? handle : `@${handle}`) : "X";
  return (
    <a className={className ?? "x-handle-link"} href={href} target="_blank" rel="noreferrer noopener">
      {label}
    </a>
  );
}
