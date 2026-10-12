/** Minimal typed fetch wrapper for the FastAPI backend (`/api`, DESIGN §7). */

import { tr } from "../lib/i18n";
import type { ErrorBody, ValidationErrorItem } from "./types";

export const API_BASE = "/api";

/** An HTTP error from the API, carrying the backend's `detail` message. */
export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;
  readonly fieldErrors: Record<string, string>;

  constructor(status: number, detail: string, fieldErrors: Record<string, string> = {}) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    this.fieldErrors = fieldErrors;
  }
}

type Query = Record<string, string | number | boolean | null | undefined>;

interface RequestOptions {
  query?: Query;
  body?: unknown;
  signal?: AbortSignal;
}

function buildUrl(path: string, query?: Query): string {
  const url = `${API_BASE}${path}`;
  if (!query) return url;
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null || value === "") continue;
    params.set(key, String(value));
  }
  const qs = params.toString();
  return qs ? `${url}?${qs}` : url;
}

/** Field path of a pydantic error, without the leading "body" segment. */
function fieldPath(loc: (string | number)[]): string {
  const parts = loc[0] === "body" ? loc.slice(1) : loc;
  return parts.join(".");
}

function formatValidation(items: ValidationErrorItem[]): {
  message: string;
  fields: Record<string, string>;
} {
  const fields: Record<string, string> = {};
  const lines: string[] = [];
  for (const item of items) {
    const path = fieldPath(item.loc ?? []);
    const msg = (item.msg ?? tr("invalid", "잘못된 값")).replace(/^Value error, /, "");
    if (path && !fields[path]) fields[path] = msg;
    lines.push(path ? `${path}: ${msg}` : msg);
  }
  return { message: lines.join("; ") || tr("Validation failed", "입력값을 확인하세요"), fields };
}

async function parseError(response: Response): Promise<ApiError> {
  let body: ErrorBody | null = null;
  try {
    body = (await response.json()) as ErrorBody;
  } catch {
    body = null;
  }
  const detail = body?.detail;
  if (Array.isArray(detail)) {
    const { message, fields } = formatValidation(detail);
    return new ApiError(response.status, message, fields);
  }
  if (typeof detail === "string" && detail) return new ApiError(response.status, detail);
  const fallback =
    response.status === 404
      ? tr("Not found", "찾을 수 없어요")
      : response.status >= 500
        ? tr(`Server error (${response.status})`, `서버 오류 (${response.status})`)
        : tr(`Request failed (${response.status} ${response.statusText})`, `요청 실패 (${response.status} ${response.statusText})`);
  return new ApiError(response.status, fallback);
}

/** Perform a request and decode JSON (returns `undefined` for empty bodies, e.g. 204). */
export async function request<T>(
  method: string,
  path: string,
  options: RequestOptions = {},
): Promise<T> {
  const init: RequestInit = {
    method,
    headers: { Accept: "application/json" },
    signal: options.signal,
  };
  if (options.body !== undefined) {
    init.body = JSON.stringify(options.body);
    init.headers = { ...init.headers, "Content-Type": "application/json" };
  }
  let response: Response;
  try {
    response = await fetch(buildUrl(path, options.query), init);
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") throw err;
    throw new ApiError(
      0,
      tr(
        "Cannot reach the backend. Is the server running on port 8765?",
        "백엔드에 연결할 수 없어요. 서버가 8765 포트에서 실행 중인지 확인하세요.",
      ),
    );
  }
  if (!response.ok) throw await parseError(response);
  if (response.status === 204) return undefined as T;
  const text = await response.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

export const http = {
  get: <T>(path: string, query?: Query, signal?: AbortSignal) =>
    request<T>("GET", path, { query, signal }),
  post: <T>(path: string, body?: unknown, query?: Query) =>
    request<T>("POST", path, { body, query }),
  put: <T>(path: string, body?: unknown) => request<T>("PUT", path, { body }),
  del: <T>(path: string) => request<T>("DELETE", path),
};

/** Human-readable message for any thrown value. */
export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.detail;
  if (err instanceof Error) return err.message;
  return String(err);
}

/** True for an aborted fetch (ignore these). */
export function isAbort(err: unknown): boolean {
  return err instanceof DOMException && err.name === "AbortError";
}
