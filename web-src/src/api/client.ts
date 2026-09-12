// ============ API 层唯一出口 ============
// 所有后端请求集中在 src/api/ 一处。base path 常量 = /api/v1（canonical，见
// app.py::API_PREFIX）。禁止在组件/视图里散落硬编码 URL——一律经 endpoints.ts。
//
// 类型策略（计划 §5.3「openapi-typescript 生成或手写等价类型，择一」）：
// 采用**手写等价类型**（types.ts）。理由：
//   1. openapi-typescript 需要运行中的服务产出 /openapi.json 作为构建期输入，
//      使「纯静态构建」依赖一个外部进程，CI/离线构建都会变脆；
//   2. 前端实际消费的响应形状很小且稳定（~20 个端点），手写类型可直接对照
//      app.py 的 JSONResponse 字面量，零代码生成噪声；
//   3. 契约防漂移由 Python 侧 tests/test_doc_consistency.py 兜底（路由↔README），
//      前端类型以 app.py 的响应字面量为唯一事实源，逐字段注释出处。

export const API_BASE = "/api/v1";

export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;

  constructor(status: number, detail: string) {
    super(detail || `HTTP ${status}`);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail || `HTTP ${status}`;
  }
}

export interface RequestOptions {
  method?: string;
  body?: unknown;
  rawBody?: BodyInit; // 非 JSON body（如 zip 上传）
  headers?: Record<string, string>;
  signal?: AbortSignal;
}

async function readDetail(res: Response): Promise<string> {
  try {
    const data = await res.json();
    if (data && typeof data.detail === "string") return data.detail;
    if (data && typeof data.detail === "object" && data.detail) {
      // FastAPI 422 校验错误形状：detail: [{loc, msg, type}, ...]
      const first = Array.isArray(data.detail) ? data.detail[0] : null;
      if (first && first.msg) return String(first.msg);
    }
  } catch {
    /* 非 JSON 响应 */
  }
  return "";
}

/** 统一 JSON 请求。返回反序列化后的响应体。 */
export async function apiFetch<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = { ...(opts.headers || {}) };
  if (opts.rawBody == null && opts.body !== undefined) {
    headers["Content-Type"] = "application/json";
  }
  const res = await fetch(`${API_BASE}${path}`, {
    method: opts.method || "GET",
    headers,
    body: opts.rawBody !== undefined ? opts.rawBody : opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
    signal: opts.signal,
  });
  if (!res.ok) {
    const detail = await readDetail(res);
    throw new ApiError(res.status, detail || `HTTP ${res.status}`);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

/** 便捷方法：GET / POST / PUT / DELETE。 */
export const api = {
  get: <T>(path: string, opts: RequestOptions = {}) => apiFetch<T>(path, { ...opts, method: "GET" }),
  post: <T>(path: string, body?: unknown, opts: RequestOptions = {}) =>
    apiFetch<T>(path, { ...opts, method: "POST", body }),
  put: <T>(path: string, body?: unknown, opts: RequestOptions = {}) =>
    apiFetch<T>(path, { ...opts, method: "PUT", body }),
  del: <T>(path: string, opts: RequestOptions = {}) => apiFetch<T>(path, { ...opts, method: "DELETE" }),
};

/** 下载类 URL（真实 attachment，禁 Blob 拼下载——WKWebView 下会被内联渲染）。 */
export function downloadUrl(path: string): string {
  return `${API_BASE}${path}`;
}
