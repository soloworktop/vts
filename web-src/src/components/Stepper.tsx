// 阶段 stepper：done ✓ / skipped – / active / failed 标红 / 未开始。
// 数据来自 computeStages（lib/events.ts），阶段集合由调用方传入。
interface StepperProps {
  stages: { key: string; label: string }[];
  doneSet: Set<string>;
  skippedSet?: Set<string>;
  activeStage: string | null;
  failedStage?: string | null;
}

export function Stepper({ stages, doneSet, skippedSet, activeStage, failedStage = null }: StepperProps) {
  const skipped = skippedSet ?? new Set<string>();
  return (
    <div className="stepper">
      {stages.map((s, i) => {
        const state = doneSet.has(s.key)
          ? "done"
          : skipped.has(s.key)
            ? "skipped"
            : s.key === failedStage
              ? "failed"
              : s.key === activeStage
                ? "active"
                : "";
        const knob = doneSet.has(s.key) ? "✓" : skipped.has(s.key) ? "–" : String(i + 1);
        // track 在 span 内位于 stage 之前，视觉上连接「上一阶段 → 当前阶段」；
        // 上一阶段已完成时高亮（替代旧版 .stage.done + .track 死选择器）
        const trackDone = i > 0 && doneSet.has(stages[i - 1].key);
        return (
          <span key={s.key}>
            {i > 0 && <i className={`track${trackDone ? " track-done" : ""}`}></i>}
            <div className={`stage ${state}`} data-stage={s.key}>
              <i className="knob">{knob}</i>
              <span>{s.label}</span>
            </div>
          </span>
        );
      })}
    </div>
  );
}
