// 最近来源下拉：URL 与本地路径两类对称复用（有历史才渲染）。
// 末项「清空记录」一键清空该类历史（本地输入框一律 autocomplete="off"，历史只走这里）。
import { useState } from "react";
import { clearRecentSources, clearValue, storedRecentSources } from "../lib/recent";
import { shortenPath } from "../lib/format";
import { RECENT_PATHS_KEY, RECENT_URLS_KEY } from "../lib/constants";

export interface RecentSelectProps {
  id: string;
  placeholder: string;
  storageKey: string;
  onPick: (value: string) => void;
}

export function RecentSelect({ id, placeholder, storageKey, onPick }: RecentSelectProps) {
  const [items, setItems] = useState<string[]>(() => storedRecentSources(storageKey));
  if (!items.length) return null;

  return (
    <select
      id={id}
      autoComplete="off"
      className="recent-select"
      defaultValue=""
      onChange={(e) => {
        const v = e.target.value;
        if (v === clearValue) {
          clearRecentSources(storageKey);
          setItems([]); // 清空后整个下拉移除
          return;
        }
        if (v) {
          onPick(v);
          e.target.value = "";
        }
      }}
    >
      <option value="">{placeholder}</option>
      {items.map((p) => (
        <option key={p} value={p} title={p}>
          {shortenPath(p)}
        </option>
      ))}
      <option value="__sep__" disabled>
        ────────
      </option>
      <option value={clearValue}>清空记录</option>
    </select>
  );
}

export { RECENT_PATHS_KEY, RECENT_URLS_KEY };
