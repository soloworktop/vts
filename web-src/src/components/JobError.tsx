// 失败三段式展示：人话标题 → 建议(+可选「去处理」按钮) → 默认折叠的原始错误全文。
// 状态页卡片与历史详情共用。action 的 panels 指向设置 tab（activatePanels 语义）。
import { classifyError } from "../lib/errors";

export interface JobErrorProps {
  rawError: string;
  onAction?: (panels: string[]) => void;
}

export function JobError({ rawError, onAction }: JobErrorProps) {
  const raw = String(rawError || "job failed");
  const friendly = classifyError(raw);
  return (
    <div className="error visible">
      {friendly && (
        <>
          <div className="error-head">{friendly.title}</div>
          <div className="error-hint">{friendly.hint}</div>
          {friendly.action && (
            <button
              type="button"
              className="secondary error-action"
              onClick={() => onAction?.(friendly.action!.panels)}
            >
              {friendly.action.label}
            </button>
          )}
        </>
      )}
      {!friendly && <div className="error-head">任务失败</div>}
      <details className="error-raw">
        <summary>原始错误</summary>
        <pre>{raw}</pre>
      </details>
    </div>
  );
}
