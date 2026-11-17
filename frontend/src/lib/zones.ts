/** 分区（Zones）前端共享常量与工具。 */

export const ZONE_COLOR_PALETTE = [
  "#4FD8FF",
  "#A78BFA",
  "#FBBF24",
  "#34D399",
  "#FB7185",
  "#60A5FA",
  "#F472B6",
  "#2DD4BF",
] as const;

export const UNASSIGNED_ZONE_ID = "unassigned";
export const UNASSIGNED_ZONE_NAME = "未分区";
export const UNASSIGNED_ZONE_COLOR = "#5B6F8C";

/** 把 zone_id（null 表示未分区）转成 chips/URL 使用的字符串 token。 */
export function zoneToken(zoneId: string | null | undefined): string {
  return zoneId ?? UNASSIGNED_ZONE_ID;
}

/** 从 token 解析回 API 的 zone_id（未分区为 null）。 */
export function zoneIdFromToken(token: string): string | null {
  return token === UNASSIGNED_ZONE_ID ? null : token;
}

/** 节点 id 形如 concept:cv_1@z_ab12，返回其 @ 后缀区域 id（无后缀返回 undefined）。 */
export function zoneSuffixFromNodeId(nodeId: string | null | undefined): string | undefined {
  if (!nodeId) return undefined;
  const index = nodeId.lastIndexOf("@");
  if (index === -1) return undefined;
  const suffix = nodeId.slice(index + 1);
  return suffix === UNASSIGNED_ZONE_ID ? UNASSIGNED_ZONE_ID : suffix;
}

export function zoneLabel(zoneId: string | null | undefined, zoneName?: string | null): string {
  if (zoneId == null) return zoneName || UNASSIGNED_ZONE_NAME;
  return zoneName || zoneId;
}

/** 当文档列表没有带区域颜色时，用 zone_id 稳定映射到色板颜色。 */
export function zoneColorFromId(zoneId: string | null | undefined): string {
  if (!zoneId) return UNASSIGNED_ZONE_COLOR;
  let hash = 0;
  for (let index = 0; index < zoneId.length; index += 1) {
    hash = (hash * 31 + zoneId.charCodeAt(index)) >>> 0;
  }
  return ZONE_COLOR_PALETTE[hash % ZONE_COLOR_PALETTE.length];
}
