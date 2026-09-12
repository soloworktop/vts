// 通用标签编辑器：pill 列表 + 输入框 + 自动补全（/api/labels，包含/前缀匹配、最多 8 条）。
// Enter / 英文逗号 / 中文逗号提交；空输入退格删最后一个 pill。
// 渲染一律走 textContent/DOM API，无 innerHTML 拼接（防注入）。
import { useCallback, useEffect, useRef, useState } from "react";
import { fetchLabels } from "../api/endpoints";
import { UNCATEGORIZED_LABEL } from "../lib/constants";
import { useToast } from "./Toast";

const MAX_LEN = 24;
const MAX_COUNT = 8;

interface TagEditorProps {
  initial?: string[];
  hint?: boolean;
  ariaLabel?: string;
  onChange?: (names: string[]) => void;
}

export function TagEditor({ initial = [], hint = false, ariaLabel = "标签输入", onChange }: TagEditorProps) {
  const [names, setNames] = useState<string[]>([...initial]);
  const [input, setInput] = useState("");
  const [suggestions, setSuggestions] = useState<string[]>([]);
  const [activeIdx, setActiveIdx] = useState(-1);
  const [open, setOpen] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const { showToast } = useToast();

  useEffect(() => {
    onChange?.(names);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [names]);

  const refreshSuggestions = useCallback(async (query: string) => {
    try {
      const data = await fetchLabels();
      const ql = String(query || "").trim().toLowerCase();
      const list = (data.labels || [])
        .map((l) => l.name)
        .filter((n) => !names.includes(n))
        .filter((n) => (ql ? n.toLowerCase().includes(ql) : true))
        .slice(0, 8);
      setSuggestions(list);
      setOpen(list.length > 0);
    } catch {
      setSuggestions([]);
      setOpen(false);
    }
  }, [names]);

  const commit = useCallback(
    (raw: string) => {
      const name = String(raw || "").trim();
      if (!name) return;
      if (name === UNCATEGORIZED_LABEL) {
        showToast("「未分类」为保留名，不能作为标签");
        return;
      }
      if (name.length > MAX_LEN) {
        showToast(`标签最长 ${MAX_LEN} 字符`);
        return;
      }
      if (names.length >= MAX_COUNT) {
        showToast(`每个任务最多 ${MAX_COUNT} 个标签`);
        return;
      }
      if (names.includes(name)) {
        setInput("");
        setOpen(false);
        return;
      }
      setNames((prev) => [...prev, name]);
      setInput("");
      setOpen(false);
      setActiveIdx(-1);
      inputRef.current?.focus();
    },
    [names, showToast],
  );

  const onKeyDown = async (e: React.KeyboardEvent<HTMLInputElement>) => {
    // 输入法组合态（拼音候选未上屏）：Enter/逗号是 IME 操作，不作为提交标签
    if (e.nativeEvent.isComposing || e.keyCode === 229) return;
    if (e.key === "Enter" || e.key === "," || e.key === "，") {
      e.preventDefault();
      if (activeIdx >= 0 && suggestions[activeIdx]) {
        commit(suggestions[activeIdx]);
      } else if (input.trim()) {
        commit(input);
      }
    } else if (e.key === "Backspace" && !input && names.length) {
      setNames((prev) => prev.slice(0, -1));
    } else if ((e.key === "ArrowDown" || e.key === "ArrowUp") && open && suggestions.length) {
      e.preventDefault();
      const delta = e.key === "ArrowDown" ? 1 : -1;
      setActiveIdx((activeIdx + delta + suggestions.length) % suggestions.length);
    } else if (e.key === "Escape") {
      setOpen(false);
      setActiveIdx(-1);
    }
  };

  const onInput = async (value: string) => {
    setInput(value);
    setActiveIdx(-1);
    if (/[,，]/.test(value)) {
      const parts = value.split(/[,，]/).map((s) => s.trim()).filter(Boolean);
      setInput("");
      for (const part of parts) commit(part);
      await refreshSuggestions("");
      return;
    }
    await refreshSuggestions(value);
  };

  return (
    <div className="tag-editor">
      <div className="tag-pills">
        {names.map((name, i) => (
          <span className="tag-pill" key={`${name}-${i}`}>
            <span>{name}</span>
            <button
              type="button"
              className="tag-x"
              title="移除标签"
              onClick={() => setNames((prev) => prev.filter((_, idx) => idx !== i))}
            >
              ×
            </button>
          </span>
        ))}
      </div>
      <div className="tag-input-row">
        <input
          ref={inputRef}
          className="tag-input"
          autoComplete="off"
          placeholder={names.length ? "添加标签…" : "标签（可选）· 回车或逗号确认"}
          aria-label={ariaLabel}
          value={input}
          onChange={(e) => void onInput(e.target.value)}
          onKeyDown={(e) => void onKeyDown(e)}
          onFocus={() => void refreshSuggestions(input)}
          onBlur={() => setTimeout(() => setOpen(false), 150)}
        />
        {open && suggestions.length > 0 && (
          <div className="tag-suggest">
            {suggestions.map((name, idx) => (
              <div
                key={name}
                className={`tag-suggest-item${idx === activeIdx ? " active" : ""}`}
                onMouseDown={(e) => {
                  e.preventDefault();
                  commit(name);
                }}
              >
                {name}
              </div>
            ))}
          </div>
        )}
      </div>
      {hint && <div className="tag-hint">回车或逗号确认；输入时自动补全已有标签</div>}
    </div>
  );
}
