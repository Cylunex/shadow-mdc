import { useState, type SyntheticEvent } from "react";

export type CoverShape = "landscape" | "wide" | "square" | "portrait";

/**
 * Classify a cover by its natural aspect ratio (idea from JavdbEmbySkin / JavDB):
 * - wide (>= 1.6, ~16:9) fills the landscape card;
 * - landscape (JAV back+front spread, ~1.42–1.6) shown whole, blurred backdrop fills slack;
 * - square (VR / FC2, 0.85–1.42) and portrait (DMM `ps` front) shown whole, centred on a blurred copy.
 */
export function classifyCover(width: number, height: number): CoverShape {
  if (!width || !height) return "landscape";
  const r = width / height;
  if (r >= 1.6) return "wide";
  if (r >= 1.42) return "landscape";
  if (r >= 0.85) return "square";
  return "portrait";
}

type Props = {
  src: string;
  className?: string;
  /** Portrait slot: show only the right half (front art) of a landscape JAV spread. */
  frontCrop?: boolean;
  loading?: "lazy" | "eager";
};

/** Whole-image cover: never hard-cropped or stretched; blurred same-image backdrop instead of black bars. */
export function CoverImage({ src, className, frontCrop, loading = "lazy" }: Props) {
  const [shape, setShape] = useState<CoverShape | null>(null);
  const [broken, setBroken] = useState(false);
  if (broken) return <div className="poster-fallback" aria-hidden />;
  const onLoad = (event: SyntheticEvent<HTMLImageElement>) => {
    const img = event.currentTarget;
    setShape(classifyCover(img.naturalWidth, img.naturalHeight));
  };
  const crop = frontCrop && (shape === "landscape" || shape === "wide");
  return (
    <div
      className={["cover-frame", className].filter(Boolean).join(" ")}
      data-shape={shape ?? "pending"}
      data-crop={crop ? "front" : undefined}
    >
      <img className="cover-bg" src={src} alt="" aria-hidden loading={loading} decoding="async" />
      <img
        className="cover-main"
        src={src}
        alt=""
        loading={loading}
        decoding="async"
        onLoad={onLoad}
        onError={() => setBroken(true)}
      />
    </div>
  );
}
