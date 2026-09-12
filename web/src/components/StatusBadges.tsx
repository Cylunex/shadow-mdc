import { memo } from "react";
import { computeWorkStatus, statusBadges } from "../lib/workStatus";

type Props = {
  wantList?: boolean;
  inCatalog?: boolean;
  hasLocalMedia?: boolean;
  embyLinked?: boolean;
};

export const StatusBadges = memo(function StatusBadges(props: Props) {
  const badges = statusBadges(
    computeWorkStatus({
      wantList: Boolean(props.wantList),
      inCatalog: props.inCatalog !== false,
      hasLocalMedia: Boolean(props.hasLocalMedia),
      embyLinked: props.embyLinked
    })
  );
  if (badges.length === 0) return null;
  return (
    <div className="status-badges">
      {badges.map((badge) => (
        <span key={badge} className={`status-badge status-${badge}`}>{badge}</span>
      ))}
    </div>
  );
});
