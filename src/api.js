import { koreanError } from '../shared/ko.js';

export async function api(path, options = {}) {
  let response;
  try { response = await fetch(`/api${path}`, { ...options, headers: { 'Content-Type': 'application/json', ...options.headers }, body: options.body === undefined ? undefined : JSON.stringify(options.body) }); }
  catch { throw new Error('서버에 연결할 수 없습니다. 서버 실행 상태를 확인해 주세요.'); }
  if (response.status === 204) return null;
  const data = await response.json();
  if (!response.ok) throw new Error(koreanError(data));
  return data;
}
export const money = n => n == null ? '—' : `${Math.round(n).toLocaleString('ko-KR')}원`;
export function shortDate(date, opts = {}) { return date ? new Date(`${date.slice(0, 10)}T12:00:00`).toLocaleDateString('ko-KR', { month: 'long', day: 'numeric', ...opts }) : '—'; }
export const clockTime = iso => iso?.slice(11, 16) || '—';
export const duration = n => n == null ? '비행시간 정보 없음' : `${Math.floor(n / 60)}시간 ${n % 60}분`;
export function dateTime(iso, timezone = 'Asia/Seoul') { return iso ? new Date(iso).toLocaleString('ko-KR', { month: 'long', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false, timeZone: timezone }) : '예약된 확인 없음'; }
