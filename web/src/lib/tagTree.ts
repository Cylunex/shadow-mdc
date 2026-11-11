export type TreeNode = { id: string; name: string; parent_id?: string | null; aliases?: string[]; redirect_ids?: string[] };
export type TreeRow<T extends TreeNode> = { node: T; path: T[]; depth: number; hasChildren: boolean };

/** Preserve sibling ordering, tolerate old root-only responses, and bound malformed external trees. */
export function tagTreeRows<T extends TreeNode>(nodes: T[]): TreeRow<T>[] {
  const indexed = new Map(nodes.map(node => [node.id, node]));
  const children = new Map<string, T[]>();
  const roots: T[] = [];
  for (const node of nodes) {
    if (node.parent_id && node.parent_id !== node.id && indexed.has(node.parent_id)) {
      const siblings = children.get(node.parent_id) ?? [];
      siblings.push(node); children.set(node.parent_id, siblings);
    } else roots.push(node);
  }
  const result: TreeRow<T>[] = [];
  const seen = new Set<string>();
  const visit = (root: T) => {
    const stack: { node: T; path: T[] }[] = [{ node: root, path: [] }];
    while (stack.length) {
      const { node, path } = stack.pop()!;
      if (seen.has(node.id) || path.length >= 64) continue;
      seen.add(node.id);
      const branch = [...path, node];
      const nested = children.get(node.id) ?? [];
      result.push({ node, path: branch, depth: path.length, hasChildren: nested.length > 0 });
      for (const child of [...nested].reverse()) stack.push({ node: child, path: branch });
    }
  };
  for (const root of roots) visit(root);
  for (const node of nodes) if (!seen.has(node.id)) visit(node);
  return result;
}

export const tagPathLabel = (row: TreeRow<TreeNode>) => row.path.map(node => node.name).join(" › ");
