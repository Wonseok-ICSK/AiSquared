import shared from './en-shared.json';
import english from './en.json';

export function getLanguage() {
  try { return localStorage.getItem('wayfinder-language') === 'en' ? 'en' : 'ko'; } catch { return 'ko'; }
}
let language = getLanguage();
export function setLanguage(value) {
  language = value === 'en' ? 'en' : 'ko';
  try { localStorage.setItem('wayfinder-language', language); } catch { /* Storage can be disabled. */ }
  document.documentElement.lang = language;
  document.title = language === 'en' ? 'Wayfinder — Your travel companion' : 'Wayfinder — 나만의 여행 도우미';
}
export const locale = () => language === 'en' ? 'en-US' : 'ko-KR';
const vocabulary = { ...shared, ...english };
const pattern = new RegExp(Object.keys(vocabulary).sort((a,b) => b.length-a.length).map(s => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|'), 'g');
// Translate display strings only; React elements, callbacks and form values stay intact.
export function translate(value) {
  if (language !== 'en' || typeof value !== 'string') return value;
  return value.replace(pattern, match => vocabulary[match]);
}
setLanguage(language);
