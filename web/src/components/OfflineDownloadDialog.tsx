import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api";
import type { PanOfflineTask, PanStatus } from "../model";
import { queryClient } from "../query/client";
import { resources } from "../query/resources";

type Folder = { id: string; name: string };
type FolderPage = { folders: Folder[]; hasNext: boolean };

export function offlineDestinationReady(pan: PanStatus | null | undefined, directory: string): boolean {
  if (!pan?.connected) return false;
  const value = directory.trim();
  if (!value) return Boolean(pan.offline_target ?? (pan.backend === "openlist" ? null : pan.offline_directory_id));
  if (pan.backend !== "openlist") return /^[0-9]{1,64}$/.test(value);
  return value.startsWith("/") && !value.includes("\\") &&
    !value.split("/").includes("..") && value.split("/").some((part) => part && part !== ".");
}

export function offlineDirectoryLabel(task: PanOfflineTask): string {
  return task.backend === "openlist" ? task.directory_id : `115 目录 ID ${task.directory_id}`;
}

export function offlineTaskLabel(task: PanOfflineTask): string {
  switch (task.download_state) {
    case "verifying": return "下载完成 · 正在核对文件";
    case "cancelling": return "正在取消";
    case "switching":
    case "submitting": return "正在切换磁力";
    case "stalled": return "下载停滞";
  }
  if (task.status === "cancelled") return "已取消";
  if (task.status === "failed") return "离线失败";
  if (task.strm_path) return "STRM 已生成";
  if (task.status === "done" || task.status === "completed") {
    return task.error ? "下载完成 · 入库待处理" : "下载完成";
  }
  return `离线中 ${Math.round(task.progress)}%`;
}

export function OfflineDirectoryPicker({ pan, value, onChange, disabled }: {
  pan: PanStatus;
  value: string;
  onChange: (directory: string) => void;
  disabled: boolean;
}) {
  const openlist = pan.backend === "openlist";
  const root = openlist ? "/" : "0";
  const defaultTarget = pan.offline_target ?? (openlist ? null : pan.offline_directory_id);
  const [trail, setTrail] = useState<Folder[] | null>(null);
  const [page, setPage] = useState(1);
  const current = trail?.at(-1);
  const listing = useQuery({
    queryKey: ["mdc", "offline-directory", openlist, current?.id, page],
    enabled: Boolean(current && pan.connected),
    retry: false,
    async queryFn({ signal }): Promise<FolderPage> {
      const id = current?.id ?? root;
      if (openlist) {
        const data = await api.openListFiles(id, page, signal);
        return {
          folders: data.items.filter((item) => item.is_directory).map((item) => ({ id: item.path, name: item.name })),
          hasNext: page * 100 < data.total,
        };
      }
      const data = await api.panFiles(id, page, signal);
      return {
        folders: data.items.filter((item) => item.is_directory).map((item) => ({ id: item.id, name: item.name })),
        hasNext: page * data.limit < data.total,
      };
    },
  }, queryClient);

  return (
    <fieldset className="offline-directory-picker" disabled={disabled || !pan.connected}>
      <label>
        <span>下载到{openlist ? "（OpenList 路径）" : "（115 目录 ID）"}</span>
        <input
          value={value}
          onChange={(event) => onChange(event.target.value)}
          maxLength={openlist ? 2048 : 64}
          inputMode={openlist ? "text" : "numeric"}
          placeholder={defaultTarget || (openlist ? "/网盘/目录" : "填写目录 ID")}
        />
      </label>
      <div className="offline-directory-actions">
        <small className="muted">{defaultTarget ? `留空使用默认目录 ${defaultTarget}` : "请为本次任务选择目录"}</small>
        <button type="button" className="ghost" aria-expanded={Boolean(trail)} onClick={() => {
          setTrail(trail ? null : [{ id: root, name: "根目录" }]);
          setPage(1);
        }}>{trail ? "收起目录" : "浏览目录"}</button>
      </div>
      {current && trail && (
        <div className="offline-directory-browser">
          <div className="offline-directory-actions">
            <strong className="offline-directory-path">{openlist ? current.id : trail.map((folder) => folder.name).join(" / ")}</strong>
            {trail.length > 1 && (
              <button type="button" className="ghost" onClick={() => {
                setTrail(trail.slice(0, -1));
                setPage(1);
              }}>上一级</button>
            )}
            {(!openlist || current.id !== root) && listing.isSuccess && (
              <button type="button" className="secondary" onClick={() => {
                onChange(current.id);
                setTrail(null);
              }}>选用此目录</button>
            )}
          </div>
          {listing.isPending && <small role="status">正在加载目录…</small>}
          {listing.isError && (
            <div role="alert" className="offline-directory-actions">
              <small className="danger-text">{listing.error.message}</small>
              <button type="button" className="ghost" onClick={() => void listing.refetch()}>重试</button>
            </div>
          )}
          {listing.data && (
            <>
              {listing.data.folders.length ? (
                <ul>
                  {listing.data.folders.map((folder) => (
                    <li key={folder.id}>
                      <button type="button" className="ghost" onClick={() => {
                        setTrail([...trail, folder]);
                        setPage(1);
                      }}>{folder.name}</button>
                    </li>
                  ))}
                </ul>
              ) : <small className="muted">此页没有子目录</small>}
              {(page > 1 || listing.data.hasNext) && (
                <div className="offline-directory-actions">
                  <button type="button" className="ghost" disabled={page === 1} onClick={() => setPage(page - 1)}>上一页</button>
                  <small>第 {page} 页</small>
                  <button type="button" className="ghost" disabled={!listing.data.hasNext} onClick={() => setPage(page + 1)}>下一页</button>
                </div>
              )}
            </>
          )}
        </div>
      )}
    </fieldset>
  );
}

export function OfflineDownloadDialog({ name, onClose, submit }: {
  name: string;
  onClose: () => void;
  submit: (directory?: string) => Promise<void>;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const pending = useRef(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [directory, setDirectory] = useState("");
  const pan = useQuery(resources.panStatus(), queryClient);
  useEffect(() => {
    dialog.current?.showModal();
  }, []);

  return (
    <dialog
      ref={dialog}
      className="offline-download-dialog"
      aria-label="提交离线下载"
      onCancel={(event) => {
        event.preventDefault();
        if (!pending.current) onClose();
      }}
    >
      <form onSubmit={(event) => {
        event.preventDefault();
        if (pending.current || !offlineDestinationReady(pan.data, directory)) return;
        pending.current = true;
        setSubmitting(true);
        setError("");
        void submit(directory.trim() || undefined).then(onClose).catch((reason: unknown) => {
          setError(reason instanceof Error ? reason.message : String(reason));
        }).finally(() => {
          pending.current = false;
          setSubmitting(false);
        });
      }}>
        <h2>离线下载</h2>
        <p>{name}</p>
        {pan.isPending && <p role="status">正在检查网盘连接…</p>}
        {pan.isError && <p role="alert" className="danger-text">{pan.error.message}</p>}
        {pan.data?.connected && (
          <OfflineDirectoryPicker key={pan.data.backend} pan={pan.data} value={directory} onChange={setDirectory} disabled={submitting} />
        )}
        {pan.data && !pan.data.connected && <p role="alert">请先在设置中连接 115 或 OpenList。</p>}
        {error && <p role="alert" className="danger-text">{error}</p>}
        <div className="offline-directory-actions">
          <button type="button" className="ghost" disabled={submitting} onClick={onClose}>取消</button>
          <button type="submit" disabled={submitting || !offlineDestinationReady(pan.data, directory)}>
            {submitting ? "提交中…" : "提交离线下载"}
          </button>
        </div>
      </form>
    </dialog>
  );
}
