export type WorkStatusFlags = {
  wantList: boolean;
  inCatalog: boolean;
  hasLocalOrEmby: boolean;
};

export function computeWorkStatus(input: {
  wantList: boolean;
  inCatalog: boolean;
  hasLocalMedia: boolean;
  embyLinked?: boolean;
}): WorkStatusFlags {
  return {
    wantList: input.wantList,
    inCatalog: input.inCatalog,
    hasLocalOrEmby: input.hasLocalMedia || Boolean(input.embyLinked)
  };
}

export function statusBadges(flags: WorkStatusFlags): string[] {
  const badges: string[] = [];
  if (flags.wantList) badges.push("想看");
  if (flags.inCatalog) badges.push("已入库");
  if (flags.hasLocalOrEmby) badges.push("有本地或Emby");
  return badges;
}
