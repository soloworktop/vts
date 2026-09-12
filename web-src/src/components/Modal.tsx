// 通用模态框：遮罩点击/Esc 关闭（可禁用），role=dialog + aria-modal。
// 焦点管理：打开时移焦到卡片内第一个可聚焦元素（无则聚焦卡片本身），
// 关闭时把焦点还给打开前的元素；Tab 循环圈定在卡片内（focus trap）。
import { useEffect, useRef, type ReactNode } from "react";

export interface ModalProps {
  open: boolean;
  onClose: () => void;
  title?: string;
  children: ReactNode;
  className?: string; // modal-card 上的额外类
  dismissOnOverlay?: boolean;
  dismissOnEsc?: boolean;
  ariaLabel?: string;
}

const FOCUSABLE =
  'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])';

export function Modal({
  open,
  onClose,
  title,
  children,
  className = "",
  dismissOnOverlay = true,
  dismissOnEsc = true,
  ariaLabel,
}: ModalProps) {
  const cardRef = useRef<HTMLDivElement | null>(null);

  // 打开时移焦 / 关闭时还焦
  useEffect(() => {
    if (!open) return;
    const prev = document.activeElement as HTMLElement | null;
    const card = cardRef.current;
    const focusables = card?.querySelectorAll<HTMLElement>(FOCUSABLE);
    (focusables && focusables.length ? focusables[0] : card)?.focus();
    return () => prev?.focus();
  }, [open]);

  // Tab 圈焦：焦点跑到卡片外时拉回第一个/最后一个可聚焦元素
  useEffect(() => {
    if (!open) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key !== "Tab") return;
      const card = cardRef.current;
      if (!card) return;
      const focusables = Array.from(card.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
        (el) => !el.hasAttribute("disabled"),
      );
      if (!focusables.length) return;
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [open]);

  useEffect(() => {
    if (!open || !dismissOnEsc) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [open, dismissOnEsc, onClose]);

  if (!open) return null;
  return (
    <div
      className="modal-overlay"
      role="dialog"
      aria-modal="true"
      aria-label={ariaLabel}
      onMouseDown={(e) => {
        if (dismissOnOverlay && e.target === e.currentTarget) onClose();
      }}
    >
      <div ref={cardRef} tabIndex={-1} className={`modal-card ${className}`}>
        {title && <h3>{title}</h3>}
        {children}
      </div>
    </div>
  );
}
