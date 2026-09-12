// 极简 toast：右下角浮现，自动消失，供全局轻量反馈复用。
// 读屏可达：容器 aria-live=polite；error 单条 role=alert。
import { createContext, useCallback, useContext, useRef, useState, type ReactNode } from "react";

export interface ToastAction {
  label: string;
  onClick: () => void;
}

export interface ToastOptions {
  kind?: "info" | "error";
  ttl?: number;
  action?: ToastAction;
}

interface ToastItem extends ToastOptions {
  id: number;
  message: string;
  visible: boolean;
}

interface ToastContextValue {
  showToast: (message: string, opts?: ToastOptions) => void;
}

const ToastContext = createContext<ToastContextValue>({ showToast: () => undefined });

export function useToast(): ToastContextValue {
  return useContext(ToastContext);
}

let nextId = 1;

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const timers = useRef<Map<number, ReturnType<typeof setTimeout>>>(new Map());

  const dismiss = useCallback((id: number) => {
    setToasts((list) => list.map((t) => (t.id === id ? { ...t, visible: false } : t)));
    const timer = timers.current.get(id);
    if (timer) clearTimeout(timer);
    timers.current.delete(id);
    setTimeout(() => setToasts((list) => list.filter((t) => t.id !== id)), 300);
  }, []);

  const showToast = useCallback(
    (message: string, opts: ToastOptions = {}) => {
      const id = nextId++;
      const ttl = opts.ttl ?? 2600;
      setToasts((list) => [...list, { id, message, visible: true, ...opts }]);
      const timer = setTimeout(() => dismiss(id), ttl);
      timers.current.set(id, timer);
    },
    [dismiss],
  );

  return (
    <ToastContext.Provider value={{ showToast }}>
      {children}
      <div className="toast-wrap" aria-live="polite">
        {toasts.map((t) => (
          <div
            key={t.id}
            className={`toast${t.visible ? " visible" : ""}${t.kind === "error" ? " toast-error" : ""}`}
            role={t.kind === "error" ? "alert" : undefined}
          >
            <span>{t.message}</span>
            {t.action && (
              <button
                type="button"
                className="toast-action"
                onClick={() => {
                  dismiss(t.id);
                  t.action?.onClick();
                }}
              >
                {t.action.label}
              </button>
            )}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}
